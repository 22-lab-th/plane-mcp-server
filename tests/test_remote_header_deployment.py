"""Coverage for shared PAT/header-only deployments."""

import pytest

from plane_mcp.__main__ import oauth_enabled
from plane_mcp.auth import PlaneHeaderAuthProvider


@pytest.mark.parametrize(
    ("setting", "client_id", "client_secret", "expected"),
    [
        ("false", "id", "secret", False),
        ("true", "id", "secret", True),
        ("auto", "id", "secret", True),
        ("auto", "", "", False),
    ],
)
def test_oauth_enabled_modes(monkeypatch, setting, client_id, client_secret, expected):
    monkeypatch.setenv("PLANE_OAUTH_ENABLED", setting)
    monkeypatch.setenv("PLANE_OAUTH_PROVIDER_CLIENT_ID", client_id)
    monkeypatch.setenv("PLANE_OAUTH_PROVIDER_CLIENT_SECRET", client_secret)
    assert oauth_enabled() is expected


def test_oauth_enabled_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("PLANE_OAUTH_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="auto, true, false"):
        oauth_enabled()


def test_workspace_allowlist(monkeypatch):
    monkeypatch.setenv("PLANE_ALLOWED_WORKSPACE_SLUGS", "projects, another-workspace")
    assert PlaneHeaderAuthProvider._workspace_allowed("projects")
    assert not PlaneHeaderAuthProvider._workspace_allowed("other")


def test_empty_workspace_allowlist_allows_any_workspace(monkeypatch):
    monkeypatch.delenv("PLANE_ALLOWED_WORKSPACE_SLUGS", raising=False)
    assert PlaneHeaderAuthProvider._workspace_allowed("projects")
