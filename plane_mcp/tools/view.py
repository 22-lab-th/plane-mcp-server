"""Saved project views and their work-item filters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from fastmcp import FastMCP
from plane.models.pagination import PaginatedResponse
from plane.models.query_params import PaginatedQueryParams
from pydantic import BaseModel, ConfigDict

from plane_mcp.client import get_plane_client_context
from plane_mcp.toolkit import Action, as_params, build_annotations, build_description, envelope, missing

NAME = "view"
TITLE = "Views"

ACTIONS = (
    Action("list", ("project_id",), ("cursor", "per_page"), read=True),
    Action("retrieve", ("project_id", "view_id"), read=True),
    Action(
        "create",
        ("project_id", "name"),
        (
            "description",
            "filters",
            "display_filters",
            "display_properties",
            "rich_filters",
            "logo_props",
            "sort_order",
        ),
    ),
    Action(
        "update",
        ("project_id", "view_id"),
        (
            "name",
            "description",
            "filters",
            "display_filters",
            "display_properties",
            "rich_filters",
            "logo_props",
            "sort_order",
        ),
        note="only the fields you pass are changed",
    ),
    Action("delete", ("project_id", "view_id"), destructive=True),
)

FOOTER = (
    "filters, display_filters, display_properties, rich_filters and logo_props are JSON objects. "
    'Example: filters=\'{"priority":["urgent"]}\'. A view is visible to its owner and project members.'
)

LEGACY: dict[str, str] = {}


class View(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str | None = None
    name: str | None = None
    description: str | None = None
    query: dict[str, Any] | None = None
    filters: dict[str, Any] | None = None
    display_filters: dict[str, Any] | None = None
    display_properties: dict[str, Any] | None = None
    rich_filters: dict[str, Any] | None = None
    logo_props: dict[str, Any] | None = None
    sort_order: float | None = None
    archived_at: str | None = None
    owned_by: str | None = None
    project: str | None = None
    workspace: str | None = None


class PaginatedViewResponse(PaginatedResponse):
    results: list[View]


def _json_object(field: str, raw: str) -> dict[str, Any] | None | str:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return f"Error: {field} must be a valid JSON object"
    if not isinstance(value, dict):
        return f"Error: {field} must be a JSON object"
    return value


def _payload(**values: Any) -> tuple[dict[str, Any], str | None]:
    payload: dict[str, Any] = {}
    json_fields = {"filters", "display_filters", "display_properties", "rich_filters", "logo_props"}
    for field, value in values.items():
        if field in json_fields:
            parsed = _json_object(field, value)
            if isinstance(parsed, str):
                return {}, parsed
            if parsed is not None:
                payload[field] = parsed
        elif value is not None:
            payload[field] = value
    return payload, None


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description("Saved project views and their work-item filters.", ACTIONS, FOOTER),
        annotations=build_annotations(TITLE, ACTIONS),
    )
    def view(
        action: Literal["list", "retrieve", "create", "update", "delete"],
        project_id: str = "",
        view_id: str = "",
        name: str | None = None,
        description: str | None = None,
        filters: str = "",
        display_filters: str = "",
        display_properties: str = "",
        rich_filters: str = "",
        logo_props: str = "",
        sort_order: float | None = None,
        cursor: str = "",
        per_page: int = 0,
    ) -> View | dict[str, Any] | str | None:
        client, workspace_slug = get_plane_client_context()
        if not project_id:
            return missing(action, "project_id")
        endpoint = f"{workspace_slug}/projects/{project_id}/views"

        if action == "list":
            params = as_params(PaginatedQueryParams, cursor=cursor, per_page=per_page)
            response = client.projects._get(
                endpoint,
                params=params.model_dump(exclude_none=True) if params else None,
            )
            page = PaginatedViewResponse.model_validate(response) if isinstance(response, Mapping) else response
            return envelope(page)

        if not view_id and action in ("retrieve", "update", "delete"):
            return missing(action, "view_id")
        detail_endpoint = f"{endpoint}/{view_id}" if view_id else endpoint

        if action == "retrieve":
            return View.model_validate(client.projects._get(detail_endpoint))

        payload, error = _payload(
            name=name,
            description=description,
            filters=filters,
            display_filters=display_filters,
            display_properties=display_properties,
            rich_filters=rich_filters,
            logo_props=logo_props,
            sort_order=sort_order,
        )
        if error:
            return error

        if action == "create":
            if not name:
                return missing(action, "name")
            return View.model_validate(client.projects._post(endpoint, payload))

        if action == "update":
            if not payload:
                return "Error: update requires at least one field to change"
            return View.model_validate(client.projects._patch(detail_endpoint, payload))

        client.projects._delete(detail_endpoint)
        return None
