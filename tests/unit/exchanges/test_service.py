"""C3: one adapter — hence one rate limiter — per venue, shared by every bot via read-only views."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.exchanges.base import ExchangeAdapter, MarketDataView
from shadow_trading_suite.exchanges.coinbase import CoinbaseAdapter
from shadow_trading_suite.exchanges.coinbase.adapter import BURST
from shadow_trading_suite.exchanges.cryptocom import CryptoComAdapter
from shadow_trading_suite.exchanges.ratelimit import TokenBucket
from shadow_trading_suite.exchanges.service import MarketDataService, create_default_service
from tests.unit.exchanges.fakes import FakeTime, ScriptedFetcher, fixture_json, ok

NOW = datetime(2026, 10, 19, 12, 30, tzinfo=UTC)
TICKER = "/api/v3/brokerage/market/products/BTC-USD/ticker"


def coinbase(t: FakeTime) -> tuple[CoinbaseAdapter, ScriptedFetcher]:
    body = json.dumps(fixture_json("coinbase/ticker_btc_usd.json"))
    fetcher = ScriptedFetcher(routes={TICKER: lambda p: ok(body)})
    adapter = CoinbaseAdapter(
        fetcher=fetcher, clock=ManualClock(NOW), monotonic=t.monotonic, sleep=t.sleep
    )
    return adapter, fetcher


def cryptocom(t: FakeTime) -> CryptoComAdapter:
    return CryptoComAdapter(
        fetcher=ScriptedFetcher(), clock=ManualClock(NOW), monotonic=t.monotonic, sleep=t.sleep
    )


class TestOneAdapterPerVenue:
    def test_duplicate_venue_rejected(self) -> None:
        t = FakeTime()
        with pytest.raises(ValueError, match="coinbase"):
            MarketDataService([coinbase(t)[0], coinbase(t)[0]])

    def test_unknown_venue_view_rejected(self) -> None:
        service = MarketDataService([coinbase(FakeTime())[0]])
        with pytest.raises(ValueError, match="cryptocom"):
            service.view(Venue.CRYPTOCOM)

    def test_venues(self) -> None:
        t = FakeTime()
        service = MarketDataService([coinbase(t)[0], cryptocom(t)])
        assert service.venues == (Venue.COINBASE, Venue.CRYPTOCOM)

    def test_default_service_has_one_adapter_per_supported_venue(self) -> None:
        service = create_default_service()
        assert service.venues == (Venue.COINBASE, Venue.CRYPTOCOM)
        sources: list[ExchangeAdapter] = service.discovery_sources()
        assert [s.venue for s in sources] == [Venue.COINBASE, Venue.CRYPTOCOM]


class TestSharedRateLimit:
    def test_two_bots_on_one_venue_share_one_rate_limiter(self) -> None:
        t = FakeTime()
        adapter, fetcher = coinbase(t)
        service = MarketDataService([adapter])
        short_term = service.view(Venue.COINBASE)
        long_term = service.view(Venue.COINBASE)
        # BURST requests pass immediately; the next one from EITHER bot must wait.
        for i in range(BURST):
            (short_term if i % 2 else long_term).get_recent_trades("BTC-USD", limit=5)
        assert t.sleeps == []
        long_term.get_recent_trades("BTC-USD", limit=5)
        assert t.sleeps == [pytest.approx(0.2)]
        assert len(fetcher.requests) == BURST + 1

    def test_adapter_accepts_an_injected_shared_limiter(self) -> None:
        t = FakeTime()
        shared = TokenBucket(rate_per_s=1, burst=1, monotonic=t.monotonic, sleep=t.sleep)
        body = json.dumps(fixture_json("coinbase/ticker_btc_usd.json"))
        adapters = [
            CoinbaseAdapter(
                fetcher=ScriptedFetcher(routes={TICKER: lambda p: ok(body)}),
                clock=ManualClock(NOW),
                limiter=shared,
                sleep=t.sleep,
            )
            for _ in range(2)
        ]
        adapters[0].get_recent_trades("BTC-USD", limit=5)
        adapters[1].get_recent_trades("BTC-USD", limit=5)
        assert t.sleeps == [pytest.approx(1.0)]


class TestReadOnlyView:
    def view(self) -> MarketDataView:
        t = FakeTime()
        return MarketDataService([coinbase(t)[0]]).view(Venue.COINBASE)

    def test_view_conforms_to_market_data_view(self) -> None:
        view: MarketDataView = self.view()
        assert view.venue is Venue.COINBASE

    def test_view_exposes_no_discovery_or_transport(self) -> None:
        public = {n for n in dir(self.view()) if not n.startswith("_")}
        assert public == {
            "venue",
            "capabilities",
            "limits",
            "get_candles",
            "get_order_book",
            "get_recent_trades",
            "get_funding_rate",
            "get_funding_history",
            "get_mark_prices",
            "get_index_prices",
        }

    def test_view_cannot_be_extended(self) -> None:
        view: Any = self.view()
        with pytest.raises(AttributeError):
            view.extra = 1
