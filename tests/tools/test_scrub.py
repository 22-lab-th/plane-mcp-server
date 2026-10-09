"""Blanket rule: no URL query string or userinfo leaves a tool call or a log line.

Exceptions: the successful result of an action whose purpose is to return a
signed link (SIGNED_LINK_ACTIONS). Errors are scrubbed for every action.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from plane.errors.errors import HttpError

from plane_mcp.middleware import ScrubSignedUrls
from plane_mcp.server import get_stdio_mcp
from plane_mcp.tools import workitem_attachment as attachment_module

SIGNED = "https://plane.example/uploads/obj?X-Amz-Signature=SECRETSIG&X-Amz-Credential=SECRETCRED"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
UPLOAD_ARGS = {
    "action": "upload_from_url",
    "project_id": "project-1",
    "workitem_id": "work-item-1",
    "url": "https://example.com/diagram.png",
}


def _call(mcp: FastMCP, name: str, arguments: dict):
    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(name, arguments, raise_on_error=False)

    return asyncio.run(run())


def _text(result) -> str:
    parts = [getattr(block, "text", "") for block in result.content]
    if result.structured_content is not None:
        parts.append(json.dumps(result.structured_content))
    return "\n".join(parts)


class _Response:
    def __init__(self, content=PNG, headers=None):
        self.content = content
        self.headers = headers or {"Content-Type": "image/png"}


@pytest.fixture
def server(spy, monkeypatch):
    """The real stdio server stack with the Plane client and outbound HTTP stubbed."""
    spy.returns["work_items.attachments._post"] = {
        "asset_id": "asset-1",
        "upload_data": {"url": SIGNED, "fields": {}},
        "attachment": {"id": "attachment-1", "asset": "asset-1"},
    }
    monkeypatch.setattr(attachment_module.safe_http, "request", lambda *a, **k: _Response())
    return get_stdio_mcp()


def _signed_http_error() -> HttpError:
    return HttpError("Bad request", 400, {"detail": f"upload data was {SIGNED}", "url": SIGNED})


def test_sdk_create_error_body_does_not_leak(server, spy):
    spy.returns["work_items.attachments._post"] = _signed_http_error()
    result = _call(server, "workitem_attachment", UPLOAD_ARGS)
    assert result.is_error
    assert "SECRETSIG" not in _text(result) and "SECRETCRED" not in _text(result)
    assert "400" in _text(result)


def test_sdk_mark_uploaded_error_body_does_not_leak(server, spy):
    spy.returns["work_items.attachments._patch"] = _signed_http_error()
    result = _call(server, "workitem_attachment", UPLOAD_ARGS)
    assert result.is_error
    assert "SECRETSIG" not in _text(result)
    assert [m.rsplit(".", 1)[-1] for m in spy.recorder.methods] == ["_post", "_patch"]


def test_storage_failure_with_failing_cleanup_does_not_leak(server, spy, monkeypatch):
    def fake_request(method, url, **kwargs):
        if method == "GET":
            return _Response()
        raise ValueError(f"storage said no: {url}")  # worst case: a raw URL in the text

    monkeypatch.setattr(attachment_module.safe_http, "request", fake_request)
    spy.returns["work_items.attachments._delete"] = _signed_http_error()
    with pytest.raises(ValueError):
        # A failed cleanup is swallowed; the storage failure is what surfaces...
        attachment_module._store(spy, "acme", "p", "w", PNG, "a.png", "image/png")
    result = _call(server, "workitem_attachment", UPLOAD_ARGS)
    # ...and even raw URL text in it is scrubbed at the middleware boundary.
    assert result.is_error
    assert "SECRETSIG" not in _text(result)


def test_direct_upload_boundary_reports_status_only(spy, monkeypatch):
    monkeypatch.setattr(attachment_module.safe_http, "request", lambda *a, **k: _Response())
    spy.returns["work_items.attachments._post"] = _signed_http_error()
    with pytest.raises(ValueError) as info:
        attachment_module._upload(spy, "acme", "p", "w", "https://example.com/a.png", "")
    assert str(info.value) == "Failed to upload attachment: HTTP 400"
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_signed_link_action_result_is_kept(server, spy):
    spy.returns["work_items.attachments.get_download_url"] = SIGNED
    spy.returns["work_items.attachments.list"] = [SimpleNamespace(id="a", attributes={"name": "r.pdf"})]
    result = _call(
        server,
        "workitem_attachment",
        {"action": "download_url", "project_id": "p", "workitem_id": "w", "attachment_id": "a"},
    )
    assert not result.is_error
    assert "SECRETSIG" in _text(result)  # the link is the deliverable


# A minimal stack: the middleware alone, around tools that misbehave on purpose.
def _toy() -> FastMCP:
    mcp = FastMCP("toy")
    mcp.add_middleware(ScrubSignedUrls())

    @mcp.tool
    def leaky(action: str = "x") -> dict:
        return {"note": f"see {SIGNED}", SIGNED: "as a key", "nested": [{"k": SIGNED}]}

    @mcp.tool
    def boom(action: str = "x") -> str:
        raise RuntimeError("failed at https://user:pw@plane.example/a?token=SECRETSIG")

    @mcp.tool
    def page(action: str = "download_asset_url") -> str:
        if action == "download_asset_url":
            return SIGNED
        raise RuntimeError(SIGNED)

    return mcp


def test_results_are_scrubbed_in_keys_values_and_text():
    result = _call(_toy(), "leaky", {})
    assert not result.is_error
    text = _text(result)
    assert "SECRETSIG" not in text and "SECRETCRED" not in text
    assert "https://plane.example/uploads/obj" in text


def test_errors_are_scrubbed_including_userinfo():
    result = _call(_toy(), "boom", {})
    assert result.is_error
    assert "SECRETSIG" not in _text(result) and "user:pw" not in _text(result)


def test_exempt_action_keeps_link_but_its_errors_are_scrubbed():
    toy = _toy()
    assert "SECRETSIG" in _text(_call(toy, "page", {"action": "download_asset_url"}))
    failed = _call(toy, "page", {"action": "other"})
    assert failed.is_error and "SECRETSIG" not in _text(failed)


@pytest.mark.parametrize(
    "payload",
    [
        {SIGNED: "failed"},
        {"outer": {SIGNED: "failed", "safe": 1}},
        {SIGNED: "a", "https://plane.example/uploads/obj?other=SECRETSIG": "b"},
    ],
)
def test_log_formatter_scrubs_signed_urls_in_keys(payload):
    from plane_mcp.__main__ import JSONFormatter

    record = logging.LogRecord("t", logging.INFO, __file__, 1, json.dumps(payload), None, None)
    line = JSONFormatter().format(record)
    assert "SECRETSIG" not in line and "SECRETCRED" not in line
    parsed = json.loads(line)
    if "outer" in payload:
        assert parsed["outer"]["safe"] == 1
    if len(payload) == 2:
        # Colliding redacted keys are both kept, neither overwrites the other.
        assert {"https://plane.example/uploads/obj", "https://plane.example/uploads/obj#2"} <= set(parsed)
