"""Read-only checks of core connectivity and optional backend capabilities."""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib.metadata import version
from typing import Any
from urllib.parse import quote

import requests

from plane_mcp.compat import normalize_plane_base_url


def _read(session, url, headers):
    try:
        response = session.get(url, headers=headers, timeout=15)
    except requests.RequestException as exc:
        return {"ok": False, "status": None, "error": type(exc).__name__}, None
    try:
        data = response.json()
    except (ValueError, AttributeError):
        data = None
    return {"ok": 200 <= response.status_code < 300, "status": response.status_code}, data


def _capability(probe):
    status = probe["status"]
    state = (
        "supported"
        if probe["ok"]
        else "forbidden"
        if status in {401, 403}
        else "unsupported"
        if status in {404, 405}
        else "unknown"
    )
    return {**probe, "state": state}


def diagnose_connection(
    *,
    base_url: str,
    workspace_slug: str,
    api_key: str,
    session: requests.Session | None = None,
    project_id: str = "",
    access_token: str = "",
) -> dict[str, Any]:
    origin = normalize_plane_base_url(base_url)
    root = f"{origin}/api/v1/workspaces/{quote(workspace_slug, safe='')}"
    client = session or requests.Session()
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    else:
        headers["X-Api-Key"] = api_key
    projects, data = _read(client, f"{root}/projects/", headers)
    report = {
        "base_url": origin,
        "workspace_slug": workspace_slug,
        "mcp_version": version("plane-mcp-server"),
        "connected": projects["ok"],
        "core_api": projects,
        "extensions_ready": False,
        "capabilities": {},
    }
    if not projects["ok"]:
        report["pages_api"] = {"ok": False, "status": None, "supported": False}
        return report
    pages, _ = _read(client, f"{root}/pages/", headers)
    report["pages_api"] = {**pages, "supported": pages["ok"]}
    if not project_id and isinstance(data, dict):
        rows = data.get("results") or []
        if rows:
            project_id = rows[0].get("id", "")
    if not project_id:
        report["capabilities"] = {
            "project_extensions": {"state": "unknown", "reason": "No accessible project selected"}
        }
        return report
    report["project_id"] = project_id
    project = f"{root}/projects/{quote(project_id, safe='')}"
    selected, _ = _read(client, f"{project}/", headers)
    report["selected_project"] = _capability(selected)
    if not selected["ok"]:
        return report
    endpoints = {
        "project_files": f"{project}/files/",
        "file_storage": f"{project}/files/storage/",
        "bookmarks": f"{root}/bookmarks/",
        "page_summary": f"{project}/pages-summary/",
        "confluence_imports": f"{project}/confluence/runs/",
        "jira_imports": f"{project}/jira/runs/",
        "workitem_types": f"{project}/work-item-types/",
        "workspace_features": f"{root}/features/",
        "workspace_workitems": f"{root}/work-items/?per_page=1",
        "workitem_count": f"{root}/work-items/count/",
    }
    capabilities = report["capabilities"]
    for name, url in endpoints.items():
        probe, _ = _read(client, url, headers)
        capabilities[name] = _capability(probe)
        if probe["status"] == 401:
            return report
    page_probe, page_data = _read(client, f"{project}/pages/?per_page=1", headers)
    page_rows = page_data.get("results", []) if isinstance(page_data, dict) else []
    if page_probe["ok"] and page_rows:
        page_id = quote(str(page_rows[0]["id"]), safe="")
        probe, _ = _read(client, f"{project}/pages/{page_id}/versions/", headers)
        capabilities["page_versions"] = _capability(probe)
        if probe["status"] == 404:
            capabilities["page_versions"]["state"] = "unknown"
    else:
        capabilities["page_versions"] = {"state": "unknown", "reason": "No accessible sample Page"}
    pql_probe, pql_data = _read(client, f"{project}/work-items/?per_page=1&pql=priority%20%3D%20%22urgent%22", headers)
    capabilities["pql"] = _capability(pql_probe)
    if pql_probe["status"] == 400 and "not supported" in json.dumps(pql_data).lower():
        capabilities["pql"]["state"] = "unsupported"
    required = ("project_files", "file_storage", "bookmarks", "page_summary", "confluence_imports", "jira_imports")
    report["extensions_ready"] = all(capabilities[name]["state"] == "supported" for name in required)
    return report


def run_doctor() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--require-extensions", action="store_true")
    options = parser.parse_args(sys.argv[2:])
    missing = [name for name in ("PLANE_API_KEY", "PLANE_WORKSPACE_SLUG") if not os.getenv(name)]
    if missing:
        print(json.dumps({"connected": False, "missing_environment": missing}, indent=2))
        return 2
    try:
        report = diagnose_connection(
            base_url=os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "https://api.plane.so"),
            workspace_slug=os.environ["PLANE_WORKSPACE_SLUG"],
            api_key=os.environ["PLANE_API_KEY"],
            project_id=options.project_id,
        )
    except ValueError as exc:
        print(json.dumps({"connected": False, "configuration_error": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["connected"] and (report["extensions_ready"] or not options.require_extensions) else 1
