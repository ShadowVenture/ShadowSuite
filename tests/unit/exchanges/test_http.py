import http.client
import io
import urllib.error
import urllib.request
from email.message import Message
from typing import Any, cast

import pytest

from shadow_trading_suite.core.errors import SafetyViolationError
from shadow_trading_suite.exchanges.errors import (
    ResponseFormatError,
    TransportError,
    TransportPolicyError,
)
from shadow_trading_suite.exchanges.http import (
    HttpRequest,
    HttpResponse,
    UrllibFetcher,
    build_default_opener,
)

URL = "https://api.example.com/v1/public/x?a=1"
OK_HEADERS = {"Accept": "application/json", "User-Agent": "test"}


class TestHttpRequestPolicy:
    def test_valid_request(self) -> None:
        req = HttpRequest(url=URL, headers=OK_HEADERS, timeout_s=5)
        assert req.url == URL

    def test_policy_errors_are_safety_violations(self) -> None:
        assert issubclass(TransportPolicyError, SafetyViolationError)

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.example.com/x",
            "ftp://api.example.com/x",
            "https://user:pw@api.example.com/x",
            "https://token@api.example.com/x",
            "https:///x",
            "https://api.example.com/x#frag",
        ],
    )
    def test_rejects_unsafe_urls(self, url: str) -> None:
        with pytest.raises(TransportPolicyError):
            HttpRequest(url=url, headers=OK_HEADERS, timeout_s=5)

    @pytest.mark.parametrize(
        "header",
        [
            "Authorization",
            "authorization",
            "Cookie",
            "X-API-KEY",
            "CB-ACCESS-KEY",
            "CB-ACCESS-SIGN",
            "Proxy-Authorization",
            "X-Forwarded-For",
        ],
    )
    def test_rejects_any_header_outside_allowlist(self, header: str) -> None:
        with pytest.raises(TransportPolicyError, match="header"):
            HttpRequest(url=URL, headers={**OK_HEADERS, header: "x"}, timeout_s=5)

    def test_rejects_header_injection(self) -> None:
        with pytest.raises(TransportPolicyError):
            HttpRequest(url=URL, headers={"User-Agent": "x\r\nAuthorization: y"}, timeout_s=5)

    @pytest.mark.parametrize("header", ["If-None-Match", "if-modified-since"])
    def test_conditional_headers_need_an_explicit_conditional_request(self, header: str) -> None:
        with pytest.raises(TransportPolicyError, match="not allowed"):
            HttpRequest(url=URL, headers={**OK_HEADERS, header: '"abc"'}, timeout_s=5)
        req = HttpRequest(
            url=URL, headers={**OK_HEADERS, header: '"abc"'}, timeout_s=5, conditional=True
        )
        assert req.conditional

    @pytest.mark.parametrize("header", ["Authorization", "Cookie", "X-API-KEY", "If-Match"])
    def test_a_conditional_request_still_refuses_every_other_header(self, header: str) -> None:
        with pytest.raises(TransportPolicyError, match="not allowed"):
            HttpRequest(url=URL, headers={**OK_HEADERS, header: "x"}, timeout_s=5, conditional=True)

    @pytest.mark.parametrize("value", ["", "x" * 257, "café", "a\tb", "a\x00b"])
    def test_conditional_header_values_are_validated(self, value: str) -> None:
        with pytest.raises(TransportPolicyError, match="unsafe value"):
            HttpRequest(url=URL, headers={"If-None-Match": value}, timeout_s=5, conditional=True)

    @pytest.mark.parametrize("timeout", [0, -1, 120])
    def test_timeout_must_be_bounded(self, timeout: float) -> None:
        with pytest.raises(TransportPolicyError, match="timeout"):
            HttpRequest(url=URL, headers=OK_HEADERS, timeout_s=timeout)


class _FakeResp(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None):
        super().__init__(body)
        self.status = status
        msg = Message()
        for k, v in (headers or {"Content-Type": "application/json"}).items():
            msg[k] = v
        self.headers = msg

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _FakeOpener:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.seen: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def open(self, req: urllib.request.Request, timeout: float) -> Any:
        self.seen.append(req)
        self.timeouts.append(timeout)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _req() -> HttpRequest:
    return HttpRequest(url=URL, headers=OK_HEADERS, timeout_s=7)


