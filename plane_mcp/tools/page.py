"""Pages, at workspace or project scope, and their links to work items.

Every page action is scoped by whether project_id is supplied: with it the page
is a project page, without it a workspace page. The SDK has a separate endpoint
pair for each, so the branch is explicit rather than a default.
"""

from __future__ import annotations

import mimetypes
import os
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

import requests
from fastmcp import FastMCP
from plane.errors import HttpError
from plane.models.pages import CreatePage, Page, UpdatePage
from plane.models.query_params import PaginatedQueryParams
from plane.models.work_item_pages import CreateWorkItemPage, WorkItemPage

from plane_mcp.attachments import HTTP_TIMEOUT, READABLE_IMAGE_TYPES, UPLOAD_SIZE_LIMIT, assert_public_url
from plane_mcp.client import get_plane_client_context
from plane_mcp.toolkit import Action, as_params, build_annotations, build_description, envelope, missing, needs

NAME = "page"
TITLE = "Pages"

ACTIONS = (
    Action(
        "list",
        (),
        ("project_id", "parent_id", "folders_only", "cursor", "per_page"),
        note="workspace pages unless project_id is given; parent_id='root' lists root nodes",
        read=True,
    ),
    Action("retrieve", ("page_id",), ("project_id",), read=True),
    Action(
        "create",
        ("name",),
        (
            "project_id",
            "description_html",
            "access",
            "color",
            "is_locked",
            "external_source",
            "external_id",
            "parent_id",
            "sort_order",
        ),
    ),
    Action(
        "create_folder",
        ("project_id", "name"),
        ("parent_id", "access", "color", "sort_order"),
        note="creates a document folder; parent_id omitted means root",
    ),
    Action(
        "update",
        ("page_id",),
        (
            "project_id",
            "name",
            "description_html",
            "access",
            "color",
            "is_locked",
            "external_source",
            "external_id",
            "parent_id",
            "sort_order",
        ),
        note="only the fields you pass are changed",
    ),
    Action("archive", ("page_id",), ("project_id",), destructive=True),
    Action("restore", ("project_id", "page_id"), note="restores an archived page or folder tree"),
    Action(
        "move",
        ("project_id", "page_id"),
        ("parent_id", "sort_order"),
        note="move to parent_id, or omit parent_id to move to root",
    ),
    Action("list_assets", ("project_id", "page_id"), ("cursor", "per_page"), read=True),
    Action("retrieve_asset", ("project_id", "page_id", "asset_id"), read=True),
    Action("download_asset_url", ("project_id", "page_id", "asset_id"), read=True),
    Action(
        "upload_asset_from_path",
        ("project_id", "page_id", "file_path"),
        ("name",),
        note="file_path must be inside a directory listed in PLANE_FILE_UPLOAD_ROOTS",
    ),
    Action(
        "upload_asset_from_url",
        ("project_id", "page_id", "url"),
        ("name",),
        note="the URL must be public and is checked against SSRF redirects",
    ),
    Action(
        "import_markdown_from_path",
        ("project_id", "file_path"),
        ("parent_id", "access", "remote_images", "on_error", "dry_run"),
        note="imports one Markdown file and referenced images; file_path must be inside PLANE_FILE_UPLOAD_ROOTS",
    ),
    Action(
        "import_markdown_bundle_from_path",
        ("project_id", "file_path"),
        ("parent_id", "access", "remote_images", "on_error", "dry_run"),
        note="imports a Markdown directory or ZIP bundle while preserving folders",
    ),
    Action("delete_asset", ("project_id", "page_id", "asset_id"), destructive=True),
    Action("list_workitem_pages", ("project_id", "workitem_id"), read=True),
    Action("attach_to_workitem", ("project_id", "workitem_id", "page_id")),
    Action(
        "detach_from_workitem",
        ("project_id", "workitem_id", "workitem_page_id"),
        note="workitem_page_id is the link id from list_workitem_pages, not the page id",
        destructive=True,
    ),
)

