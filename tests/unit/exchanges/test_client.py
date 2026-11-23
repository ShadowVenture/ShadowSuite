from decimal import Decimal

import pytest

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.exchanges.client import PublicRestClient
from shadow_trading_suite.exchanges.errors import (
    ExchangeHttpError,
    ExchangeUnavailableError,
    RateLimitedError,
    ResponseFormatError,
    TransportError,
    TransportPolicyError,
)
from shadow_trading_suite.exchanges.policy import EndpointPolicy
from shadow_trading_suite.exchanges.ratelimit import TokenBucket
from shadow_trading_suite.exchanges.retry import RetryPolicy
from tests.unit.exchanges.fakes import FakeTime, ScriptedFetcher, ok, status

POLICY = EndpointPolicy(
    venue=Venue.COINBASE,
    base_url="https://api.example.com",
    path_patterns=(r"/v1/thing",),
    allowed_params=frozenset({"id"}),
)


def make_client(
    fetcher: ScriptedFetcher, t: FakeTime, retry: RetryPolicy | None = None
) -> PublicRestClient:
    return PublicRestClient(
        policy=POLICY,
        fetcher=fetcher,
        limiter=TokenBucket(rate_per_s=100, burst=100, monotonic=t.monotonic, sleep=t.sleep),
        retry=retry or RetryPolicy(max_attempts=3, base_delay_s=0.5, max_delay_s=4),
        sleep=t.sleep,
        timeout_s=5,
    )


class TestSuccess:
    def test_returns_json_with_exact_decimals(self) -> None:
        f = ScriptedFetcher([ok('{"price": 0.1, "n": 3, "s": "1.10"}')])
        data = make_client(f, FakeTime()).get_json("/v1/thing", {"id": "A"})
        assert data == {"price": Decimal("0.1"), "n": 3, "s": "1.10"}
        assert isinstance(data["price"], Decimal)

    def test_request_carries_only_safe_headers(self) -> None:
        f = ScriptedFetcher([ok("{}")])
        make_client(f, FakeTime()).get_json("/v1/thing")
        req = f.requests[0]
        assert {k.lower() for k in req.headers} == {"accept", "user-agent"}
        assert "paper-only" in req.headers["User-Agent"]
        assert req.timeout_s == 5

    def test_every_attempt_goes_through_rate_limiter(self) -> None:
        t = FakeTime()
        f = ScriptedFetcher([ok("{}")] * 3)
        client = PublicRestClient(
            policy=POLICY,
            fetcher=f,
            limiter=TokenBucket(rate_per_s=2, burst=1, monotonic=t.monotonic, sleep=t.sleep),
            sleep=t.sleep,
        )
        for _ in range(3):
            client.get_json("/v1/thing")
        assert t.sleeps == [pytest.approx(0.5), pytest.approx(0.5)]


class TestPolicyEnforcement:
    def test_disallowed_path_never_reaches_fetcher(self) -> None:
        f = ScriptedFetcher()
        with pytest.raises(TransportPolicyError):
            make_client(f, FakeTime()).get_json("/v1/orders")
        assert f.requests == []

    def test_disallowed_param_never_reaches_fetcher(self) -> None:
        f = ScriptedFetcher()
        with pytest.raises(TransportPolicyError):
            make_client(f, FakeTime()).get_json("/v1/thing", {"api_key": "x"})
        assert f.requests == []

    def test_client_exposes_no_write_methods(self) -> None:
        public = {n for n in dir(PublicRestClient) if not n.startswith("_")}
        assert public == {"get_json", "venue"}


class TestRetries:
    def test_retries_transient_5xx_then_succeeds(self) -> None:
        t = FakeTime()
        f = ScriptedFetcher([status(503), status(502), ok('{"ok": true}')])
        assert make_client(f, t).get_json("/v1/thing") == {"ok": True}
        assert t.sleeps == [0.5, 1.0]

    def test_retries_network_errors(self) -> None:
        t = FakeTime()
        f = ScriptedFetcher([TransportError("timeout"), ok("{}")])
        assert make_client(f, t).get_json("/v1/thing") == {}
        assert t.sleeps == [0.5]

    def test_gives_up_after_max_attempts(self) -> None:
        t = FakeTime()
        f = ScriptedFetcher([status(500)] * 3)
        with pytest.raises(ExchangeUnavailableError, match="3 attempts"):
            make_client(f, t).get_json("/v1/thing")
        assert len(f.requests) == 3
        assert t.sleeps == [0.5, 1.0]  # no sleep after the final attempt

    def test_network_errors_exhausted(self) -> None:
        f = ScriptedFetcher([TransportError("down")] * 3)
        with pytest.raises(ExchangeUnavailableError, match="down"):
            make_client(f, FakeTime()).get_json("/v1/thing")

    def test_429_honours_retry_after(self) -> None:
        t = FakeTime()
        f = ScriptedFetcher([status(429, headers={"retry-after": "2"}), ok("{}")])
        make_client(f, t).get_json("/v1/thing")
        assert t.sleeps == [2.0]

    def test_429_with_excessive_retry_after_fails_closed_immediately(self) -> None:
        t = FakeTime()
        f = ScriptedFetcher([status(429, headers={"retry-after": "3600"})])
        with pytest.raises(RateLimitedError):
            make_client(f, t).get_json("/v1/thing")
        assert t.sleeps == []
        assert len(f.requests) == 1

    def test_429_exhausted_is_rate_limited_error(self) -> None:
        f = ScriptedFetcher([status(429)] * 3)
        with pytest.raises(RateLimitedError):
            make_client(f, FakeTime()).get_json("/v1/thing")

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 301, 302])
    def test_non_retryable_statuses_fail_immediately(self, code: int) -> None:
        f = ScriptedFetcher([status(code, b'{"error":"x"}')])
        with pytest.raises(ExchangeHttpError) as info:
            make_client(f, FakeTime()).get_json("/v1/thing")
        assert info.value.status == code
        assert len(f.requests) == 1


class TestResponseValidation:
    @pytest.mark.parametrize("body", [b"", b"not json", b"<html>", b"\xff\xfe"])
    def test_malformed_body(self, body: bytes) -> None:
        f = ScriptedFetcher([ok(body)])
        with pytest.raises(ResponseFormatError):
            make_client(f, FakeTime()).get_json("/v1/thing")

    def test_non_finite_numbers_rejected(self) -> None:
        f = ScriptedFetcher([ok('{"x": NaN}')])
        with pytest.raises(ResponseFormatError):
            make_client(f, FakeTime()).get_json("/v1/thing")

    def test_error_message_is_truncated(self) -> None:
        f = ScriptedFetcher([status(400, b"e" * 5000)])
        with pytest.raises(ExchangeHttpError) as info:
            make_client(f, FakeTime()).get_json("/v1/thing")
        assert len(str(info.value)) < 400