class TestUrllibFetcher:
    def test_success_returns_status_headers_body(self) -> None:
        opener = _FakeOpener(_FakeResp(b'{"a": 1}', headers={"Content-Type": "application/json"}))
        resp = UrllibFetcher(opener=opener).get(_req())
        assert resp == HttpResponse(
            status=200, headers={"content-type": "application/json"}, body=b'{"a": 1}'
        )

    def test_sends_get_with_only_given_headers_and_timeout(self) -> None:
        opener = _FakeOpener(_FakeResp(b"{}"))
        UrllibFetcher(opener=opener).get(_req())
        sent = opener.seen[0]
        assert sent.get_method() == "GET"
        assert sent.data is None
        assert {k.lower(): v for k, v in sent.header_items()} == {
            "accept": "application/json",
            "user-agent": "test",
        }
        assert opener.timeouts == [7]

    def test_http_error_becomes_response(self) -> None:
        hdrs = Message()
        hdrs["Retry-After"] = "3"
        err = urllib.error.HTTPError(URL, 429, "Too Many", hdrs, io.BytesIO(b"slow down"))
        resp = UrllibFetcher(opener=_FakeOpener(err)).get(_req())
        assert resp.status == 429
        assert resp.headers["retry-after"] == "3"
        assert resp.body == b"slow down"

    def test_http_error_response_is_closed(self) -> None:
        body = io.BytesIO(b"server error")
        err = urllib.error.HTTPError(URL, 503, "Unavailable", Message(), body)
        UrllibFetcher(opener=_FakeOpener(err)).get(_req())
        assert body.closed, "HTTPError response must be closed to release the connection"

    @pytest.mark.parametrize(
        "exc",
        [
            urllib.error.URLError("dns failure"),
            TimeoutError("timed out"),
            ConnectionResetError("reset"),
        ],
    )
    def test_network_failures_become_transport_error(self, exc: BaseException) -> None:
        with pytest.raises(TransportError):
            UrllibFetcher(opener=_FakeOpener(exc)).get(_req())

    def test_oversized_body_rejected(self) -> None:
        opener = _FakeOpener(_FakeResp(b"x" * 101))
        with pytest.raises(ResponseFormatError, match="exceeds"):
            UrllibFetcher(opener=opener, max_body_bytes=100).get(_req())


def _handlers(opener: urllib.request.OpenerDirector) -> list[Any]:
    # `handlers` exists at runtime but is not part of the typeshed stubs.
    return list(vars(opener)["handlers"])


class TestDefaultOpener:
    def test_ignores_environment_proxies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:8080")

        def env_proxies(opener: urllib.request.OpenerDirector) -> list[dict[str, str]]:
            return [
                vars(h)["proxies"]
                for h in _handlers(opener)
                if isinstance(h, urllib.request.ProxyHandler) and vars(h).get("proxies")
            ]

        # Control: urllib's default opener would route through the environment proxy.
        assert env_proxies(urllib.request.build_opener()) == [{"https": "http://evil.example:8080"}]
        assert env_proxies(build_default_opener()) == []

    def test_refuses_redirects(self) -> None:
        opener = build_default_opener()
        handlers = [
            h for h in _handlers(opener) if isinstance(h, urllib.request.HTTPRedirectHandler)
        ]
        assert handlers
        req = urllib.request.Request("https://api.example.com/a")
        for h in handlers:
            fp = cast(Any, None)
            hdrs = http.client.HTTPMessage()
            assert h.redirect_request(req, fp, 302, "Found", hdrs, "https://evil/") is None

    def test_tls_verification_enabled(self) -> None:
        import ssl

        opener = build_default_opener()
        https = [h for h in _handlers(opener) if isinstance(h, urllib.request.HTTPSHandler)]
        assert https
        for h in https:
            ctx: ssl.SSLContext = vars(h)["_context"]
            assert ctx.verify_mode is ssl.CERT_REQUIRED
            assert ctx.check_hostname is True