FOOTER = (
    "description_html is the page body as HTML. access is the page access level. "
    "Omit project_id to work with workspace-level pages. Page assets currently require a project page. "
    "Image uploads return image_html; insert that exact image-component into description_html with update. "
    "Markdown imports copy relative images into Page assets, can copy or keep public remote images, and return an "
    "import report. ZIP imports reject traversal paths and symlinks. "
    "Folders support nesting; the API rejects cycles and access mismatches. PNG, JPEG, GIF, and WebP are supported "
    "up to 5 MB."
)

LEGACY = {
    "list_pages": "list",
    "retrieve_page": "retrieve",
    "create_page": "create",
    "list_work_item_pages": "list_workitem_pages",
    "attach_page_to_work_item": "attach_to_workitem",
    "detach_page_from_work_item": "detach_from_workitem",
}


def _patch_page(client, workspace_slug: str, page_id: str, data: UpdatePage, project_id: str = "") -> Page:
    """Patch a page through the public API missing from plane-sdk 0.2.20."""
    if project_id:
        endpoint = f"{workspace_slug}/projects/{project_id}/pages/{page_id}"
    else:
        endpoint = f"{workspace_slug}/pages/{page_id}"
    response = client.pages._patch(endpoint, data.model_dump(exclude_none=True))
    return Page.model_validate(response)


def _page_api_error(error: HttpError) -> str:
    """Turn a missing public Pages API into an actionable self-hosted error."""
    if error.status_code == 404:
        return (
            "Error: Plane Pages API is not available through the API-token route on this instance. "
            "The Plane web UI may still expose Pages through session-authenticated app routes, but MCP must not reuse "
            "a browser cookie. Upgrade or extend the Plane backend to expose /api/v1 workspace/project Pages endpoints."
        )
    raise error


def _asset_endpoint(workspace_slug: str, project_id: str, page_id: str, asset_id: str = "") -> str:
    endpoint = f"{workspace_slug}/projects/{project_id}/pages/{page_id}/assets"
    return f"{endpoint}/{asset_id}" if asset_id else endpoint


def _project_page_endpoint(workspace_slug: str, project_id: str, page_id: str = "") -> str:
    endpoint = f"{workspace_slug}/projects/{project_id}/pages"
    return f"{endpoint}/{page_id}" if page_id else endpoint


def _image_html(asset_id: str) -> str:
    return f'<image-component src="{asset_id}" alignment="center" status="uploaded"></image-component>'


def _normalise_image(payload: bytes, filename: str, content_type: str) -> tuple[str, str]:
    if len(payload) > UPLOAD_SIZE_LIMIT:
        raise ValueError(
            f"Image is {len(payload) / 1024 / 1024:.1f} MB; maximum is {UPLOAD_SIZE_LIMIT // 1024 // 1024} MB"
        )
    if not payload:
        raise ValueError("Image is empty")

    content_type = content_type.split(";", 1)[0].strip().lower()
    if not content_type or content_type == "application/octet-stream":
        content_type = mimetypes.guess_type(filename)[0] or ""
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    if content_type not in READABLE_IMAGE_TYPES:
        raise ValueError("Page images must be PNG, JPEG, GIF, or WebP")

    signatures = {
        "image/png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": payload.startswith(b"\xff\xd8\xff"),
        "image/gif": payload.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP",
    }
    if not signatures[content_type]:
        raise ValueError(f"File contents do not match declared type {content_type}")
    return filename, content_type


