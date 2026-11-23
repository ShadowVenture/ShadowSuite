"""Shared market-data service: exactly one adapter — and so one rate limiter — per venue.

Every bot instance obtains read-only `MarketDataView`s from the same service, so all bots
on a venue share that venue's rate limit. Strategies and bots never construct transports,
clients, adapters or limiters themselves (see tests/safety/test_suite_boundaries.py).
"""

from collections.abc import Iterable
from datetime import datetime

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.core.market import (
    Candle,
    FundingRate,
    IndexPrice,
    MarkPrice,
    OrderBookSnapshot,
    Trade,
)
from shadow_trading_suite.core.time import Clock
from shadow_trading_suite.exchanges.base import (
    CandleInterval,
    Capability,
    ExchangeAdapter,
    MarketDataView,
    VenueLimits,
)
from shadow_trading_suite.exchanges.coinbase import CoinbaseAdapter
from shadow_trading_suite.exchanges.cryptocom import CryptoComAdapter


class _ReadOnlyView:
    """Delegates read-only market-data calls; exposes no discovery, transport or adapter."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: ExchangeAdapter) -> None:
        self._adapter = adapter

    @property
    def venue(self) -> Venue:
        return self._adapter.venue

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self._adapter.capabilities

    @property
    def limits(self) -> VenueLimits:
        return self._adapter.limits

    def get_candles(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        return self._adapter.get_candles(symbol, interval, start, end)

    def get_order_book(self, symbol: str, depth: int) -> OrderBookSnapshot:
        return self._adapter.get_order_book(symbol, depth)

    def get_recent_trades(self, symbol: str, limit: int) -> list[Trade]:
        return self._adapter.get_recent_trades(symbol, limit)

    def get_funding_rate(self, symbol: str) -> FundingRate | None:
        return self._adapter.get_funding_rate(symbol)

    def get_funding_history(self, symbol: str, start: datetime, end: datetime) -> list[FundingRate]:
        return self._adapter.get_funding_history(symbol, start, end)

    def get_mark_prices(self, symbol: str, start: datetime, end: datetime) -> list[MarkPrice]:
        return self._adapter.get_mark_prices(symbol, start, end)

    def get_index_prices(self, symbol: str, start: datetime, end: datetime) -> list[IndexPrice]:
        return self._adapter.get_index_prices(symbol, start, end)


class MarketDataService:
    def __init__(self, adapters: Iterable[ExchangeAdapter]) -> None:
        self._adapters: dict[Venue, ExchangeAdapter] = {}
        for adapter in adapters:
            if adapter.venue in self._adapters:
                raise ValueError(f"duplicate adapter for venue {adapter.venue.value}")
            self._adapters[adapter.venue] = adapter

    @property
    def venues(self) -> tuple[Venue, ...]:
        return tuple(self._adapters)

    def view(self, venue: Venue) -> MarketDataView:
        """A read-only market-data view for a bot/strategy. All views share the adapter."""
        try:
            return _ReadOnlyView(self._adapters[venue])
        except KeyError:
            raise ValueError(f"no adapter configured for venue {venue.value}") from None

    def discovery_sources(self) -> list[ExchangeAdapter]:
        """Platform use only (instrument registry refresh); never handed to strategies."""
        return list(self._adapters.values())


def create_default_service(clock: Clock | None = None) -> MarketDataService:
    """The process-wide service: one Coinbase and one Crypto.com adapter."""
    return MarketDataService([CoinbaseAdapter(clock=clock), CryptoComAdapter(clock=clock)])
