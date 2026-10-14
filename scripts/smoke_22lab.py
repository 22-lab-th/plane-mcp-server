"""Verify a configured 22lab backend through a fresh stdio MCP process.

Reads only by default. --write requires two projects whose names start with
'MCP validation', reserved for disposable operator fixtures. Fixture cleanup is
performed by the release operator using the recorded workspace IDs.
Write fixtures need a test token with 600 requests/minute because imports make
multiple API requests inside one tool call.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests
from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def smoke(options):
    passed = []
    origin = os.environ["PLANE_BASE_URL"].rstrip("/")
    workspace = os.environ["PLANE_WORKSPACE_SLUG"]
    project = options.project_id
    env = {**os.environ, "PLANE_BACKEND_PROFILE": "22lab", "LOG_PAYLOADS": "false"}
    with tempfile.TemporaryDirectory(prefix="plane-mcp-smoke-") as directory:
        env["PLANE_FILE_UPLOAD_ROOTS"] = directory
        env["PLANE_STORAGE_UPLOAD_HOSTS"] = "127.0.0.1,localhost"
        with tempfile.TemporaryFile(mode="w+") as logs:
            transport = StdioTransport(
                sys.executable,
                ["-m", "plane_mcp", "stdio"],
                env=env,
                cwd=str(Path(__file__).resolve().parents[1]),
                log_file=logs,
            )
            async with Client(transport) as client:

                async def call(tool, action, **args):
                    # Space core reads; import calls may each make multiple API requests.
                    await asyncio.sleep(1.1)
                    result = await client.call_tool(tool, {"action": action, **args}, raise_on_error=False)
                    if result.is_error:
                        raise AssertionError(f"{tool}.{action}: {result.structured_content or result.content}")
                    passed.append(f"{tool}.{action}")
                    return result.data

                tools = await client.list_tools()
                assert len(tools) == 36
                capabilities = await call("workspace", "get_capabilities", project_id=project)
                assert capabilities["extensions_ready"], capabilities
                await asyncio.sleep(18)
                await call("page", "list", project_id=project, per_page=1)
                await call("project_file", "list", project_id=project, per_page=1)
                await call("project_file", "get_storage", project_id=project)
                await call("bookmark", "list")
                for provider in ("jira", "confluence"):
                    await call("atlassian_import", "list_runs", provider=provider, project_id=project)
                if not options.write:
                    return {"mode": "read-only", "passed": passed, "capabilities": capabilities}
                for target in (project, options.target_project_id):
                    record = await call("project", "retrieve", project_id=target)
                    assert record["name"].startswith("MCP validation"), (
                        "Writes require disposable MCP validation projects"
                    )

                tag = str(int(time.time()))
                folder = await call("page", "create_folder", project_id=project, name="Smoke folder " + tag)
                page = await call(
                    "page",
                    "create",
                    project_id=project,
                    parent_id=folder["id"],
                    name="Smoke page " + tag,
                    description_html="<p>MCP verified initial content</p>",
                )
                page_id = page["id"]
                detail = await call("page", "retrieve", project_id=project, page_id=page_id)
                assert detail["parent_id"] == folder["id"]
                assert "verified initial" in detail["description_html"]
                await call(
                    "page",
                    "update",
                    project_id=project,
                    page_id=page_id,
                    description_html="<p>MCP verified changed content</p>",
                )
                await call("page", "move", project_id=project, page_id=page_id, sort_order=14.5)
                detail = await call("page", "retrieve", project_id=project, page_id=page_id)
                assert detail["parent_id"] is None and detail["sort_order"] == 14.5
                assert "verified changed" in detail["description_html"]
                await call("page", "lock", project_id=project, page_id=page_id)
                assert (await call("page", "retrieve", project_id=project, page_id=page_id))["is_locked"] is True
                await call("page", "unlock", project_id=project, page_id=page_id)
                await call("page", "set_access", project_id=project, page_id=page_id, access=1)
                await call("page", "set_access", project_id=project, page_id=page_id, access=0)
                await call("page", "favorite", project_id=project, page_id=page_id)
                await call("page", "unfavorite", project_id=project, page_id=page_id)
                duplicate = await call("page", "duplicate", project_id=project, page_id=page_id)
                await call("page", "get_summary", project_id=project)
                versions = []
                for _ in range(15):
                    versions = await call("page", "list_versions", project_id=project, page_id=page_id)
                    if versions:
                        break
                    await asyncio.sleep(1)
                assert versions, "Page history worker did not produce a version"
                await call(
                    "page", "retrieve_version", project_id=project, page_id=page_id, version_id=versions[0]["id"]
                )

                # PNG bytes accepted by the Page image route and storage.
                import base64

                image = Path(directory) / "image.png"
                image.write_bytes(
                    base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII="
                    )
                )
                asset = await call(
                    "page", "upload_asset_from_path", project_id=project, page_id=page_id, file_path=str(image)
                )
                asset_id = asset.get("asset_id") or asset["id"]
                await call("page", "list_assets", project_id=project, page_id=page_id)
                await call("page", "retrieve_asset", project_id=project, page_id=page_id, asset_id=asset_id)
                await call("page", "download_asset_url", project_id=project, page_id=page_id, asset_id=asset_id)
                await call("page", "delete_asset", project_id=project, page_id=page_id, asset_id=asset_id)

                file_folder = await call("project_file", "create_folder", project_id=project, name="Files " + tag)
                file_folder = file_folder.get("folder", file_folder)
                await call(
                    "project_file",
                    "update_folder",
                    project_id=project,
                    folder_id=file_folder["id"],
                    name="Files renamed " + tag,
                )
                payload = Path(directory) / "smoke.txt"
                payload.write_bytes(b"MCP verified file version one\n")
                uploaded = await call(
                    "project_file",
                    "upload_from_path",
                    project_id=project,
                    file_path=str(payload),
                    folder_id=file_folder["id"],
                )
                file_id = uploaded["file"]["id"]
                await call(
                    "project_file",
                    "complete_upload",
                    project_id=project,
                    file_id=file_id,
                    version_no=1,
                    size_bytes=len(payload.read_bytes()),
                    checksum_sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),
                )
                reserved = await call(
                    "project_file",
                    "initiate_upload",
                    project_id=project,
                    name="aborted-smoke.txt",
                    size_bytes=10,
                    mime_type="text/plain",
                )
                await call(
                    "project_file",
                    "abort_upload",
                    project_id=project,
                    file_id=reserved["file"]["id"],
                    version_no=reserved["version_no"],
                )
                await call("project_file", "retrieve", project_id=project, file_id=file_id)
                delivery = await call("project_file", "download_url", project_id=project, file_id=file_id)
                body = requests.get(delivery.get("url") or delivery["download_url"], timeout=30)
                body.raise_for_status()
                assert hashlib.sha256(body.content).digest() == hashlib.sha256(payload.read_bytes()).digest()
                await call("project_file", "preview_url", project_id=project, file_id=file_id)
                await call(
                    "project_file", "get_web_url", project_id=project, file_id=file_id, folder_id=file_folder["id"]
                )
                payload.write_bytes(b"MCP verified file version two\n")
                await call(
                    "project_file", "upload_from_path", project_id=project, file_id=file_id, file_path=str(payload)
                )
                await call("project_file", "list_versions", project_id=project, file_id=file_id)
                await call("project_file", "activate_version", project_id=project, file_id=file_id, version_no=2)
                delivery = await call("project_file", "download_url", project_id=project, file_id=file_id)
                body = requests.get(delivery.get("url") or delivery["download_url"], timeout=30)
                body.raise_for_status()
                assert body.content == payload.read_bytes()
                await call(
                    "project_file",
                    "update",
                    project_id=project,
                    file_id=file_id,
                    is_pinned=True,
                    name="smoke-renamed.txt",
                )
                link = await call(
                    "project_file", "link", project_id=project, file_id=file_id, entity_type="page", entity_id=page_id
                )
                link = link.get("link", link)
                await call(
                    "project_file", "list_entity_links", project_id=project, entity_type="page", entity_id=page_id
                )
                await call("project_file", "unlink", project_id=project, file_id=file_id, link_id=link["id"])
                await call("project_file", "copy", project_id=project, file_id=file_id)
                copied = await call(
                    "project_file",
                    "copy_to_project",
                    project_id=project,
                    file_id=file_id,
                    target_project_id=options.target_project_id,
                )
                copied_id = copied.get("id") or copied["file"]["id"]
                await call(
                    "project_file",
                    "move_to_project",
                    project_id=options.target_project_id,
                    file_id=copied_id,
                    target_project_id=project,
                    confirm=True,
                )
                await call("project_file", "list_activity", project_id=project)
                await call("project_file", "delete", project_id=project, file_id=file_id)
                await call("project_file", "restore", project_id=project, file_id=file_id)
                await call("project_file", "delete", project_id=project, file_id=file_id)
                await call("project_file", "purge", project_id=project, file_id=file_id, confirm=True)
                await call(
                    "project_file",
                    "delete_folder",
                    project_id=project,
                    folder_id=file_folder["id"],
                    recursive=True,
                    confirm=True,
                )

                group = await call("bookmark", "create_group", name="Smoke " + tag)
                await call("bookmark", "retrieve_group", group_id=group["id"])
                await call("bookmark", "update_group", group_id=group["id"], name="Smoke renamed " + tag)
                await call("bookmark", "list_groups")
                bookmark = await call("bookmark", "create", title="Smoke " + tag, url=origin, group_id=group["id"])
                await call("bookmark", "retrieve", bookmark_id=bookmark["id"])
                await call(
                    "bookmark", "update", bookmark_id=bookmark["id"], remark="verified", ungroup=True, sort_order=0
                )
                await call("bookmark", "delete", bookmark_id=bookmark["id"])
                await call("bookmark", "delete_group", group_id=group["id"])
                markdown = Path(directory) / "smoke.md"
                markdown.write_text("# Markdown import verified\n\nMCP smoke document.\n")
                await call("page", "import_markdown_from_path", project_id=project, file_path=str(markdown))
                bundle = Path(directory) / "markdown.zip"
                with zipfile.ZipFile(bundle, "w") as archive:
                    archive.writestr("nested/guide.md", "# Nested guide\n\n![Pixel](../image.png)\n")
                    archive.writestr("image.png", image.read_bytes())
                imported = await call(
                    "page", "import_markdown_bundle_from_path", project_id=project, file_path=str(bundle)
                )
                assert not imported.get("error"), imported
                export = Path(directory) / "confluence.zip"
                with zipfile.ZipFile(export, "w") as archive:
                    archive.writestr("index.html", "<title>MCP Confluence fixture " + tag + "</title>")
                    archive.writestr(
                        "Home_1.html",
                        '<h1 id="title-text">Home</h1><div id="main-content">'
                        '<p>Before media</p><img src="attachments/1/image.png"><p>After media</p></div>',
                    )
                    archive.writestr("attachments/1/image.png", image.read_bytes())
                imported = await call(
                    "atlassian_import", "import_html_export_from_path", project_id=project, file_path=str(export)
                )
                assert imported["pages_imported"] == 1 and imported["files_uploaded"] == 1, imported
                assert imported["counts"]["failed"] == 0
                await call("page", "archive", project_id=project, page_id=page_id)
                await call("page", "restore", project_id=project, page_id=page_id)
                await call("page", "archive", project_id=project, page_id=page_id)
                await call("page", "delete", project_id=project, page_id=page_id)
                duplicate_id = duplicate.get("id") or duplicate.get("page", {}).get("id")
                if duplicate_id:
                    await call(
                        "page",
                        "move_to_project",
                        project_id=project,
                        page_id=duplicate_id,
                        target_project_id=options.target_project_id,
                    )
    return {"mode": "fixture-writes", "workspace": workspace, "passed": passed, "unique_actions": len(set(passed))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--target-project-id", default="")
    parser.add_argument("--write", action="store_true")
    options = parser.parse_args()
    if options.write and not options.target_project_id:
        parser.error("--write requires --target-project-id")
    print(json.dumps(asyncio.run(smoke(options)), indent=2))


if __name__ == "__main__":
    main()
