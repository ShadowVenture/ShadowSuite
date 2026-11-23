"""UTC-only time handling and an injectable clock for deterministic tests and backtests."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Protocol

from pydantic import AfterValidator


def ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; normalise aware ones to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
"""Pydantic field type: timezone-aware datetime, normalised to UTC."""


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Monotonic clock controlled by tests and the backtester."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        value = ensure_utc(value)
        if value < self._now:
            raise ValueError("clock cannot move backwards")
        self._now = value

    def advance(self, delta: timedelta) -> None:
        self.set(self._now + delta)
