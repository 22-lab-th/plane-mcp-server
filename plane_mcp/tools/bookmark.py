"""Shared workspace bookmarks and groups."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from plane_mcp.client import get_plane_client_context
from plane_mcp.extensions import present, request, segment
from plane_mcp.toolkit import Action, build_annotations, build_description, needs

NAME = "bookmark"
LEGACY: dict[str, str] = {}
ACTIONS = (
    Action("list", (), ("search", "group_id"), read=True, note="group_id='ungrouped' filters ungrouped bookmarks"),
    Action("retrieve", ("bookmark_id",), read=True),
    Action("create", ("title", "url"), ("group_id", "remark", "sort_order")),
    Action("update", ("bookmark_id",), ("title", "url", "group_id", "ungroup", "remark", "sort_order")),
    Action("delete", ("bookmark_id",), destructive=True),
    Action(
        "get_url_metadata",
        ("url",),
        read=True,
        note="explicitly fetches public URL metadata using the server's SSRF protections",
    ),
    Action("list_groups", read=True),
    Action("retrieve_group", ("group_id",), read=True),
    Action("create_group", ("name",), ("sort_order",)),
    Action("update_group", ("group_id",), ("name", "sort_order")),
    Action("delete_group", ("group_id",), destructive=True, note="bookmarks in the group become ungrouped"),
)


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description(
            "Manage shared workspace bookmarks and groups.",
            ACTIONS,
            "Lists return all matching rows. Search matches title, URL and remark. Only supplied update fields change; "
            "remark='' clears a remark, sort_order=0 is valid. Use ungroup=true to clear group membership.",
        ),
        annotations=build_annotations("Bookmarks", ACTIONS),
    )
    def bookmark(
        action: Literal[
            "list",
            "retrieve",
            "create",
            "update",
            "delete",
            "get_url_metadata",
            "list_groups",
            "retrieve_group",
            "create_group",
            "update_group",
            "delete_group",
        ],
        bookmark_id: str = "",
        group_id: str | None = None,
        title: str | None = None,
        url: str | None = None,
        remark: str | None = None,
        name: str | None = None,
        sort_order: float | None = None,
        search: str = "",
        ungroup: bool = False,
    ) -> dict[str, Any] | list | str:
        values = locals()
        declaration = next(a for a in ACTIONS if a.name == action)
        if error := needs(action, **{key: values[key] for key in declaration.requires}):
            return error
        if ungroup and group_id:
            return "Error: ungroup cannot be combined with group_id"
        client, workspace = get_plane_client_context()
        root = f"{segment(workspace)}/bookmarks"
        if action == "get_url_metadata":
            return request(client, "post", f"{root}/metadata", data={"url": url})
        if action == "list":
            return request(client, "get", root, params=present(q=search or None, group=group_id))
        group_action = action.endswith("_group") or action == "list_groups"
        if group_action:
            root = f"{segment(workspace)}/bookmark-groups"
        identifier = group_id if group_action else bookmark_id
        if action.startswith(("retrieve", "update", "delete")):
            root += f"/{segment(identifier)}"
        if action.startswith(("list", "retrieve")):
            return request(client, "get", root)
        if action.startswith("delete"):
            result = request(client, "delete", root)
            return result or {"id": identifier, "deleted": True}
        data = (
            present(name=name, sort_order=sort_order)
            if group_action
            else present(title=title, url=url, remark=remark, group=group_id, sort_order=sort_order)
        )
        if ungroup:
            data["group"] = None
        if not data:
            return "Error: update requires at least one changed field"
        return request(client, "post" if action.startswith("create") else "patch", root, data=data)
