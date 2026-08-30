"""Connection diagnostics for API-key based self-hosted Plane servers."""

from __future__ import annotations

import json
import os
from typing import Any

import requests

from plane_mcp.compat import normalize_plane_base_url


def _probe(session: requests.Session, url: str, headers: dict[str, str]) -> dict[str, Any]:
    try:
        response = session.get(url, headers=headers, timeout=15)
    except requests.RequestException as exc:
        return {"ok": False, "status": None, "error": type(exc).__name__}
    return {"ok": 200 <= response.status_code < 300, "status": response.status_code}


def diagnose_connection(
    *,
    base_url: str,
    workspace_slug: str,
    api_key: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Probe core and Pages API routes without exposing the API key."""
    origin = normalize_plane_base_url(base_url)
    root = f"{origin}/api/v1/workspaces/{workspace_slug}"
    client = session or requests.Session()
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    projects = _probe(client, f"{root}/projects/", headers)
    pages = _probe(client, f"{root}/pages/", headers)
    return {
        "base_url": origin,
        "workspace_slug": workspace_slug,
        "connected": projects["ok"],
        "core_api": projects,
        "pages_api": {**pages, "supported": pages["ok"]},
    }


def run_doctor() -> int:
    """Run diagnostics from environment variables and print JSON only."""
    missing = [name for name in ("PLANE_API_KEY", "PLANE_WORKSPACE_SLUG") if not os.getenv(name)]
    if missing:
        print(json.dumps({"connected": False, "missing_environment": missing}, indent=2))
        return 2

    try:
        report = diagnose_connection(
            base_url=os.getenv("PLANE_BASE_URL", "https://api.plane.so"),
            workspace_slug=os.environ["PLANE_WORKSPACE_SLUG"],
            api_key=os.environ["PLANE_API_KEY"],
        )
    except ValueError as exc:
        print(json.dumps({"connected": False, "configuration_error": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["connected"] else 1
