"""Project execution automations available in Plane Community Edition."""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP
from plane.models.projects import Project

from plane_mcp.client import get_plane_client_context
from plane_mcp.toolkit import Action, build_annotations, build_description, missing

NAME = "automation"
TITLE = "Automations"

ACTIONS = (
    Action("get", ("project_id",), read=True),
    Action(
        "update",
        ("project_id",),
        ("archive_in", "close_in", "default_state"),
        note="months range from 0 (disabled) to 12; enabling auto-close requires default_state",
    ),
)

FOOTER = (
    "Plane Community Edition provides auto-archive and auto-close. archive_in and close_in are month counts; "
    "0 disables that automation. default_state is a cancelled-state id used by auto-close."
)

LEGACY: dict[str, str] = {}


def _result(project: Project) -> dict[str, Any]:
    return {
        "project_id": project.id,
        "archive_in": project.archive_in,
        "close_in": project.close_in,
        "default_state": project.default_state,
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description("Project execution automations.", ACTIONS, FOOTER),
        annotations=build_annotations(TITLE, ACTIONS),
    )
    def automation(
        action: Literal["get", "update"],
        project_id: str = "",
        archive_in: int | None = None,
        close_in: int | None = None,
        default_state: str = "",
    ) -> dict[str, Any] | str:
        client, workspace_slug = get_plane_client_context()
        if not project_id:
            return missing(action, "project_id")

        if action == "get":
            project = client.projects.retrieve(workspace_slug=workspace_slug, project_id=project_id)
            return _result(project)

        for field, value in (("archive_in", archive_in), ("close_in", close_in)):
            if value is not None and not 0 <= value <= 12:
                return f"Error: {field} must be between 0 and 12"
        if close_in is not None and close_in > 0 and not default_state:
            return "Error: enabling auto-close requires default_state"

        payload: dict[str, Any] = {}
        if archive_in is not None:
            payload["archive_in"] = archive_in
        if close_in is not None:
            payload["close_in"] = close_in
            if close_in == 0:
                payload["default_state"] = None
        if default_state:
            payload["default_state"] = default_state
        if not payload:
            return "Error: update requires archive_in, close_in or default_state"

        response = client.projects._patch(f"{workspace_slug}/projects/{project_id}", payload)
        return _result(Project.model_validate(response))
