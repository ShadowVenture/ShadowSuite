from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ValidationError

from shadow_trading_suite.core.time import Clock, ManualClock, SystemClock, UtcDatetime

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class _Event(BaseModel):
    ts: UtcDatetime


class TestUtcDatetime:
    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            _Event(ts=datetime(2026, 1, 1, 12, 0))  # noqa: DTZ001

    def test_normalises_offset_to_utc(self) -> None:
        cet = timezone(timedelta(hours=1))
        event = _Event(ts=datetime(2026, 1, 1, 13, 0, tzinfo=cet))
        assert event.ts == T0
        assert event.ts.tzinfo is UTC


class TestManualClock:
    def test_starts_at_given_time(self) -> None:
        assert ManualClock(T0).now() == T0

    def test_advance(self) -> None:
        clock = ManualClock(T0)
        clock.advance(timedelta(seconds=5))
        assert clock.now() == T0 + timedelta(seconds=5)

    def test_cannot_go_backwards(self) -> None:
        clock = ManualClock(T0)
        with pytest.raises(ValueError, match="backwards"):
            clock.advance(timedelta(seconds=-1))
        with pytest.raises(ValueError, match="backwards"):
            clock.set(T0 - timedelta(microseconds=1))

    def test_rejects_naive_start(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            ManualClock(datetime(2026, 1, 1))  # noqa: DTZ001


def test_system_clock_is_utc_and_satisfies_protocol() -> None:
    clock: Clock = SystemClock()
    assert clock.now().tzinfo is UTC
