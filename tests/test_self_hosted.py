"""Self-hosted Plane compatibility behavior."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from plane.errors import HttpError

from plane_mcp.compat import normalize_plane_base_url
from plane_mcp.doctor import diagnose_connection
from plane_mcp.tools.page import _page_api_error


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("https://plane.22lab.dev", "https://plane.22lab.dev"),
        ("https://plane.22lab.dev/", "https://plane.22lab.dev"),
        ("https://plane.22lab.dev/api", "https://plane.22lab.dev"),
        ("https://plane.22lab.dev/api/v1/", "https://plane.22lab.dev"),
        ("https://example.test/plane/api/v1", "https://example.test/plane"),
    ],
)
def test_normalize_plane_base_url(configured, expected):
    assert normalize_plane_base_url(configured) == expected


@pytest.mark.parametrize("configured", ["", "plane.22lab.dev", "ftp://plane.22lab.dev"])
def test_normalize_plane_base_url_rejects_non_http_urls(configured):
    with pytest.raises(ValueError, match="absolute http"):
        normalize_plane_base_url(configured)


@dataclass
class _Response:
    status_code: int


class _Session:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.calls = []

    def get(self, url, *, headers, timeout):
        self.calls.append((url, headers, timeout))
        return _Response(next(self.statuses))


def test_doctor_distinguishes_core_api_from_optional_pages_api():
    session = _Session([200, 404])
    report = diagnose_connection(
        base_url="https://plane.22lab.dev/api/v1",
        workspace_slug="acme",
        api_key="secret",
        session=session,
    )

    assert report["connected"] is True
    assert report["core_api"] == {"ok": True, "status": 200}
    assert report["pages_api"] == {"ok": False, "status": 404, "supported": False}
    assert all("secret" not in str(value) for value in report.values())
    assert session.calls[0][0] == "https://plane.22lab.dev/api/v1/workspaces/acme/projects/"


def test_page_404_explains_the_self_hosted_api_gap():
    message = _page_api_error(HttpError("not found", 404, {"detail": "Page not found."}))
    assert message.startswith("Error:")
    assert "API-token route" in message
    assert "browser cookie" in message


def test_page_non_404_is_not_hidden():
    error = HttpError("forbidden", 403)
    with pytest.raises(HttpError) as raised:
        _page_api_error(error)
    assert raised.value is error