def _read_allowed_file(file_path: str, name: str) -> tuple[bytes, str, str]:
    roots = [
        Path(value).expanduser().resolve()
        for value in os.getenv("PLANE_FILE_UPLOAD_ROOTS", "").split(os.pathsep)
        if value
    ]
    if not roots:
        raise ValueError("Local uploads are disabled; configure PLANE_FILE_UPLOAD_ROOTS first")

    path = Path(file_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Not a regular file: {file_path!r}")
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError(f"File is outside PLANE_FILE_UPLOAD_ROOTS: {file_path!r}")
    if path.stat().st_size > UPLOAD_SIZE_LIMIT:
        raise ValueError(f"Image exceeds the {UPLOAD_SIZE_LIMIT // 1024 // 1024} MB upload limit")

    filename = name or path.name
    payload = path.read_bytes()
    filename, content_type = _normalise_image(payload, filename, mimetypes.guess_type(filename)[0] or "")
    return payload, filename, content_type


def _fetch_public_image(url: str, name: str) -> tuple[bytes, str, str]:
    current_url = url
    for _ in range(6):
        assert_public_url(current_url)
        try:
            response = requests.get(current_url, timeout=HTTP_TIMEOUT, allow_redirects=False)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ValueError(f"Failed to fetch image from {current_url!r}: {exc}") from exc

        if 300 <= response.status_code < 400 and response.headers.get("Location"):
            current_url = urljoin(current_url, response.headers["Location"])
            continue

        declared_size = response.headers.get("Content-Length")
        if declared_size and int(declared_size) > UPLOAD_SIZE_LIMIT:
            raise ValueError(f"Image exceeds the {UPLOAD_SIZE_LIMIT // 1024 // 1024} MB upload limit")
        filename = name or Path(requests.utils.urlparse(current_url).path).name or "page-image"
        payload = response.content
        filename, content_type = _normalise_image(payload, filename, response.headers.get("Content-Type", ""))
        return payload, filename, content_type
    raise ValueError("Too many redirects while fetching page image")


def _upload_asset(
    client, workspace_slug: str, project_id: str, page_id: str, payload: bytes, name: str, content_type: str
):
    endpoint = _asset_endpoint(workspace_slug, project_id, page_id)
    created = client.pages._post(endpoint, {"name": name, "type": content_type, "size": len(payload)})
    asset_id = created.get("asset_id") or created.get("id")
    upload_data = created.get("upload_data") or {}
    upload_url = upload_data.get("url")
    if not asset_id or not upload_url:
        raise ValueError("Page asset API did not return asset_id and upload_data.url")

    try:
        response = requests.post(
            upload_url,
            data=upload_data.get("fields") or {},
            files={"file": (name, payload, content_type)},
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        client.pages._patch(f"{endpoint}/{asset_id}", {"is_uploaded": True})
        verified = client.pages._get(f"{endpoint}/{asset_id}")
    except (requests.RequestException, HttpError) as exc:
        try:
            client.pages._delete(f"{endpoint}/{asset_id}")
        except HttpError:
            pass
        raise ValueError(f"Page image upload failed: {exc}") from exc

    if not verified.get("is_uploaded"):
        raise ValueError("Page asset API did not confirm the uploaded image")
    return {**verified, "image_html": _image_html(asset_id)}


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description("Pages at workspace or project scope.", ACTIONS, FOOTER),
        annotations=build_annotations(TITLE, ACTIONS),
    )
    def page(
        action: Literal[
            "list",
            "retrieve",
            "create",
            "create_folder",
            "update",
            "archive",
            "restore",
            "move",
            "list_assets",
            "retrieve_asset",
            "download_asset_url",
            "upload_asset_from_path",
            "upload_asset_from_url",
            "import_markdown_from_path",
            "import_markdown_bundle_from_path",
            "delete_asset",
            "list_workitem_pages",
            "attach_to_workitem",
            "detach_from_workitem",
        ],
        project_id: str = "",
        page_id: str = "",
        workitem_id: str = "",
        workitem_page_id: str = "",
        asset_id: str = "",
        file_path: str = "",
        url: str = "",
        remote_images: Literal["copy", "keep"] = "copy",
        on_error: Literal["stop", "continue"] = "stop",
        dry_run: bool = False,
        name: str | None = None,
        description_html: str | None = None,
        parent_id: str | None = None,
        folders_only: bool = False,
        sort_order: int | None = None,
        # Left unset rather than defaulted: 0 is a real access level.
        access: int | None = None,
        color: str | None = None,
        is_locked: bool | None = None,
        external_source: str | None = None,
        external_id: str | None = None,
        cursor: str = "",
        per_page: int = 0,
    ) -> Page | WorkItemPage | list[WorkItemPage] | dict[str, Any] | str | None:
        client, workspace_slug = get_plane_client_context()

        if action in {"import_markdown_from_path", "import_markdown_bundle_from_path"}:
            if error := needs(action, project_id=project_id, file_path=file_path):
                return error
            from plane_mcp.page_import import import_markdown_path

            try:
                return import_markdown_path(
                    client=client,
                    workspace_slug=workspace_slug,
                    project_id=project_id,
                    file_path=file_path,
                    parent_id=parent_id,
                    access=access if access is not None else 0,
                    remote_images=remote_images,
                    on_error=on_error,
                    dry_run=dry_run,
                )
            except HttpError as error:
                return _page_api_error(error)

        if action == "list":
            params = as_params(PaginatedQueryParams, cursor=cursor, per_page=per_page)
            try:
                if project_id:
                    raw_params = params.model_dump(exclude_none=True) if params else {}
                    if parent_id is not None:
                        raw_params["parent"] = parent_id
                    if folders_only:
                        raw_params["folders_only"] = "true"
                    return client.pages._get(
                        _project_page_endpoint(workspace_slug, project_id), params=raw_params or None
                    )
                else:
                    response = client.pages.list_workspace_pages(workspace_slug=workspace_slug, params=params)
            except HttpError as error:
                return _page_api_error(error)
            return envelope(response)

        if action == "retrieve":
            if not page_id:
                return missing(action, "page_id")
            try:
                if project_id:
                    return client.pages.retrieve_project_page(
                        workspace_slug=workspace_slug, project_id=project_id, page_id=page_id
                    )
                return client.pages.retrieve_workspace_page(workspace_slug=workspace_slug, page_id=page_id)
            except HttpError as error:
                return _page_api_error(error)

        if action == "create":
            if error := needs(action, name=name):
                return error
            payload = {
                key: value
                for key, value in {
                    "name": name,
                    "description_html": description_html,
                    "access": access,
                    "color": color,
                    "is_locked": is_locked,
                    "external_id": external_id,
                    "external_source": external_source,
                    "parent": parent_id,
                    "sort_order": sort_order,
                }.items()
                if value is not None
            }
            data = CreatePage(
                name=name,
                description_html=description_html or "<p></p>",
                access=access,
                color=color,
                is_locked=is_locked,
                external_id=external_id,
                external_source=external_source,
            )
            try:
                if project_id:
                    return client.pages._post(_project_page_endpoint(workspace_slug, project_id), payload)
                return client.pages.create_workspace_page(workspace_slug=workspace_slug, data=data)
            except HttpError as error:
                return _page_api_error(error)

        if action == "create_folder":
            if error := needs(action, project_id=project_id, name=name):
                return error
            payload = {
                key: value
                for key, value in {
                    "name": name,
                    "node_type": "folder",
                    "access": access,
                    "color": color,
                    "parent": parent_id,
                    "sort_order": sort_order,
                }.items()
                if value is not None
            }
            try:
                return client.pages._post(_project_page_endpoint(workspace_slug, project_id), payload)
            except HttpError as error:
                return _page_api_error(error)

        if action == "update":
            if not page_id:
                return missing(action, "page_id")
            updates = {
                key: value
                for key, value in {
                    "name": name,
                    "description_html": description_html,
                    "access": access,
                    "color": color,
                    "is_locked": is_locked,
                    "external_id": external_id,
                    "external_source": external_source,
                    "parent": parent_id,
                    "sort_order": sort_order,
                }.items()
                if value is not None
            }
            if not updates:
                return "Error: update requires at least one field to change"
            try:
                if "parent" in updates or "sort_order" in updates:
                    if not project_id:
                        return "Error: parent_id and sort_order require project_id"
                    return client.pages._patch(_project_page_endpoint(workspace_slug, project_id, page_id), updates)
                data = UpdatePage(**updates)
                return _patch_page(client, workspace_slug, page_id, data, project_id)
            except HttpError as error:
                return _page_api_error(error)

        if action == "archive":
            if not page_id:
                return missing(action, "page_id")
            try:
                if project_id:
                    client.pages._post(f"{_project_page_endpoint(workspace_slug, project_id, page_id)}/archive", {})
                    return {"page_id": page_id, "archived": True}
                data = UpdatePage(archived_at=date.today().isoformat())
                return _patch_page(client, workspace_slug, page_id, data, project_id)
            except HttpError as error:
                return _page_api_error(error)

        if action == "restore":
            if error := needs(action, project_id=project_id, page_id=page_id):
                return error
            try:
                client.pages._delete(f"{_project_page_endpoint(workspace_slug, project_id, page_id)}/archive")
                return {"page_id": page_id, "archived": False}
            except HttpError as error:
                return _page_api_error(error)

        if action == "move":
            if error := needs(action, project_id=project_id, page_id=page_id):
                return error
            payload: dict[str, Any] = {"parent": parent_id}
            if sort_order is not None:
                payload["sort_order"] = sort_order
            try:
                return client.pages._patch(_project_page_endpoint(workspace_slug, project_id, page_id), payload)
            except HttpError as error:
                return _page_api_error(error)

        if action in {
            "list_assets",
            "retrieve_asset",
            "download_asset_url",
            "upload_asset_from_path",
            "upload_asset_from_url",
            "delete_asset",
        }:
            if error := needs(action, project_id=project_id, page_id=page_id):
                return error
            endpoint = _asset_endpoint(workspace_slug, project_id, page_id, asset_id)
            try:
                if action == "list_assets":
                    params = {key: value for key, value in {"cursor": cursor, "per_page": per_page}.items() if value}
                    return client.pages._get(endpoint, params=params or None)
                if action in {"retrieve_asset", "download_asset_url", "delete_asset"} and not asset_id:
                    return missing(action, "asset_id")
                if action == "retrieve_asset":
                    return client.pages._get(endpoint)
                if action == "download_asset_url":
                    return (client.pages._get(endpoint) or {}).get("download_url")
                if action == "delete_asset":
                    client.pages._delete(endpoint)
                    return {"asset_id": asset_id, "deleted": True}
                if action == "upload_asset_from_path":
                    if not file_path:
                        return missing(action, "file_path")
                    payload, filename, content_type = _read_allowed_file(file_path, name or "")
                else:
                    if not url:
                        return missing(action, "url")
                    payload, filename, content_type = _fetch_public_image(url, name or "")
                return _upload_asset(client, workspace_slug, project_id, page_id, payload, filename, content_type)
            except HttpError as error:
                if error.status_code == 404:
                    return "Error: Page asset API is unavailable, or the page/asset was not found"
                raise

        if error := needs(action, project_id=project_id, workitem_id=workitem_id):
            return error

        if action == "list_workitem_pages":
            response = client.work_items.pages.list(
                workspace_slug=workspace_slug, project_id=project_id, work_item_id=workitem_id
            )
            return response.results

        if action == "attach_to_workitem":
            if not page_id:
                return missing(action, "page_id")
            return client.work_items.pages.create(
                workspace_slug=workspace_slug,
                project_id=project_id,
                work_item_id=workitem_id,
                data=CreateWorkItemPage(page_id=page_id),
            )

        if not workitem_page_id:
            return missing(action, "workitem_page_id")
        client.work_items.pages.delete(
            workspace_slug=workspace_slug,
            project_id=project_id,
            work_item_id=workitem_id,
            work_item_page_id=workitem_page_id,
        )
        return None
