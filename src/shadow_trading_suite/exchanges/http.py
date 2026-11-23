"""The single network chokepoint: HTTPS GET with no credentials, redirects or proxies.

This is the only module in the package allowed to import networking libraries
(enforced by tests/safety/test_suite_boundaries.py).
"""

import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import Message
from typing import Any, Protocol
from urllib.parse import urlsplit

from shadow_trading_suite.exchanges.errors import (
    ResponseFormatError,
    TransportError,
    TransportPolicyError,
)

ALLOWED_REQUEST_HEADERS = frozenset({"accept", "user-agent"})
# HTTP validators a server sent us may be echoed back, and only on a request explicitly
# marked `conditional`. They carry no credential and no client identity.
CONDITIONAL_REQUEST_HEADERS = frozenset({"if-none-match", "if-modified-since"})
MAX_VALIDATOR_LEN = 256
MAX_TIMEOUT_S = 60.0
DEFAULT_MAX_BODY_BYTES = 16 * 1024 * 1024


def valid_validator(value: str) -> bool:
    """A validator we are willing to echo: short, printable ASCII, no control characters."""
    return 0 < len(value) <= MAX_VALIDATOR_LEN and all(" " <= ch <= "~" for ch in value)


@dataclass(frozen=True)
class HttpRequest:
    """An outbound request. Construction validates the transport policy."""

    url: str
    headers: Mapping[str, str]
    timeout_s: float
    conditional: bool = False  # may carry CONDITIONAL_REQUEST_HEADERS (news feeds only)

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme != "https":
            raise TransportPolicyError(f"only https is allowed, got {parts.scheme!r}")
        if not parts.hostname:
            raise TransportPolicyError("url has no host")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise TransportPolicyError("credentials in url are not allowed")
        if parts.fragment:
            raise TransportPolicyError("url fragments are not allowed")
        for name, value in self.headers.items():
            if "\r" in value or "\n" in value:
                raise TransportPolicyError(f"request header {name!r} contains a line break")
            if name.lower() in CONDITIONAL_REQUEST_HEADERS:
                if not self.conditional:
                    raise TransportPolicyError(f"request header {name!r} is not allowed")
                if not valid_validator(value):
                    raise TransportPolicyError(f"request header {name!r} has an unsafe value")
            elif name.lower() not in ALLOWED_REQUEST_HEADERS:
                raise TransportPolicyError(f"request header {name!r} is not allowed")
        if not 0 < self.timeout_s <= MAX_TIMEOUT_S:
            raise TransportPolicyError(f"timeout must be in (0, {MAX_TIMEOUT_S}] seconds")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-cased names
    body: bytes = b""


class Fetcher(Protocol):
    def get(self, request: HttpRequest) -> HttpResponse: ...


class Opener(Protocol):
    def open(self, req: urllib.request.Request, timeout: float) -> Any: ...


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a 3xx surfaces as an error response instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def build_default_opener() -> urllib.request.OpenerDirector:
    """Verified TLS, environment proxies ignored, redirects refused."""
    context = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _RefuseRedirects(),
    )


def _lower_headers(headers: Message | None) -> dict[str, str]:
    if headers is None:
        return {}
    return {k.lower(): v for k, v in headers.items()}


def _read_limited(stream: Any, limit: int) -> bytes:
    body: bytes = stream.read(limit + 1)
    if len(body) > limit:
        raise ResponseFormatError(f"response body exceeds {limit} bytes")
    return body


class UrllibFetcher:
    def __init__(
        self, opener: Opener | None = None, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    ) -> None:
        self._opener = opener or build_default_opener()
        self._max_body_bytes = max_body_bytes

    def get(self, request: HttpRequest) -> HttpResponse:
        # S310: scheme is validated as https by HttpRequest; no file:/custom schemes possible.
        req = urllib.request.Request(  # noqa: S310
            request.url, headers=dict(request.headers), method="GET"
        )
        try:
            with self._opener.open(req, timeout=request.timeout_s) as resp:
                return HttpResponse(
                    status=resp.status,
                    headers=_lower_headers(resp.headers),
                    body=_read_limited(resp, self._max_body_bytes),
                )
        except urllib.error.HTTPError as exc:
            with exc:  # release the underlying connection
                body = _read_limited(exc, self._max_body_bytes) if exc.fp is not None else b""
                return HttpResponse(status=exc.code, headers=_lower_headers(exc.headers), body=body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc
