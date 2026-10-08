"""Regression cases for the outbound URL guard (security review H1/H2/M3/M4)."""

from __future__ import annotations

import json
import logging
import socket

import pytest
import requests

from plane_mcp import safe_http
from plane_mcp.safe_http import BlockedDestinationError

PUBLIC = "93.184.216.34"


def _answers(*ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


@pytest.fixture
def dns(monkeypatch):
    """Map hostnames to a queue of answer lists; each lookup pops the next list."""
    table: dict[str, list[list[str]]] = {}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        queue = table.get(host)
        if not queue:
            raise socket.gaierror("no such host")
        ips = queue.pop(0) if len(queue) > 1 else queue[0]
        return _answers(*ips)

    monkeypatch.setattr(safe_http.socket, "getaddrinfo", fake_getaddrinfo)
    return table


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
        "100.64.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "::1",
        "fe80::1",
        "fc00::1",
        "240.0.0.1",
        "224.0.0.1",
        "2002:7f00:1::1",
    ],
)
def test_non_global_addresses_are_blocked(ip):
    assert safe_http.is_blocked_address(ip)


def test_public_address_is_allowed():
    assert not safe_http.is_blocked_address(PUBLIC)


def test_mixed_public_and_private_answers_are_rejected(dns):
    dns["mixed.test"] = [[PUBLIC, "127.0.0.1"]]
    with pytest.raises(BlockedDestinationError):
        safe_http.check_url("https://mixed.test/x")


def test_mapped_loopback_literal_url_is_rejected():
    with pytest.raises(BlockedDestinationError):
        safe_http.check_url("http://[::ffff:127.0.0.1]/x")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "https://user:pw@example.com/x"])
def test_bad_scheme_or_userinfo_is_rejected(url, dns):
    dns["example.com"] = [[PUBLIC]]
    with pytest.raises(BlockedDestinationError):
        safe_http.check_url(url)


def test_allowlist_rejects_unlisted_origin(dns, monkeypatch):
    monkeypatch.setenv(safe_http.ALLOWED_ORIGINS_ENV, "https://plane.locable.dev")
    dns["plane.locable.dev"] = [[PUBLIC]]
    dns["example.com"] = [[PUBLIC]]
    safe_http.check_url("https://plane.locable.dev/uploads/a?sig=1")
    for url in ("https://example.com/a", "http://plane.locable.dev/a", "https://plane.locable.dev:8443/a"):
        with pytest.raises(BlockedDestinationError):
            safe_http.check_url(url)


def test_resolution_change_between_check_and_connect_is_blocked(dns, monkeypatch):
    """DNS rebinding: the URL check sees a public answer, the connection sees loopback."""
    dns["rebind.test"] = [[PUBLIC], ["127.0.0.1"]]
    safe_http.check_url("https://rebind.test/x")  # first lookup: public

    connects = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: connects.append(addr))
    conn = safe_http.GuardedHTTPSConnection("rebind.test", 443)
    with pytest.raises(BlockedDestinationError):
        conn._new_conn()  # second lookup: loopback
    assert connects == []


def test_connection_connects_to_the_validated_address(dns, monkeypatch):
    dns["ok.test"] = [[PUBLIC]]
    connects = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: connects.append(addr))
    sock = safe_http.GuardedHTTPConnection("ok.test", 80)._new_conn()
    sock.close()
    assert connects == [(PUBLIC, 443)]


class _Resp:
    def __init__(self, status, location=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.is_redirect = location is not None
        self.url = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def close(self):
        pass


def test_redirect_to_loopback_is_not_followed(dns, monkeypatch):
    dns["public.test"] = [[PUBLIC]]
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append(url)
        assert kwargs["allow_redirects"] is False
        return _Resp(302, "http://127.0.0.1:8211/internal")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    with pytest.raises(BlockedDestinationError):
        safe_http.request("GET", "https://public.test/start")
    assert calls == ["https://public.test/start"]


@pytest.mark.parametrize("target", ["http://10.0.0.5/", "http://169.254.169.254/latest/meta-data/"])
def test_redirect_to_private_ranges_is_not_followed(dns, monkeypatch, target):
    dns["public.test"] = [[PUBLIC]]
    monkeypatch.setattr(requests.Session, "request", lambda self, m, u, **k: _Resp(301, target))
    with pytest.raises(BlockedDestinationError):
        safe_http.request("GET", "https://public.test/start")


def test_redirect_limit(dns, monkeypatch):
    dns["public.test"] = [[PUBLIC]]
    monkeypatch.setattr(requests.Session, "request", lambda self, m, u, **k: _Resp(302, "/again"))
    with pytest.raises(ValueError, match="Too many redirects"):
        safe_http.request("GET", "https://public.test/start")


def test_post_does_not_follow_redirects(dns, monkeypatch):
    dns["public.test"] = [[PUBLIC]]
    monkeypatch.setattr(requests.Session, "request", lambda self, m, u, **k: _Resp(307, "https://public.test/b"))
    with pytest.raises(ValueError, match="Unexpected redirect"):
        safe_http.request("POST", "https://public.test/a")


def test_request_errors_do_not_leak_signed_urls(dns, monkeypatch):
    dns["store.test"] = [[PUBLIC]]
    signed = "https://store.test/obj?X-Amz-Signature=SECRETSIG"

    def boom(self, method, url, **kwargs):
        raise requests.ConnectionError(f"failed for url: {url}")

    monkeypatch.setattr(requests.Session, "request", boom)
    with pytest.raises(ValueError) as info:
        safe_http.request("GET", signed)
    assert "SECRETSIG" not in str(info.value)
    assert "SECRETSIG" not in repr(info.value.__cause__) and info.value.__suppress_context__

    monkeypatch.setattr(requests.Session, "request", lambda self, m, u, **k: _Resp(403))
    with pytest.raises(ValueError) as info:
        safe_http.request("GET", signed)
    assert "SECRETSIG" not in str(info.value)
    assert "403" in str(info.value)


def test_redact_urls_strips_query_and_userinfo():
    text = "GET https://u:p@store.test/a/b?X-Amz-Signature=SECRET&x=1 failed; see http://h/x?token=T"
    out = safe_http.redact_urls(text)
    assert "SECRET" not in out and "u:p@" not in out and "token=T" not in out
    assert "https://store.test/a/b" in out and "http://h/x" in out


def test_json_log_formatter_redacts_signed_urls():
    from plane_mcp.__main__ import JSONFormatter

    try:
        raise ValueError("fetch failed https://store.test/o?X-Amz-Signature=SECRETSIG")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "msg https://s.test/a?sig=SECRET2", None, sys.exc_info()
        )
    line = JSONFormatter().format(record)
    assert "SECRETSIG" not in line and "SECRET2" not in line
    json.loads(line)
