"""Explicit compatibility profile for the 22lab Community Edition backend."""

from __future__ import annotations

import os

from fastmcp.server.middleware import Middleware

from plane_mcp.errors import failure

UNSUPPORTED_22LAB = frozenset(
    {
        "collection",
        "customer",
        "customer_property",
        "customer_request",
        "initiative",
        "milestone",
        "release",
        "release_label",
        "release_tag",
        "template",
        "work_log",
        "workitem_property",
        "workitem_type",
    }
)


def backend_profile() -> str:
    profile = os.getenv("PLANE_BACKEND_PROFILE", "auto").strip().lower()
    if profile not in {"auto", "22lab", "upstream"}:
        raise ValueError("PLANE_BACKEND_PROFILE must be auto, 22lab or upstream")
    return profile


def unsupported_operation(tool: str, args: dict) -> str | None:
    if backend_profile() != "22lab":
        return None
    action = args.get("action")
    if tool in UNSUPPORTED_22LAB or tool == "workspace" and action != "get_capabilities":
        return f"{tool} {action} is not supported by the 22lab Community Edition API."
    if tool == "workitem" and (action == "count" or action == "list" and not args.get("project_id")):
        return "Use project-scoped workitem list and its total_count; this API has no workspace list/count route."
    if args.get("pql") and tool in {"workitem", "cycle", "module"}:
        return "This Plane edition does not support PQL. List all pages of project-scoped results and filter locally."
    if tool == "page" and not args.get("project_id"):
        if action == "delete" or action == "archive" and args.get("archive") is False:
            return "Workspace Page deletion/restoration is unsupported on this backend. Use project-scoped Pages."
    if tool == "project" and action == "worklog_summary":
        return "Work logs are not supported by the 22lab Community Edition API."
    return None


class CheckBackendCapabilities(Middleware):
    async def on_call_tool(self, context, call_next):
        from plane_mcp.tools.registry import alias_table

        name = context.message.name
        args = dict(context.message.arguments or {})
        alias = alias_table().get(name)
        if alias:
            name, action = alias
            args["action"] = action
        if message := unsupported_operation(name, args):
            return failure({"error": message, "code": "unsupported_capability", "backend_profile": "22lab"})
        return await call_next(context)
