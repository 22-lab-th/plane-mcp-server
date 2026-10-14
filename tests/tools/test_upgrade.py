"""Compatibility checks through real HTTP and the registered MCP transport."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from plane.errors import HttpError
from plane.models.pages import Page

from plane_mcp.capabilities import unsupported_operation
from plane_mcp.doctor import diagnose_connection
from plane_mcp.tools import register_tools


@pytest.fixture
def backend():
    calls = []
    statuses = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.path)
            code = statuses.get(self.path, 200)
            body = {"results": []}
            if self.path.endswith("/projects/"):
                body = {"results": [{"id": "project"}]}
            if self.path.endswith("/pages/?per_page=1"):
                body = {"results": [{"id": "page"}]}
            if "pql=" in self.path:
                code, body = 400, {"pql": "PQL is not supported on this edition"}
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", statuses, calls
    server.shutdown()
    thread.join()
    server.server_close()


def test_doctor_reports_partial_backend_even_with_healthy_core(backend):
    origin, statuses, calls = backend
    statuses["/api/v1/workspaces/acme/projects/project/files/"] = 404
    statuses["/api/v1/workspaces/acme/projects/project/jira/runs/"] = 403
    statuses["/api/v1/workspaces/acme/projects/project/pages/page/versions/"] = 404
    result = diagnose_connection(base_url=origin, workspace_slug="acme", api_key="secret")
    assert result["connected"] is True
    assert result["extensions_ready"] is False
    assert result["capabilities"]["project_files"]["state"] == "unsupported"
    assert result["capabilities"]["jira_imports"]["state"] == "forbidden"
    assert result["capabilities"]["page_versions"]["state"] == "unknown"
    assert result["capabilities"]["pql"]["state"] == "unsupported"
    assert "secret" not in json.dumps(result)
    assert len(calls) == len(set(calls))


def test_doctor_does_not_probe_extensions_after_bad_project_or_auth(backend):
    origin, statuses, calls = backend
    statuses["/api/v1/workspaces/acme/projects/project/"] = 404
    result = diagnose_connection(base_url=origin, workspace_slug="acme", api_key="secret")
    assert result["capabilities"] == {}
    assert result["extensions_ready"] is False
    assert not any("/files/" in path for path in calls)
    calls.clear()
    statuses["/api/v1/workspaces/acme/projects/"] = 401
    diagnose_connection(base_url=origin, workspace_slug="acme", api_key="secret")
    assert calls == ["/api/v1/workspaces/acme/projects/"]


def test_doctor_and_workspace_tool_use_matching_backend(backend, monkeypatch):
    from plane import PlaneClient

    origin, _, _ = backend
    plane = PlaneClient(base_url=origin, api_key="secret")
    monkeypatch.setattr("plane_mcp.tools.workspace.get_plane_client_context", lambda: (plane, "acme"))
    result = _call("workspace", {"action": "get_capabilities"})
    assert not result.is_error
    assert result.data["extensions_ready"] is True


def _call(tool, arguments):
    mcp = FastMCP("upgrade-regressions")
    register_tools(mcp, legacy_names=False)

    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)

    return asyncio.run(run())


def test_extension_error_has_mcp_flag_and_recovery_identifiers(spy):
    spy.returns["pages._post"] = HttpError("conflict", 409, {"error": "Already running", "run_id": "run-1"})
    result = _call("atlassian_import", {"action": "start", "provider": "jira", "project_id": "p", "remote_id": "100"})
    assert result.is_error
    assert result.structured_content == {"error": "Already running", "status_code": 409, "run_id": "run-1"}
    assert len(spy.recorder.calls) == 1


def test_sdk_errors_have_same_wire_shape(spy):
    spy.returns["pages.retrieve_project_page"] = HttpError("forbidden", 403, {"detail": "Denied", "code": "private"})
    result = _call("page", {"action": "retrieve", "project_id": "p", "page_id": "x"})
    assert result.is_error
    assert result.structured_content["status_code"] == 403
    assert result.structured_content["code"] == "private"


def test_fractional_page_order_and_canonical_parent_reach_wire(spy):
    result = _call("page", {"action": "move", "project_id": "p", "page_id": "x", "sort_order": 14.5})
    assert not result.is_error
    assert spy.recorder.only().kwargs["data"] == {"parent": None, "sort_order": 14.5}
    spy.recorder.calls.clear()
    spy.returns["pages.retrieve_project_page"] = Page(id="x", parent="folder", node_type="page", color="red")
    result = _call("page", {"action": "retrieve", "project_id": "p", "page_id": "x"})
    assert result.data["parent_id"] == "folder"
    assert result.data["node_type"] == "page"
    assert result.data["color"] == "red"


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_page_order_cannot_write(value, registered, spy):
    result = registered["page"].fn(action="move", project_id="p", page_id="x", sort_order=value)
    assert "finite" in result
    assert not spy.recorder.calls


@pytest.mark.parametrize(
    "tool,args",
    [
        ("page", {"action": "delete", "page_id": "x"}),
        ("page", {"action": "archive", "page_id": "x", "archive": False}),
        ("workitem", {"action": "list"}),
        ("workitem", {"action": "count", "project_id": "p"}),
        ("workitem_type", {"action": "resolve", "name": "Epic", "project_id": "p"}),
        ("workitem", {"action": "list", "project_id": "p", "pql": 'priority = "urgent"'}),
    ],
)
def test_explicit_22lab_profile_blocks_unsupported_calls_before_plane(tool, args, monkeypatch, spy):
    monkeypatch.setenv("PLANE_BACKEND_PROFILE", "22lab")
    result = _call(tool, args)
    assert result.is_error
    assert result.structured_content["code"] == "unsupported_capability"
    assert not spy.recorder.calls


def test_profile_does_not_restrict_upstream(monkeypatch):
    monkeypatch.setenv("PLANE_BACKEND_PROFILE", "upstream")
    assert unsupported_operation("workitem_type", {"action": "list"}) is None


def test_files_web_url_is_stable_encoded_and_requires_no_api(registered, spy, monkeypatch):
    monkeypatch.delenv("PLANE_BASE_URL", raising=False)
    spy.pages.config = SimpleNamespace(base_path="https://plane.example/api/v1")
    result = registered["project_file"].fn(
        action="get_web_url", project_id="project/1", folder_id="folder 1", file_id="file/1"
    )
    assert result["url"] == "https://plane.example/acme/projects/project%2F1/files/?folder=folder+1&file=file%2F1"
    assert not spy.recorder.calls


def test_files_web_url_uses_public_origin_for_internal_api_clients(registered, spy, monkeypatch):
    monkeypatch.setenv("PLANE_BASE_URL", "https://plane.example")
    spy.pages.config = SimpleNamespace(base_path="http://api:8000/api/v1")
    result = registered["project_file"].fn(action="get_web_url", project_id="p", file_id="f")
    assert result["url"] == "https://plane.example/acme/projects/p/files/?file=f"
    assert not spy.recorder.calls
