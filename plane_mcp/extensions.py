"""Requests for 22lab public API features not yet present in plane-sdk."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from plane.api.base_resource import BaseResource
from plane.errors import HttpError

from plane_mcp.errors import api_failure


def segment(value: str) -> str:
    return quote(str(value), safe="")


def project_path(workspace: str, project: str, resource: str) -> str:
    return f"{segment(workspace)}/projects/{segment(project)}/{resource}"


def request(client, method: str, endpoint: str, *, data=None, params=None, instance: bool = False) -> Any:
    resource = BaseResource(client.pages.config, "/instance") if instance else client.pages
    operation = getattr(resource, f"_{method}")
    kwargs = {}
    if data is not None:
        kwargs["data"] = data
    if params:
        kwargs["params"] = params
    try:
        return operation(endpoint, **kwargs)
    except HttpError as exc:
        # Preserve job conflicts, partial progress and quota/permission reasons.
        # Do not retry a POST: the response may describe an already-created run.
        return api_failure(exc)


def present(**values) -> dict:
    """Omit absent values while retaining false, zero and explicit empty strings."""
    return {name: value for name, value in values.items() if value is not None}
