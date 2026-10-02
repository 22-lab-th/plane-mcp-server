"""Confluence and Jira background imports, progress and recovery."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict

from plane_mcp.client import get_plane_client_context
from plane_mcp.extensions import present, project_path, request, segment
from plane_mcp.toolkit import Action, build_annotations, build_description, needs

NAME = "atlassian_import"
LEGACY: dict[str, str] = {}
ACTIONS = (
    Action(
        "list_spaces",
        ("project_id",),
        ("search", "cursor"),
        read=True,
        note="Confluence; searches every accessible space by name or key",
    ),
    Action("list_projects", ("project_id",), ("offset",), read=True, note="Jira source projects"),
    Action("list_members", ("project_id",), read=True, note="Plane members eligible for Jira user mapping"),
    Action("list_runs", ("project_id", "provider"), read=True),
    Action(
        "retrieve_run",
        ("project_id", "provider", "run_id"),
        ("offset", "status"),
        read=True,
        note="returns phase, type counts and item failure reasons; follow next_offset",
    ),
    Action(
        "start",
        ("project_id", "provider", "remote_id"),
        ("parent_id", "access", "user_mapping"),
        note="Confluence space or Jira project; enqueues a background job",
    ),
    Action(
        "sync",
        ("project_id", "provider", "source_id"),
        ("user_mapping",),
        note="new, changed and failed source items; keeps existing IDs",
    ),
    Action("retry_failed", ("project_id", "provider", "source_id")),
    Action("retry_selected", ("project_id", "provider", "source_id", "item_ids")),
    Action(
        "overwrite",
        ("project_id", "provider", "source_id", "confirm"),
        destructive=True,
        note="confirm=true replaces all imported content including local edits; retains destination IDs",
    ),
    Action(
        "import_html_export_from_path",
        ("project_id", "file_path"),
        ("parent_id", "access", "dry_run"),
        note="Confluence HTML ZIP with attachments; allowed local roots required; repeated ZIP import creates a copy",
    ),
)


class UserMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str
    member_id: str


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description(
            "Import Confluence Cloud spaces and Jira Cloud projects into Plane.",
            ACTIONS,
            "Connections are configured in God Mode. Confluence pages keep hierarchy and embedded media positions; "
            "Jira includes work items, comments, attachments and sprints as Cycles. "
            "Files use Project Files permissions. "
            "Read source_id from a run; item_ids are result item IDs, not remote IDs. One active run per source. "
            "History returns the latest 20 runs. Do not resubmit after a timeout; inspect history first. "
            "Jira visibility restrictions are not copied; target project permissions apply.",
        ),
        annotations=build_annotations("Atlassian imports", ACTIONS),
    )
    def atlassian_import(
        action: Literal[
            "list_spaces",
            "list_projects",
            "list_members",
            "list_runs",
            "retrieve_run",
            "start",
            "sync",
            "retry_failed",
            "retry_selected",
            "overwrite",
            "import_html_export_from_path",
        ],
        project_id: str = "",
        provider: Literal["confluence", "jira"] | None = None,
        remote_id: str = "",
        source_id: str = "",
        run_id: str = "",
        search: str = "",
        cursor: str = "",
        offset: int = 0,
        status: Literal["pending", "running", "completed", "failed", "skipped"] | None = None,
        parent_id: str | None = None,
        access: Literal[0, 1] = 0,
        user_mapping: list[UserMapping] | None = None,
        item_ids: list[str] | None = None,
        confirm: bool = False,
        file_path: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any] | list | str:
        values = locals()
        declaration = next(a for a in ACTIONS if a.name == action)
        if error := needs(action, **{key: values[key] for key in declaration.requires}):
            return error
        if action == "import_html_export_from_path":
            if error := needs(action, file_path=file_path):
                return error
            from plane_mcp.confluence_export import import_export

            client, workspace = get_plane_client_context()
            return import_export(
                client, workspace, project_id, file_path, parent_id=parent_id, access=access, dry_run=dry_run
            )
        fixed = {"list_spaces": "confluence", "list_projects": "jira", "list_members": "jira"}
        provider = fixed.get(action, provider)
        if error := needs(action, provider=provider):
            return error
        if offset < 0 or len(search) > 200:
            return "Error: offset must be nonnegative and search at most 200 characters"
        if provider == "confluence" and user_mapping is not None:
            return "Error: user_mapping is only supported for Jira"
        if provider == "jira" and (parent_id is not None or access):
            return "Error: parent_id and access are only supported for Confluence"
        if user_mapping is not None and (
            len(user_mapping) > 500 or len({row.account_id for row in user_mapping}) != len(user_mapping)
        ):
            return "Error: use at most 500 unique Jira account mappings"
        if action == "retrieve_run" and (error := needs(action, run_id=run_id)):
            return error
        if action == "start" and (error := needs(action, remote_id=remote_id)):
            return error
        if action == "start" and (not remote_id.isascii() or not remote_id.isdigit()):
            return "Error: remote_id must be a numeric Atlassian ID"
        if action in {"sync", "retry_failed", "retry_selected", "overwrite"} and (
            error := needs(action, source_id=source_id)
        ):
            return error
        if action == "retry_selected" and (not item_ids or len(item_ids) > 500):
            return "Error: retry_selected requires 1 to 500 item_ids"
        if action == "overwrite" and not confirm:
            return "Error: overwrite requires confirm=true after reviewing the source and local edits"
        client, workspace = get_plane_client_context()
        root = project_path(workspace, project_id, provider)
        if action == "list_spaces":
            return request(
                client, "get", f"{root}/spaces", params=present(search=search or None, cursor=cursor or None)
            )
        if action == "list_projects":
            return request(client, "get", f"{root}/projects", params={"offset": offset})
        if action == "list_members":
            return request(client, "get", f"{root}/members")
        if action == "list_runs":
            return request(client, "get", f"{root}/runs")
        if action == "retrieve_run":
            return request(
                client, "get", f"{root}/runs/{segment(run_id)}", params=present(offset=offset, status=status)
            )
        mode = {
            "start": "changed",
            "sync": "changed",
            "retry_failed": "failed",
            "retry_selected": "selected",
            "overwrite": "all",
        }[action]
        data = {"mode": mode}
        if action == "start":
            data["space_id" if provider == "confluence" else "remote_project_id"] = remote_id
            if provider == "confluence":
                data.update(parent_id=parent_id, access=access)
        else:
            data["source_id"] = source_id
        if action == "retry_selected":
            data["item_ids"] = item_ids
        if user_mapping is not None:
            data["user_mapping"] = {row.account_id: row.member_id for row in user_mapping}
        return request(client, "post", f"{root}/runs", data=data)
