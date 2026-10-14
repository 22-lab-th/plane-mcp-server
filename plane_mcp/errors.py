"""Preserve API recovery details while setting the MCP error flag."""

from __future__ import annotations

import json
from typing import Any

import requests
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolResult
from plane.errors import HttpError


class ErrorResult(ToolResult):
    def to_mcp_result(self) -> CallToolResult:
        return CallToolResult(content=self.content, structuredContent=self.structured_content, isError=True)


def failure(payload: dict[str, Any]) -> ErrorResult:
    return ErrorResult(content=json.dumps(payload, default=str), structured_content=payload)


def api_failure(error: HttpError) -> dict[str, Any]:
    payload = error.response if isinstance(error.response, dict) else {}
    return {
        **payload,
        "error": payload.get("error") or payload.get("detail") or "Plane API request failed.",
        "status_code": error.status_code,
    }


class NormalizeToolErrors(Middleware):
    async def on_call_tool(self, context, call_next):
        try:
            result = await call_next(context)
        except HttpError as exc:
            return failure(api_failure(exc))
        except requests.RequestException as exc:
            return failure(
                {
                    "error": "Plane connection failed. Read the target before retrying a write.",
                    "error_type": type(exc).__name__,
                }
            )
        except Exception as exc:
            # FastMCP may wrap SDK errors inside a ToolError before middleware sees them.
            cause = exc.__cause__
            while cause is not None:
                if isinstance(cause, HttpError):
                    return failure(api_failure(cause))
                cause = cause.__cause__
            raise
        payload = result.structured_content
        if isinstance(payload, dict) and set(payload) == {"result"}:
            payload = payload["result"]
        if isinstance(payload, dict) and payload.get("error"):
            return failure(payload)
        for block in result.content:
            if block.type == "text" and block.text.startswith("Error:"):
                return failure({"error": block.text.removeprefix("Error:").strip()})
        return result
