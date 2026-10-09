"""Attachment helpers shared by both tool surfaces.

Network limits, MIME allow-lists, the SSRF guard and the attachment
normaliser. Kept out of either surface package so neither depends on the other.
"""

from typing import Any

from plane_mcp.safe_http import check_url

# ── Limits ────────────────────────────────────────────────────────────────────
IMAGE_READ_LIMIT = 5 * 1024 * 1024  # 5 MB
TEXT_READ_LIMIT = 1 * 1024 * 1024  # 1 MB
UPLOAD_SIZE_LIMIT = 5 * 1024 * 1024  # 5 MB

# Connect timeout / read timeout tuple used for all outbound HTTP calls.
HTTP_TIMEOUT = (10, 60)

# ── Supported MIME types ──────────────────────────────────────────────────────
READABLE_IMAGE_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
READABLE_TEXT_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "text/xml",
        "text/yaml",
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
    }
)


def assert_public_url(url: str) -> None:
    """Raise ValueError unless the URL is an allowed public http(s) URL (see safe_http)."""
    check_url(url)


def attachment_to_dict(attachment: Any, workspace_slug: str) -> dict[str, Any]:
    data = attachment.model_dump()
    attrs = data.get("attributes") or {}
    data["name"] = attrs.get("name")
    data["size"] = attrs.get("size") or data.get("size")
    data["content_type"] = attrs.get("type")
    return data
