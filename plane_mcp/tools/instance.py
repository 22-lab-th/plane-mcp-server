"""Explicitly enabled instance-administrator Atlassian configuration."""

from __future__ import annotations

import os
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token

from plane_mcp.client import get_plane_client_context
from plane_mcp.extensions import present, request
from plane_mcp.toolkit import Action, build_annotations, build_description, needs

NAME = "instance"
LEGACY: dict[str, str] = {}
ACTIONS = (
    Action(
        "get_connector",
        ("provider",),
        read=True,
        note="redacted God Mode configuration; unscoped instance-admin token only",
    ),
    Action(
        "update_connector",
        ("provider", "confirm"),
        ("enabled", "site_url", "email", "cloud_id", "token_from_env", "clear_token"),
        destructive=True,
    ),
    Action(
        "test_connector",
        ("provider",),
        read=True,
        note="tests saved connection; does not prove visibility of every source item",
    ),
)


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description(
            "Administer Confluence and Jira connections in God Mode.",
            ACTIONS,
            "Instance operations require PLANE_ENABLE_INSTANCE_TOOLS=1 and an unscoped API token "
            "owned by an instance admin. "
            "token_from_env reads PLANE_CONFLUENCE_API_TOKEN or PLANE_JIRA_API_TOKEN in local stdio only; "
            "Never send a token in tool arguments. Omit token options to keep the saved token. "
            "Disable before clearing.",
        ),
        annotations=build_annotations("Instance connectors", ACTIONS),
    )
    def instance(
        action: Literal["get_connector", "update_connector", "test_connector"],
        provider: Literal["confluence", "jira"] | None = None,
        enabled: bool | None = None,
        site_url: str | None = None,
        email: str | None = None,
        cloud_id: str | None = None,
        token_from_env: bool = False,
        clear_token: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any] | str:
        if error := needs(action, provider=provider, **({"confirm": confirm} if action == "update_connector" else {})):
            return error
        if os.getenv("PLANE_ENABLE_INSTANCE_TOOLS") != "1":
            return "Error: configure PLANE_ENABLE_INSTANCE_TOOLS=1 to use instance administration"
        if action == "update_connector" and not confirm:
            return "Error: update_connector requires confirm=true after reviewing the configuration"
        if token_from_env and clear_token:
            return "Error: choose token_from_env or clear_token, not both"
        data = present(enabled=enabled, site_url=site_url, email=email, cloud_id=cloud_id)
        if token_from_env:
            access = get_access_token()
            if access and access.claims.get("auth_method") != "api_key_env":
                return "Error: server environment credentials can only be used by local stdio clients"
            secret = os.getenv(f"PLANE_{provider.upper()}_API_TOKEN")
            if not secret:
                return f"Error: missing PLANE_{provider.upper()}_API_TOKEN"
            data["api_token"] = secret
        elif clear_token:
            data["api_token"] = ""
        if action == "update_connector" and not data:
            return "Error: update_connector requires at least one changed field"
        client, _workspace = get_plane_client_context()
        if action == "get_connector":
            result = request(client, "get", provider, instance=True)
            if isinstance(result, dict):
                result.pop("api_token", None)
            return result
        if action == "test_connector":
            return request(client, "post", f"{provider}/test", data={}, instance=True)
        result = request(client, "patch", provider, data=data, instance=True)
        # Defensive omission even when connecting to a different backend revision.
        if isinstance(result, dict):
            result.pop("api_token", None)
        return result
