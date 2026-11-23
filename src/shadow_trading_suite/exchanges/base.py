"""Read-only exchange adapter interfaces shared by all venues.

The interface intentionally has no order, account or balance methods.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from shadow_trading_suite.core.instrument import Instrument, Venue
from shadow_trading_suite.core.market import (
    Candle,
    FundingRate,
    IndexPrice,
    MarkPrice,
    OrderBookSnapshot,
    Trade,
)


class CandleInterval(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H12 = "12h"
    D1 = "1d"

    @property
    def duration(self) -> timedelta:
        unit = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}
        return int(self.value[:-1]) * unit[self.value[-1]]


class Capability(StrEnum):
    """What a venue's PUBLIC API actually provides (verified against docs and live probes)."""

    INSTRUMENTS = "instruments"
    CANDLES = "candles"
    TRADES = "trades"  # recent trades only; no deep history on either venue
    ORDER_BOOK = "order_book"  # snapshots only; no history
    FUNDING_CURRENT = "funding_current"
    FUNDING_HISTORY = "funding_history"
    MARK_PRICE_HISTORY = "mark_price_history"
    INDEX_PRICE_HISTORY = "index_price_history"


@dataclass(frozen=True)
class VenueLimits:
    max_candles_per_request: int
    max_trades_per_request: int
    max_book_depth: int
    max_valuation_points: int = 0  # 0: venue publishes no valuation history


def require_aligned(start: datetime, end: datetime, interval: CandleInterval) -> int:
    """Number of buckets in [start, end); both bounds must sit on the UTC interval grid."""
    step = int(interval.duration.total_seconds())
    if end <= start:
        raise UnsupportedRequestError("end must be after start")
    if int(start.timestamp()) % step or int(end.timestamp()) % step or start.microsecond:
        raise UnsupportedRequestError(f"start/end must be aligned to the {interval.value} grid")
    return (int(end.timestamp()) - int(start.timestamp())) // step


class UnsupportedRequestError(ValueError):
    """The venue cannot serve this request (interval, depth, window size...)."""


@dataclass(frozen=True)
class RejectedInstrument:
    symbol: str
    reason: str


@dataclass(frozen=True)
class DiscoveryResult:
    venue: Venue
    instruments: tuple[Instrument, ...]
    rejected: tuple[RejectedInstrument, ...]


class MarketDataView(Protocol):
    """Read-only market data for one venue. This is all a strategy may see."""

    @property
    def venue(self) -> Venue: ...

    @property
    def capabilities(self) -> frozenset[Capability]: ...

    @property
    def limits(self) -> VenueLimits: ...

    def get_candles(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        """Closed candles with open_time in [start, end), ascending."""
        ...

    def get_order_book(self, symbol: str, depth: int) -> OrderBookSnapshot: ...

    def get_recent_trades(self, symbol: str, limit: int) -> list[Trade]:
        """Most recent public trades, ascending by time."""
        ...

    def get_funding_rate(self, symbol: str) -> FundingRate | None:
        """Latest known funding rate for a perpetual-style contract, else None."""
        ...

    def get_funding_history(self, symbol: str, start: datetime, end: datetime) -> list[FundingRate]:
        """Settled funding in [start, end), ascending. Requires FUNDING_HISTORY."""
        ...

    def get_mark_prices(self, symbol: str, start: datetime, end: datetime) -> list[MarkPrice]:
        """Mark-price samples in [start, end), ascending. Requires MARK_PRICE_HISTORY."""
        ...

    def get_index_prices(self, symbol: str, start: datetime, end: datetime) -> list[IndexPrice]:
        """Index-price samples in [start, end), ascending. Requires INDEX_PRICE_HISTORY."""
        ...


class ExchangeAdapter(MarketDataView, Protocol):
    """Standard venue adapter: public market data plus instrument discovery. Read-only."""

    def list_instruments(self) -> DiscoveryResult: ...
