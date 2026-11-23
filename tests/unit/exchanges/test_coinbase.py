import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from shadow_trading_suite.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentKind,
    Venue,
)
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.exchanges.base import CandleInterval, Capability, UnsupportedRequestError
from shadow_trading_suite.exchanges.coinbase import CoinbaseAdapter
from shadow_trading_suite.exchanges.coinbase.details import CoinbaseDetails
from shadow_trading_suite.exchanges.errors import (
    ExchangeHttpError,
    ResponseFormatError,
    TransportPolicyError,
)
from tests.unit.exchanges.fakes import (
    FakeTime,
    ScriptedFetcher,
    fixture_bytes,
    fixture_json,
    ok,
    status,
)

API = "/api/v3/brokerage/market"
NOW = datetime(2026, 10, 19, 12, 30, tzinfo=UTC)


def products_route(params: dict[str, str]) -> Any:
    name = {
        ("SPOT", None): "products_spot",
        ("FUTURE", "PERPETUAL"): "products_perpetual",
        ("FUTURE", "EXPIRING"): "products_expiring",
    }[(params["product_type"], params.get("contract_expiry_type"))]
    return ok(json.dumps(fixture_json(f"coinbase/{name}.json")))


def make(
    routes: dict[str, Any] | None = None, now: datetime = NOW
) -> tuple[CoinbaseAdapter, ScriptedFetcher]:
    fetcher = ScriptedFetcher(routes=routes or {f"{API}/products": products_route})
    t = FakeTime()
    adapter = CoinbaseAdapter(
        fetcher=fetcher, clock=ManualClock(now), monotonic=t.monotonic, sleep=t.sleep
    )
    return adapter, fetcher


def by_symbol(instruments: tuple[Instrument, ...]) -> dict[str, Instrument]:
    return {i.id.symbol: i for i in instruments}


class TestDiscovery:
    def test_requests_spot_perpetual_and_expiring_listings(self) -> None:
        adapter, fetcher = make()
        adapter.list_instruments()
        assert sorted(
            (p["product_type"], p.get("contract_expiry_type", "")) for p in fetcher.params
        ) == [
            ("FUTURE", "EXPIRING"),
            ("FUTURE", "PERPETUAL"),
            ("SPOT", ""),
        ]

    def test_discovers_all_fixture_products(self) -> None:
        result = make()[0].list_instruments()
        assert result.venue is Venue.COINBASE
        assert len(result.instruments) == 12
        assert result.rejected == ()

    def test_spot_normalisation(self) -> None:
        btc = by_symbol(make()[0].list_instruments().instruments)["BTC-USD"]
        assert btc.kind is InstrumentKind.SPOT
        assert (btc.base, btc.quote, btc.settle) == ("BTC", "USD", None)
        assert btc.tick_size == Decimal("0.01")
        assert btc.lot_size == Decimal("0.00000001")
        assert btc.min_qty == Decimal("0.00000001")
        assert btc.min_notional == Decimal(1)
        assert btc.published_max_leverage is None
        assert btc.sub_venue == "CBE"
        assert btc.listed_tradable
        assert btc.asset_class is AssetClass.CRYPTO

    def test_eur_spot_pair_present(self) -> None:
        assert by_symbol(make()[0].list_instruments().instruments)["BTC-EUR"].quote == "EUR"

    def test_delisted_spot_not_listed_tradable(self) -> None:
        assert not by_symbol(make()[0].list_instruments().instruments)["DAI-USD"].listed_tradable

    def test_intx_perpetual(self) -> None:
        perp = by_symbol(make()[0].list_instruments().instruments)["BTC-PERP-INTX"]
        assert perp.kind is InstrumentKind.PERPETUAL
        assert (perp.base, perp.quote, perp.settle) == ("BTC", "USDC", "USDC")
        assert perp.expiry is None
        assert perp.published_max_leverage == Decimal(50)
        assert perp.tick_size == Decimal("0.1")
        assert perp.min_notional == Decimal(10)
        assert perp.sub_venue == "INTX"
        assert not perp.listed_tradable  # trading_disabled=true in the listing

    def test_non_crypto_perpetual_classified(self) -> None:
        aapl = by_symbol(make()[0].list_instruments().instruments)["AAPL-PERP-INTX"]
        assert aapl.asset_class is AssetClass.EQUITY

    def test_fcm_long_dated_future(self) -> None:
        bip = by_symbol(make()[0].list_instruments().instruments)["BIP-20DEC30-CDE"]
        assert bip.kind is InstrumentKind.DATED_FUTURE
        assert bip.expiry == datetime(2089, 12, 30, 16, tzinfo=UTC)  # as published by venue
        assert bip.contract_size == Decimal("0.01")
        assert (bip.base, bip.settle) == ("BTC", "USD")
        assert bip.sub_venue == "FCM"
        assert bip.min_notional is None  # quote_min_size "0" means not enforced
        # FCM publishes no max leverage: not invented (derived figure lives in venue_details)
        assert bip.published_max_leverage is None

    def test_commodity_future_classified(self) -> None:
        gold = by_symbol(make()[0].list_instruments().instruments)["GOL-25NOV26-CDE"]
        assert gold.asset_class is AssetClass.COMMODITY
        assert gold.expiry == datetime(2026, 11, 25, 18, 30, tzinfo=UTC)

    def test_malformed_product_rejected_others_kept(self) -> None:
        def route(params: dict[str, str]) -> Any:
            data = fixture_json("coinbase/products_spot.json")
            data["products"][0]["price_increment"] = "-1"
            del data["products"][1]["base_currency_id"]
            if params["product_type"] == "SPOT":
                return ok(json.dumps(data))
            return products_route(params)

        result = make({f"{API}/products": route})[0].list_instruments()
        assert len(result.instruments) == 10
        assert {r.symbol for r in result.rejected} == {"BTC-USD", "BTC-USDC"}
        assert all(r.reason for r in result.rejected)

    def test_unknown_expiry_type_rejected(self) -> None:
        def route(params: dict[str, str]) -> Any:
            if params.get("contract_expiry_type") == "PERPETUAL":
                data = fixture_json("coinbase/products_perpetual.json")
                data["products"][0]["future_product_details"]["contract_expiry_type"] = "WEIRD"
                return ok(json.dumps(data))
            return products_route(params)

        result = make({f"{API}/products": route})[0].list_instruments()
        assert len(result.rejected) == 1

    def test_paginated_listing_fails_closed(self) -> None:
        def route(params: dict[str, str]) -> Any:
            data = fixture_json("coinbase/products_spot.json")
            data["pagination"]["has_next"] = True
            return ok(json.dumps(data))

        with pytest.raises(ResponseFormatError, match="incomplete"):
            make({f"{API}/products": route})[0].list_instruments()

    def test_wrong_top_level_shape_fails_closed(self) -> None:
        with pytest.raises(ResponseFormatError):
            make({f"{API}/products": lambda p: ok("[]")})[0].list_instruments()


