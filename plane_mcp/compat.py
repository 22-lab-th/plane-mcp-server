"""Compatibility helpers for self-hosted Plane deployments."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_plane_base_url(raw_url: str) -> str:
    """Return the Plane origin expected by ``plane-sdk``.

    ``plane-sdk`` appends ``/api/v1`` itself. Self-hosted operators commonly
    copy an API URL ending in ``/api`` or ``/api/v1`` from a reverse proxy or
    deployment guide; passing either through unchanged produces a duplicated
    path. Accept all three forms and reduce them to the instance origin.
    """
    value = raw_url.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("PLANE_BASE_URL must be an absolute http(s) URL")

    path = parsed.path.rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)]
            break

    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")
