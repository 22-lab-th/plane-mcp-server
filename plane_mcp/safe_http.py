"""Outbound HTTP for URLs that did not come from the Plane SDK itself.

Covers model-supplied URLs (upload from URL) and URLs returned by the Plane API
(signed download/upload URLs). Three layers:

* The connection layer resolves the host, rejects the connection if ANY answer is
  non-global (loopback, RFC 1918, link-local, shared, unspecified, IPv4-mapped
  equivalents, ...), and connects to the validated address. TLS still verifies
  against the original hostname, and nothing is re-resolved between the check and
  the connect.
* Redirects are never followed automatically. GET follows at most
  ``MAX_REDIRECTS`` hops, and each hop is checked again. Other methods do not follow
  redirects.
* ``PLANE_URL_FETCH_ALLOWED_ORIGINS`` (comma-separated ``scheme://host[:port]``)
  optionally limits every request and hop to listed origins. Unset = any public URL.

Errors never echo the URL or the underlying exception text, because signed URLs
carry credentials in the query string.
"""

import ipaddress
import os
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.util.timeout import _DEFAULT_TIMEOUT

MAX_REDIRECTS = 5
ALLOWED_ORIGINS_ENV = "PLANE_URL_FETCH_ALLOWED_ORIGINS"
_DEFAULT_PORTS = {"http": 80, "https": 443}


class BlockedDestinationError(ValueError):
    """Raised when a URL or connection target is not allowed."""


def is_blocked_address(value: str) -> bool:
    """Return True for any address that is not a globally routable unicast address."""
    addr = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address):
        for embedded in (addr.ipv4_mapped, addr.sixtofour):
            if embedded is not None and is_blocked_address(str(embedded)):
                return True
        if addr.teredo is not None:
            return True
    return not addr.is_global or addr.is_multicast


def resolve_public(host: str, port: int) -> list[tuple[Any, ...]]:
    """Resolve host and return getaddrinfo answers; raise if any answer is blocked."""
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedDestinationError(f"Could not resolve host {host!r}") from exc
    if not answers:
        raise BlockedDestinationError(f"Could not resolve host {host!r}")
    for answer in answers:
        if is_blocked_address(str(answer[4][0])):
            raise BlockedDestinationError(
                f"Host {host!r} resolves to a private or reserved address and cannot be fetched"
            )
    return answers


def _origin(url: str) -> str:
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    port = parts.port or _DEFAULT_PORTS.get(scheme)
    return f"{scheme}://{host}:{port}"


def allowed_origins() -> set[str] | None:
    raw = os.getenv(ALLOWED_ORIGINS_ENV, "").strip()
    if not raw:
        return None
    return {_origin(item.strip()) for item in raw.split(",") if item.strip()}


def check_url(url: str) -> None:
    """URL-level checks: scheme, no userinfo, origin allowlist, early DNS check."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in _DEFAULT_PORTS:
        raise BlockedDestinationError("Only http and https URLs can be fetched")
    if not parts.hostname:
        raise BlockedDestinationError("URL has no hostname")
    if parts.username or parts.password:
        raise BlockedDestinationError("URLs with embedded credentials are not allowed")
    origins = allowed_origins()
    if origins is not None and _origin(url) not in origins:
        raise BlockedDestinationError("URL origin is not in the allowed list for this installation")
    resolve_public(parts.hostname, parts.port or _DEFAULT_PORTS[parts.scheme.lower()])


class _GuardedConnectionMixin:
    def _new_conn(self):  # type: ignore[override]
        answers = resolve_public(self._dns_host, self.port)
        last_error: OSError | None = None
        for family, socktype, proto, _canon, sockaddr in answers:
            sock = socket.socket(family, socktype, proto)
            try:
                if self.timeout is not _DEFAULT_TIMEOUT:
                    sock.settimeout(self.timeout)
                for opt in self.socket_options or []:
                    sock.setsockopt(*opt)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                sock.close()
                last_error = exc
        raise NewConnectionError(self, f"Failed to establish a new connection: {type(last_error).__name__}")


class GuardedHTTPConnection(_GuardedConnectionMixin, HTTPConnection):
    pass


class GuardedHTTPSConnection(_GuardedConnectionMixin, HTTPSConnection):
    pass


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = GuardedHTTPSConnection


class GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _GuardedHTTPPool, "https": _GuardedHTTPSPool}


def guarded_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False  # no proxy/netrc from the environment
    adapter = GuardedAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def request(method: str, url: str, **kwargs) -> requests.Response:
    """Send a request with URL, redirect and connection guards. Raises ValueError on failure."""
    kwargs["allow_redirects"] = False
    current = url
    with guarded_session() as session:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(current)
            try:
                response = session.request(method, current, **kwargs)
            except requests.RequestException as exc:
                raise ValueError(describe_error(exc)) from None
            if response.is_redirect:
                if method.upper() != "GET":
                    raise ValueError(f"Unexpected redirect (HTTP {response.status_code}) for {method.upper()}")
                current = urljoin(current, response.headers["Location"])
                response.close()
                continue
            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                raise ValueError(describe_error(exc)) from None
            return response
    raise ValueError("Too many redirects")


def describe_error(exc: BaseException) -> str:
    """Safe description of a request failure: exception type and HTTP status only."""
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, BlockedDestinationError):
            return str(cause)
        cause = cause.__cause__ or cause.__context__
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")


_URL_SECRET = re.compile(r"(?P<scheme>https?://)(?:[^/\s@]*@)?(?P<rest>[^\s?#'\"]*)(?:\?[^\s#'\"]*)?", re.I)


def redact_urls(text: str) -> str:
    """Strip userinfo and query strings from any URL inside text."""
    return _URL_SECRET.sub(lambda m: m.group("scheme") + m.group("rest"), text)