class TestCandles:
    START = datetime(2026, 10, 19, 8, tzinfo=UTC)  # 1792396800

    def adapter(self, now: datetime = NOW) -> tuple[CoinbaseAdapter, ScriptedFetcher]:
        body = json.dumps(fixture_json("coinbase/candles_btc_usd_1h.json"))
        return make({f"{API}/products/BTC-USD/candles": lambda p: ok(body)}, now=now)

    def test_request_parameters(self) -> None:
        adapter, fetcher = self.adapter()
        adapter.get_candles(
            "BTC-USD", CandleInterval.H1, self.START, self.START + timedelta(hours=4)
        )
        # Coinbase treats start AND end as inclusive (observed 2026-10-19): `end` must be the
        # open time of the LAST wanted bucket, otherwise an extra bucket is requested and, at
        # the 350 limit, the OLDEST candle is silently dropped by the venue.
        assert fetcher.params == [
            {"granularity": "ONE_HOUR", "start": "1792396800", "end": "1792407600", "limit": "4"}
        ]

    def test_full_350_bucket_window_requests_exactly_350_inclusive_buckets(self) -> None:
        adapter, fetcher = self.adapter()
        adapter.get_candles(
            "BTC-USD", CandleInterval.M1, self.START, self.START + timedelta(minutes=350)
        )
        params = fetcher.params[0]
        assert int(params["end"]) - int(params["start"]) == 349 * 60
        assert params["limit"] == "350"

    def test_unaligned_window_rejected_before_request(self) -> None:
        adapter, fetcher = self.adapter()
        with pytest.raises(UnsupportedRequestError, match="aligned"):
            adapter.get_candles(
                "BTC-USD", CandleInterval.H1, self.START + timedelta(minutes=1), NOW
            )
        assert fetcher.requests == []

    def test_venue_rejection_of_oversized_window_is_not_retried(self) -> None:
        body = fixture_bytes("coinbase/error_candles_too_many.json")
        adapter, fetcher = make({f"{API}/products/BTC-USD/candles": lambda p: status(400, body)})
        with pytest.raises(ExchangeHttpError, match="less than 350"):
            adapter.get_candles(
                "BTC-USD", CandleInterval.H1, self.START, self.START + timedelta(hours=2)
            )
        assert len(fetcher.requests) == 1

    def test_returns_ascending_validated_candles(self) -> None:
        adapter, _ = self.adapter()
        candles = adapter.get_candles(
            "BTC-USD", CandleInterval.H1, self.START, self.START + timedelta(hours=4)
        )
        assert [c.open_time for c in candles] == [self.START + timedelta(hours=h) for h in range(4)]
        assert candles[0].open == Decimal("82637.82")
        assert candles[0].interval == timedelta(hours=1)
        assert all(c.instrument_id.venue is Venue.COINBASE for c in candles)

    def test_incomplete_candle_dropped(self) -> None:
        adapter, _ = self.adapter(now=self.START + timedelta(hours=3, minutes=30))
        candles = adapter.get_candles(
            "BTC-USD", CandleInterval.H1, self.START, self.START + timedelta(hours=4)
        )
        assert len(candles) == 3

    def test_window_over_venue_limit_rejected_before_request(self) -> None:
        adapter, fetcher = self.adapter()
        with pytest.raises(UnsupportedRequestError, match="350"):
            adapter.get_candles(
                "BTC-USD", CandleInterval.M1, self.START, self.START + timedelta(minutes=351)
            )
        assert fetcher.requests == []

    def test_unsupported_interval(self) -> None:
        adapter, _ = self.adapter()
        with pytest.raises(UnsupportedRequestError, match="12h"):
            adapter.get_candles("BTC-USD", CandleInterval.H12, self.START, NOW)

    def test_bad_ohlc_fails_closed(self) -> None:
        bad = {"candles": [{"start": "1792396800", "low": "5", "high": "1", "open": "2",
                            "close": "2", "volume": "1"}]}  # fmt: skip
        adapter, _ = make({f"{API}/products/BTC-USD/candles": lambda p: ok(json.dumps(bad))})
        with pytest.raises(ResponseFormatError):
            adapter.get_candles(
                "BTC-USD", CandleInterval.H1, self.START, self.START + timedelta(hours=1)
            )


