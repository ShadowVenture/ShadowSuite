import pytest

from shadow_trading_suite.exchanges.ratelimit import TokenBucket
from shadow_trading_suite.exchanges.retry import RetryPolicy
from tests.unit.exchanges.fakes import FakeTime


class TestTokenBucket:
    def test_burst_passes_without_sleeping(self) -> None:
        t = FakeTime()
        bucket = TokenBucket(rate_per_s=5, burst=3, monotonic=t.monotonic, sleep=t.sleep)
        for _ in range(3):
            bucket.acquire()
        assert t.sleeps == []

    def test_waits_once_burst_is_spent(self) -> None:
        t = FakeTime()
        bucket = TokenBucket(rate_per_s=5, burst=1, monotonic=t.monotonic, sleep=t.sleep)
        bucket.acquire()
        bucket.acquire()
        assert t.sleeps == [pytest.approx(0.2)]

    def test_sustained_rate_never_exceeded(self) -> None:
        t = FakeTime()
        bucket = TokenBucket(rate_per_s=10, burst=2, monotonic=t.monotonic, sleep=t.sleep)
        start = t.now
        for _ in range(52):
            bucket.acquire()
        # 2 burst tokens, then 50 more at 10/s
        assert t.now - start == pytest.approx(5.0)

    def test_tokens_refill_over_idle_time(self) -> None:
        t = FakeTime()
        bucket = TokenBucket(rate_per_s=1, burst=2, monotonic=t.monotonic, sleep=t.sleep)
        bucket.acquire()
        bucket.acquire()
        t.now += 10  # idle: refills, capped at burst
        bucket.acquire()
        bucket.acquire()
        assert t.sleeps == []
        bucket.acquire()
        assert t.sleeps == [pytest.approx(1.0)]

    @pytest.mark.parametrize(("rate", "burst"), [(0, 1), (-1, 1), (1, 0)])
    def test_invalid_parameters(self, rate: float, burst: int) -> None:
        with pytest.raises(ValueError, match="rate_per_s"):
            TokenBucket(rate_per_s=rate, burst=burst)


class TestRetryPolicy:
    def test_exponential_backoff_capped(self) -> None:
        p = RetryPolicy(base_delay_s=0.5, max_delay_s=2.0)
        assert [p.backoff(a) for a in (1, 2, 3, 4, 5)] == [0.5, 1.0, 2.0, 2.0, 2.0]

    def test_retry_after_seconds_honoured(self) -> None:
        assert RetryPolicy().delay_for(attempt=1, retry_after="3") == 3.0

    def test_retry_after_too_long_means_give_up(self) -> None:
        assert RetryPolicy(max_retry_after_s=30).delay_for(attempt=1, retry_after="120") is None

    @pytest.mark.parametrize("header", [None, "", "soon", "-5", "Wed, 21 Oct 2026 07:28:00 GMT"])
    def test_missing_or_unparseable_retry_after_uses_backoff(self, header: str | None) -> None:
        p = RetryPolicy(base_delay_s=0.5)
        assert p.delay_for(attempt=2, retry_after=header) == 1.0

    def test_retryable_statuses(self) -> None:
        p = RetryPolicy()
        assert {429, 500, 502, 503, 504} <= p.retry_statuses
        assert 400 not in p.retry_statuses
        assert 404 not in p.retry_statuses

    def test_max_attempts_bounded(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            RetryPolicy(max_attempts=0)
        with pytest.raises(ValueError, match="max_attempts"):
            RetryPolicy(max_attempts=11)
