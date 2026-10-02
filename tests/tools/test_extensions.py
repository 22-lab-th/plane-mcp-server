"""Public extension contracts, transport serialization and recovery semantics."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
import requests
from fastmcp import Client, FastMCP
from plane import PlaneClient
from plane.errors import HttpError

from plane_mcp.extensions import request
from plane_mcp.project_files import upload_path
from plane_mcp.tools import atlassian_import, instance, register_tools
from plane_mcp.tools.project_file import FileFilters


@pytest.mark.parametrize("provider", ["confluence", "jira"])
@pytest.mark.parametrize(
    "action,mode",
    [
        ("start", "changed"),
        ("sync", "changed"),
        ("retry_failed", "failed"),
        ("retry_selected", "selected"),
        ("overwrite", "all"),
    ],
)
def test_import_modes_and_mapping(provider, action, mode, registered, spy):
    spy.returns["pages._post"] = {"id": "run", "source_id": "source", "status": "queued"}
    arguments = {"action": action, "provider": provider, "project_id": "p", "confirm": True}
    if action == "start":
        arguments["remote_id"] = "100"
    else:
        arguments["source_id"] = "source"
    if action == "retry_selected":
        arguments["item_ids"] = ["failed-item"]
    if provider == "jira" and action in {"start", "sync"}:
        arguments["user_mapping"] = [atlassian_import.UserMapping(account_id="jira-person", member_id="plane-person")]
    result = registered["atlassian_import"].fn(**arguments)
    call = spy.recorder.only()
    assert call.kwargs["endpoint"] == f"acme/projects/p/{provider}/runs"
    assert call.kwargs["data"]["mode"] == mode
    if action == "start":
        field = "space_id" if provider == "confluence" else "remote_project_id"
        assert call.kwargs["data"][field] == "100"
    if provider == "jira" and action in {"start", "sync"}:
        assert call.kwargs["data"]["user_mapping"] == {"jira-person": "plane-person"}
    assert result["status"] == "queued"


def test_space_search_and_result_pagination(registered, spy):
    registered["atlassian_import"].fn(action="list_spaces", project_id="p", search="Team & Docs", cursor="next")
    assert spy.recorder.only().kwargs["params"] == {"search": "Team & Docs", "cursor": "next"}
    spy.recorder.calls.clear()
    spy.returns["pages._get"] = {
        "phase": "Finished",
        "counts": {"failed": 1},
        "next_offset": 100,
        "items": [{"status": "failed", "error": "Permission denied", "code": "atlassian_403"}],
    }
    result = registered["atlassian_import"].fn(
        action="retrieve_run", project_id="p", provider="jira", run_id="r", offset=0, status="failed"
    )
    assert spy.recorder.only().kwargs["params"] == {"offset": 0, "status": "failed"}
    assert result["items"][0]["code"] == "atlassian_403"
    assert result["next_offset"] == 100


def test_conflict_and_quota_reasons_are_preserved_without_repeating_write(spy):
    spy.returns["pages._post"] = HttpError(
        message="Conflict", status_code=409, response={"error": "Already running", "run_id": "r"}
    )
    result = request(spy, "post", "acme/projects/p/confluence/runs", data={"mode": "changed"})
    assert result == {"error": "Already running", "run_id": "r", "status_code": 409}
    assert len(spy.recorder.calls) == 1


def test_explicit_file_filters_and_zero_false_clear_values(registered, spy):
    registered["project_file"].fn(
        action="list", project_id="p", filters=FileFilters(pinned=False, size_min=0), cursor="next", per_page=1000
    )
    assert spy.recorder.only().kwargs["params"] == {"pinned": False, "size_min": 0, "cursor": "next", "page_size": 200}
    spy.recorder.calls.clear()
    registered["project_file"].fn(action="update", project_id="p", file_id="f", to_root=True, is_pinned=False)
    assert spy.recorder.only().kwargs["data"] == {"folder_id": None, "is_pinned": False}
    spy.recorder.calls.clear()
    registered["bookmark"].fn(action="update", bookmark_id="b", ungroup=True, remark="", sort_order=0)
    assert spy.recorder.only().kwargs["data"] == {"group": None, "remark": "", "sort_order": 0}


@pytest.mark.parametrize(
    "action,args",
    [
        ("purge", {"file_id": "f"}),
        ("move_to_project", {"file_id": "f", "target_project_id": "p2"}),
        ("delete_folder", {"folder_id": "d", "recursive": True}),
    ],
)
def test_irreversible_file_operations_require_confirmation(action, args, registered, spy):
    result = registered["project_file"].fn(action=action, project_id="p", **args)
    assert "confirm" in result
    assert not spy.recorder.calls


@pytest.mark.parametrize("action", ["get_connector", "update_connector", "test_connector"])
def test_instance_is_disabled_by_default(action, registered, spy, monkeypatch):
    monkeypatch.delenv("PLANE_ENABLE_INSTANCE_TOOLS", raising=False)
    result = registered["instance"].fn(action=action, provider="jira", confirm=True, enabled=False)
    assert "PLANE_ENABLE_INSTANCE_TOOLS" in result
    assert not spy.recorder.calls


def test_instance_uses_admin_base_path_and_scrubs_secret(registered, monkeypatch):
    monkeypatch.setenv("PLANE_ENABLE_INSTANCE_TOOLS", "1")
    client = PlaneClient(api_key="fake-admin", base_url="http://localhost:8000")
    monkeypatch.setattr(instance, "get_plane_client_context", lambda: (client, "acme"))
    from plane.api.base_resource import BaseResource

    seen = []

    def get(self, endpoint):
        seen.append((self.base_path, endpoint))
        return {"enabled": False, "api_token": "secret"}

    monkeypatch.setattr(BaseResource, "_get", get)
    result = registered["instance"].fn(action="get_connector", provider="jira")
    assert seen == [("/instance", "jira")]
    assert result == {"enabled": False}


def test_remote_cannot_read_server_environment_secrets(registered, monkeypatch):
    monkeypatch.setenv("PLANE_ENABLE_INSTANCE_TOOLS", "1")
    monkeypatch.setenv("PLANE_JIRA_API_TOKEN", "server-secret")
    monkeypatch.setattr(instance, "get_access_token", lambda: SimpleNamespace(claims={"auth_method": "pat"}))
    monkeypatch.setattr(instance, "request", lambda *args, **kwargs: pytest.fail("must not call Plane"))
    result = registered["instance"].fn(action="update_connector", provider="jira", confirm=True, token_from_env=True)
    assert "local stdio" in result
    assert "server-secret" not in result


def test_streamed_upload_verifies_exact_bytes_and_signed_headers(tmp_path, monkeypatch):
    from plane_mcp import project_files

    content = b"test-file-bytes"
    path = tmp_path / "notes.txt"
    path.write_bytes(content)
    monkeypatch.setenv("PLANE_FILE_UPLOAD_ROOTS", str(tmp_path))
    monkeypatch.setenv("PLANE_STORAGE_UPLOAD_HOSTS", "storage.local")
    calls, puts = [], []

    def api(client, method, endpoint, **kwargs):
        calls.append((endpoint, kwargs["data"]))
        if endpoint.endswith("initiate-upload"):
            return {
                "file": {"id": "f"},
                "version_no": 2,
                "upload": {
                    "url": "http://storage.local/signed?secret=signature",
                    "headers": {"Content-Type": "text/plain", "If-None-Match": "*"},
                },
            }
        return {"file": {"id": "f"}, "status": "verified"}

    def put(url, **kwargs):
        puts.append((url, kwargs["data"].read(), kwargs))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(project_files, "request", api)
    monkeypatch.setattr(project_files.requests, "put", put)
    client = SimpleNamespace(pages=SimpleNamespace(config=SimpleNamespace(base_path="http://plane.local/api/v1")))
    result = upload_path(client, "acme", "p", str(path), file_id="f")
    assert result["status"] == "verified"
    assert puts[0][1] == content
    assert puts[0][2]["headers"] == {"Content-Type": "text/plain", "If-None-Match": "*"}
    assert puts[0][2]["allow_redirects"] is False
    assert calls[-1][0].endswith("f/complete-upload")
    assert calls[-1][1] == {
        "version_no": 2,
        "size_bytes": len(content),
        "checksum_sha256": hashlib.sha256(content).hexdigest(),
    }


def test_upload_failure_aborts_and_does_not_disclose_signed_url(tmp_path, monkeypatch):
    from plane_mcp import project_files

    path = tmp_path / "file.txt"
    path.write_bytes(b"a")
    monkeypatch.setenv("PLANE_FILE_UPLOAD_ROOTS", str(tmp_path))
    calls = []

    def api(client, method, endpoint, **kwargs):
        calls.append(endpoint)
        if endpoint.endswith("initiate-upload"):
            return {
                "file": {"id": "f"},
                "version_no": 1,
                "upload": {"url": "http://plane.local/secret-signature", "headers": {}},
            }
        raise requests.ConnectionError("abort unavailable")

    def put(*args, **kwargs):
        raise requests.ConnectionError("secret-signature")

    monkeypatch.setattr(project_files, "request", api)
    monkeypatch.setattr(project_files.requests, "put", put)
    client = SimpleNamespace(pages=SimpleNamespace(config=SimpleNamespace(base_path="http://plane.local/api/v1")))
    result = upload_path(client, "acme", "p", str(path))
    assert "secret-signature" not in str(result)
    assert result["file_id"] == "f"
    assert calls[-1].endswith("/abort-upload")
    assert not any("complete-upload" in call for call in calls)


def test_upload_outside_roots_never_reserves_quota(tmp_path, monkeypatch):
    from plane_mcp import project_files

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    path = tmp_path / "outside.txt"
    path.write_text("secret")
    monkeypatch.setenv("PLANE_FILE_UPLOAD_ROOTS", str(allowed))
    monkeypatch.setattr(project_files, "request", lambda *a, **kw: pytest.fail("no API call"))
    with pytest.raises(ValueError, match="inside PLANE_FILE_UPLOAD_ROOTS"):
        upload_path(None, "acme", "p", str(path))


def test_mcp_wire_accepts_structured_filters_and_user_mappings(spy):
    spy.returns["pages._get"] = {"results": [], "page": {"next_cursor": None}}
    spy.returns["pages._post"] = {"id": "run", "source_id": "source", "status": "queued"}
    mcp = FastMCP("wire")
    register_tools(mcp, legacy_names=False)

    async def run():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "project_file", {"action": "list", "project_id": "p", "filters": {"pinned": False, "size_min": 0}}
            )
            assert not result.is_error
            result = await client.call_tool(
                "atlassian_import",
                {
                    "action": "start",
                    "provider": "jira",
                    "project_id": "p",
                    "remote_id": "100",
                    "user_mapping": [{"account_id": "j", "member_id": "u"}],
                },
            )
            assert not result.is_error
            assert result.data["status"] == "queued"

    asyncio.run(run())
    assert spy.recorder.calls[-1].kwargs["data"]["user_mapping"] == {"j": "u"}