class TestBookTradesFunding:
    def test_order_book(self) -> None:
        body = json.dumps(fixture_json("coinbase/product_book_btc_usd.json"))
        adapter, fetcher = make({f"{API}/product_book": lambda p: ok(body)})
        book = adapter.get_order_book("BTC-USD", depth=5)
        assert fetcher.params == [{"product_id": "BTC-USD", "limit": "5"}]
        assert book.best_bid is not None
        assert book.best_ask is not None
        assert book.best_bid.price == Decimal("82467.29")
        assert book.best_ask.price == Decimal("82467.3")
        assert book.ts == datetime(2026, 10, 19, 12, 28, 19, 734372, tzinfo=UTC)
        assert len(book.bids) == 5

    def test_recent_trades_ascending_without_guessing_aggressor(self) -> None:
        body = json.dumps(fixture_json("coinbase/ticker_btc_usd.json"))
        adapter, fetcher = make({f"{API}/products/BTC-USD/ticker": lambda p: ok(body)})
        trades = adapter.get_recent_trades("BTC-USD", limit=5)
        assert fetcher.params == [{"limit": "5"}]
        assert [t.ts for t in trades] == sorted(t.ts for t in trades)
        assert trades[-1].trade_id == "1107938868"
        assert all(t.aggressor is None for t in trades)

    def test_perpetual_funding_rate(self) -> None:
        body = json.dumps(fixture_json("coinbase/product_btc_perp_intx.json"))
        adapter, _ = make({f"{API}/products/BTC-PERP-INTX": lambda p: ok(body)})
        funding = adapter.get_funding_rate("BTC-PERP-INTX")
        assert funding is not None
        assert funding.rate == Decimal("0.000059")
        assert funding.interval == timedelta(hours=1)
        # The venue's own timestamp is preserved (it is 18 days old: staleness is visible).
        assert funding.ts == datetime(2026, 10, 1, 9, 0, 0, 16, tzinfo=UTC)

    def test_spot_has_no_funding(self) -> None:
        spot = fixture_json("coinbase/products_spot.json")["products"][0]
        adapter, _ = make({f"{API}/products/BTC-USD": lambda p: ok(json.dumps(spot))})
        assert adapter.get_funding_rate("BTC-USD") is None

    @pytest.mark.parametrize("symbol", ["BTC-USD/../orders", "btc usd", "BTC-USD?x=1", ""])
    def test_unsafe_symbols_rejected_by_policy(self, symbol: str) -> None:
        adapter, fetcher = make({})
        with pytest.raises(TransportPolicyError):
            adapter.get_recent_trades(symbol, limit=5)
        assert fetcher.requests == []

    @pytest.mark.parametrize("depth", [0, 1001])
    def test_book_depth_bounds(self, depth: int) -> None:
        with pytest.raises(UnsupportedRequestError):
            make({})[0].get_order_book("BTC-USD", depth=depth)


class TestVenueDetails:
    """C2: exchange-specific metadata preserved; raw vs derived kept apart; nothing invented."""

    def details(self, symbol: str) -> CoinbaseDetails:
        d = by_symbol(make()[0].list_instruments().instruments)[symbol].venue_details
        assert isinstance(d, CoinbaseDetails)
        return d

    def test_spot_order_constraints_preserved(self) -> None:
        d = self.details("BTC-USD")
        assert d.product_type == "SPOT"
        assert d.product_venue == "CBE"
        assert d.status == "online"
        assert d.base_max_size == Decimal(3400)
        assert d.quote_max_size == Decimal(150000000)
        assert d.quote_increment == Decimal("0.01")
        assert (d.limit_only, d.post_only, d.cancel_only, d.auction_mode) == (
            False,
            False,
            False,
            False,
        )
        assert d.alias_to == ("BTC-USDC",)
        assert d.future is None

    def test_intx_perpetual_contract_spec_and_published_leverage(self) -> None:
        d = self.details("BTC-PERP-INTX")
        assert d.future is not None
        assert d.future.contract_expiry_type == "PERPETUAL"
        assert d.future.funding_interval == "3600s"
        assert d.future.published_max_leverage == Decimal(50)
        assert d.future.risk_managed_by == "MANAGED_BY_VENUE"
        assert d.trading_disabled is True

    def test_fcm_margin_rates_raw_and_leverage_derived_separately(self) -> None:
        inst = by_symbol(make()[0].list_instruments().instruments)["BIP-20DEC30-CDE"]
        d = inst.venue_details
        assert isinstance(d, CoinbaseDetails)
        assert d.future is not None
        assert d.future.overnight_margin_rate is not None
        assert d.future.overnight_margin_rate.long_margin_rate == Decimal("0.21025")
        assert d.future.overnight_margin_rate.short_margin_rate == Decimal("0.256625")
        assert d.future.intraday_margin_rate is not None
        assert d.future.intraday_margin_rate.long_margin_rate == Decimal("0.100079")
        # Not published by the venue → not invented on the normalised instrument...
        assert d.future.published_max_leverage is None
        assert inst.published_max_leverage is None
        # ...but available as an explicitly derived figure.
        assert d.future.derived_max_leverage_from_overnight_margin == Decimal(3)
        assert d.future.contract_expiry_name == "2030-12"
        assert d.future.contract_expiry == "2089-12-30T16:00:00Z"  # raw, as published

    def test_dynamic_market_values_not_stored_as_metadata(self) -> None:
        d = self.details("BTC-PERP-INTX")
        dumped = d.model_dump()
        for dynamic in ("price", "volume_24h", "funding_rate", "open_interest", "index_price"):
            assert dynamic not in dumped
            assert dynamic not in d.unrecognized
        assert d.future is not None
        assert "funding_rate" not in d.future.unrecognized

    def test_unknown_scalar_fields_preserved_for_forward_compatibility(self) -> None:
        def route(params: dict[str, str]) -> Any:
            data = fixture_json("coinbase/products_spot.json")
            data["products"][0]["brand_new_rule"] = "reduce_only_after_hours"
            data["products"][0]["new_nested"] = {"x": 1}
            if params["product_type"] == "SPOT":
                return ok(json.dumps(data))
            return products_route(params)

        inst = by_symbol(make({f"{API}/products": route})[0].list_instruments().instruments)[
            "BTC-USD"
        ]
        assert isinstance(inst.venue_details, CoinbaseDetails)
        assert inst.venue_details.unrecognized["brand_new_rule"] == "reduce_only_after_hours"
        assert "new_nested" not in inst.venue_details.unrecognized  # only scalars are kept

    def test_spot_has_no_published_leverage(self) -> None:
        assert (
            by_symbol(make()[0].list_instruments().instruments)["BTC-USD"].published_max_leverage
            is None
        )


class TestCapabilities:
    def test_declared_capabilities_and_limits(self) -> None:
        adapter = make({})[0]
        assert adapter.capabilities == frozenset(
            {
                Capability.INSTRUMENTS,
                Capability.CANDLES,
                Capability.TRADES,
                Capability.ORDER_BOOK,
                Capability.FUNDING_CURRENT,
            }
        )
        assert adapter.limits.max_candles_per_request == 350

    @pytest.mark.parametrize(
        "method", ["get_funding_history", "get_mark_prices", "get_index_prices"]
    )
    def test_history_not_published_by_coinbase(self, method: str) -> None:
        adapter, fetcher = make({})
        with pytest.raises(UnsupportedRequestError, match="coinbase"):
            getattr(adapter, method)("BTC-PERP-INTX", NOW - timedelta(days=1), NOW)
        assert fetcher.requests == []
