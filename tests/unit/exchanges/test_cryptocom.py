import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from shadow_trading_suite.core.instrument import AssetClass, Instrument, InstrumentKind, Venue
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.exchanges.base import CandleInterval, Capability, UnsupportedRequestError
from shadow_trading_suite.exchanges.cryptocom import CryptoComAdapter
from shadow_trading_suite.exchanges.cryptocom.adapter import DEFAULT_LIMITS
from shadow_trading_suite.exchanges.cryptocom.details import CryptoComDetails
from shadow_trading_suite.exchanges.errors import (
    ExchangeHttpError,
    ResponseFormatError,
    TransportPolicyError,
    VenueApiError,
)
from tests.unit.exchanges.fakes import (
    FakeTime,
    ScriptedFetcher,
    fixture_bytes,
    fixture_json,
    ok,
    status,
)

PUB = "/exchange/v1/public"
NOW = datetime(2026, 10, 19, 12, 30, tzinfo=UTC)
NEWEST_MARK = Decimal("82269.6")


def make(routes: dict[str, Any], now: datetime = NOW) -> tuple[CryptoComAdapter, ScriptedFetcher]:
    fetcher = ScriptedFetcher(routes=routes)
    t = FakeTime()
    adapter = CryptoComAdapter(
        fetcher=fetcher, clock=ManualClock(now), monotonic=t.monotonic, sleep=t.sleep
    )
    return adapter, fetcher


def fixture_route(rel: str) -> Any:
    body = fixture_bytes(f"cryptocom/{rel}")
    return lambda params: ok(body)


def instruments() -> dict[str, Instrument]:
    result = make({f"{PUB}/get-instruments": fixture_route("get_instruments.json")})[
        0
    ].list_instruments()
    assert result.rejected == ()
    return {i.id.symbol: i for i in result.instruments}


class TestDiscovery:
    def test_all_fixture_instruments_parsed(self) -> None:
        assert len(instruments()) == 13

    def test_spot(self) -> None:
        btc = instruments()["BTC_USD"]
        assert btc.kind is InstrumentKind.SPOT
        assert btc.id.venue is Venue.CRYPTOCOM
        assert (btc.base, btc.quote, btc.settle) == ("BTC", "USD", None)
        assert btc.tick_size == Decimal("0.01")
        assert btc.lot_size == Decimal("0.00001")
        assert btc.min_qty == Decimal("0.00001")
        # venue lists spot margin leverage 50 (kept raw in venue_details); spot is unleveraged
        assert btc.published_max_leverage is None
        assert btc.listed_tradable

    def test_perpetual(self) -> None:
        perp = instruments()["BTCUSD-PERP"]
        assert perp.kind is InstrumentKind.PERPETUAL
        assert (perp.base, perp.quote, perp.settle) == ("BTC", "USD", "USD")
        assert perp.published_max_leverage == Decimal(100)
        assert perp.contract_size == Decimal(1)
        assert perp.expiry is None

    def test_dated_future(self) -> None:
        fut = instruments()["BTCUSD-261030"]
        assert fut.kind is InstrumentKind.DATED_FUTURE
        assert fut.expiry == datetime(2026, 10, 30, 8, tzinfo=UTC)
        assert fut.asset_class is AssetClass.CRYPTO

    @pytest.mark.parametrize(
        ("symbol", "asset_class"),
        [
            ("AAOIUSD-PERP", AssetClass.EQUITY),
            ("CSOPSAMSUNG2LUSD-PERP", AssetClass.EQUITY),
            ("ANTHROPICIPOUSD-PERP", AssetClass.EQUITY),
            ("BZUSD-PERP", AssetClass.COMMODITY),
            ("EUR_USDT", AssetClass.FX),
            ("BTCUSD-261127", AssetClass.UNKNOWN),  # product_type null: not guessed
            ("SHIB_BTC@OEX_HK", AssetClass.UNKNOWN),
        ],
    )
    def test_asset_classes(self, symbol: str, asset_class: AssetClass) -> None:
        assert instruments()[symbol].asset_class is asset_class

    def test_regional_suffix_becomes_sub_venue(self) -> None:
        assert instruments()["SHIB_BTC@OEX_HK"].sub_venue == "OEX_HK"
        assert instruments()["BTC_USD"].sub_venue is None

    def test_unknown_inst_type_rejected_others_kept(self) -> None:
        data = fixture_json("cryptocom/get_instruments.json")
        data["result"]["data"][0]["inst_type"] = "OPTION"
        data["result"]["data"][1]["price_tick_size"] = "0"
        adapter, _ = make({f"{PUB}/get-instruments": lambda p: ok(json.dumps(data))})
        result = adapter.list_instruments()
        assert len(result.instruments) == 11
        assert len(result.rejected) == 2

    def test_venue_error_envelope_fails_closed(self) -> None:
        body = json.dumps({"code": 10001, "message": "SYS_ERROR", "result": {}})
        adapter, _ = make({f"{PUB}/get-instruments": lambda p: ok(body)})
        with pytest.raises(VenueApiError, match="10001"):
            adapter.list_instruments()

    def test_missing_code_fails_closed(self) -> None:
        adapter, _ = make({f"{PUB}/get-instruments": lambda p: ok('{"result": {"data": []}}')})
        with pytest.raises(ResponseFormatError):
            adapter.list_instruments()


class TestMarketData:
    def test_candles(self) -> None:
        adapter, fetcher = make(
            {f"{PUB}/get-candlestick": fixture_route("get_candlestick_btc_usd_1h.json")}
        )
        start = datetime(2026, 10, 19, 10, tzinfo=UTC)
        candles = adapter.get_candles(
            "BTC_USD", CandleInterval.H1, start, start + timedelta(hours=3)
        )
        assert fetcher.params == [
            {
                "instrument_name": "BTC_USD",
                "timeframe": "1h",
                "start_ts": "1792404000000",
                "end_ts": "1792414800000",
                "count": "3",
            }
        ]
        # 12:00 candle is still open at 12:30 and is dropped
        assert [c.open_time.hour for c in candles] == [10, 11]
        assert candles[0].high == Decimal("82822.86")

    def test_six_hour_candles_unsupported(self) -> None:
        adapter, _ = make({})
        with pytest.raises(UnsupportedRequestError, match="6h"):
            adapter.get_candles("BTC_USD", CandleInterval.H6, NOW - timedelta(days=1), NOW)

    def test_order_book(self) -> None:
        adapter, fetcher = make({f"{PUB}/get-book": fixture_route("get_book_btc_usd.json")})
        book = adapter.get_order_book("BTC_USD", depth=5)
        assert fetcher.params == [{"instrument_name": "BTC_USD", "depth": "5"}]
        assert book.best_bid is not None
        assert book.best_ask is not None
        assert book.best_bid.price == Decimal("82488.99")
        assert book.best_ask.price == Decimal("82489.00")
        assert book.ts == datetime(2026, 10, 19, 12, 28, 22, 14000, tzinfo=UTC)

    @pytest.mark.parametrize("depth", [0, 51])
    def test_book_depth_bounded_by_documented_max(self, depth: int) -> None:
        with pytest.raises(UnsupportedRequestError):
            make({})[0].get_order_book("BTC_USD", depth=depth)

    def test_trades_ascending_with_taker_side(self) -> None:
        adapter, fetcher = make({f"{PUB}/get-trades": fixture_route("get_trades_btc_usd.json")})
        trades = adapter.get_recent_trades("BTC_USD", limit=5)
        assert fetcher.params == [{"instrument_name": "BTC_USD", "count": "5"}]
        assert [t.ts for t in trades] == sorted(t.ts for t in trades)
        latest = trades[-1]
        assert latest.trade_id == "1792412893976738721"
        assert latest.aggressor is Side.SELL
        assert latest.price == Decimal("82488.99")

    @pytest.mark.parametrize("limit", [0, 151])
    def test_trade_limit_bounded_by_documented_max(self, limit: int) -> None:
        with pytest.raises(UnsupportedRequestError):
            make({})[0].get_recent_trades("BTC_USD", limit=limit)

    def test_funding_rate_latest(self) -> None:
        adapter, fetcher = make(
            {f"{PUB}/get-valuations": fixture_route("get_valuations_funding_hist.json")}
        )
        funding = adapter.get_funding_rate("BTCUSD-PERP")
        assert fetcher.params == [
            {"instrument_name": "BTCUSD-PERP", "valuation_type": "funding_hist", "count": "1"}
        ]
        assert funding is not None
        assert funding.rate == Decimal("0.000011763")
        assert funding.ts == datetime(2026, 10, 19, 12, tzinfo=UTC)
        assert funding.interval == timedelta(hours=1)

    def test_spot_has_no_funding_and_makes_no_request(self) -> None:
        adapter, fetcher = make({})
        assert adapter.get_funding_rate("BTC_USD") is None
        assert fetcher.requests == []

    def test_http_400_with_venue_error_is_not_retried(self) -> None:
        body = fixture_bytes("cryptocom/error_invalid_instrument.json")
        adapter, fetcher = make({f"{PUB}/get-book": lambda p: status(400, body)})
        with pytest.raises(ExchangeHttpError, match="40004"):
            adapter.get_order_book("NOPE_NOPE", depth=5)
        assert len(fetcher.requests) == 1

    @pytest.mark.parametrize("symbol", ["BTC_USD&api_key=1", "BTC USD", "../private", ""])
    def test_unsafe_symbols_rejected(self, symbol: str) -> None:
        adapter, fetcher = make({})
        with pytest.raises(TransportPolicyError):
            adapter.get_order_book(symbol, depth=5)
        assert fetcher.requests == []


class TestVenueDetails:
    """C2: exchange-specific metadata preserved; published leverage is not an entitlement."""

    def details(self, symbol: str) -> CryptoComDetails:
        d = instruments()[symbol].venue_details
        assert isinstance(d, CryptoComDetails)
        return d

    def test_spot_margin_leverage_kept_raw_not_normalised(self) -> None:
        inst = instruments()["BTC_USD"]
        d = self.details("BTC_USD")
        assert d.inst_type == "CCY_PAIR"
        assert d.published_max_leverage == Decimal(50)  # venue's spot-margin figure, raw
        assert d.margin_buy_enabled is True  # as published by the venue
        assert d.margin_sell_enabled is True
        assert d.quote_decimals == 2
        assert d.quantity_decimals == 5
        assert inst.published_max_leverage is None  # spot is simulated unleveraged

    def test_perpetual_contract_spec(self) -> None:
        inst = instruments()["BTCUSD-PERP"]
        d = self.details("BTCUSD-PERP")
        assert d.underlying_symbol == "BTCUSD-INDEX"
        assert d.contract_size == Decimal(1)
        assert d.published_max_leverage == Decimal(100)
        assert inst.published_max_leverage == Decimal(100)

    def test_null_product_type_preserved_as_none(self) -> None:
        assert self.details("BTCUSD-261127").product_type is None
        assert self.details("BTCUSD-261127").expiry_timestamp_ms == 1795766400000

    def test_unknown_scalar_fields_preserved(self) -> None:
        data = fixture_json("cryptocom/get_instruments.json")
        data["result"]["data"][0]["new_venue_flag"] = True
        adapter, _ = make({f"{PUB}/get-instruments": lambda p: ok(json.dumps(data))})
        inst = adapter.list_instruments().instruments[0]
        assert isinstance(inst.venue_details, CryptoComDetails)
        assert inst.venue_details.unrecognized == {"new_venue_flag": "true"}


class TestCandleWindowSemantics:
    """Observed 2026-10-19: start inclusive, end exclusive, max 300; omitting `count` silently
    returns only 25 and over-asking silently drops the OLDEST candles."""

    def test_count_always_sent_and_equals_window(self) -> None:
        adapter, fetcher = make(
            {f"{PUB}/get-candlestick": fixture_route("get_candlestick_btc_usd_1h.json")}
        )
        start = datetime(2026, 10, 19, 10, tzinfo=UTC)
        adapter.get_candles("BTC_USD", CandleInterval.H1, start, start + timedelta(hours=300))
        assert fetcher.params[0]["count"] == "300"

    def test_301_rejected_before_request(self) -> None:
        adapter, fetcher = make({})
        start = datetime(2026, 10, 19, 10, tzinfo=UTC)
        with pytest.raises(UnsupportedRequestError, match="300"):
            adapter.get_candles("BTC_USD", CandleInterval.H1, start, start + timedelta(hours=301))
        assert fetcher.requests == []


class TestValuationHistory:
    def test_capabilities_and_limits(self) -> None:
        adapter = make({})[0]
        assert {
            Capability.FUNDING_HISTORY,
            Capability.MARK_PRICE_HISTORY,
            Capability.INDEX_PRICE_HISTORY,
        } <= adapter.capabilities
        assert adapter.limits.max_candles_per_request == 300
        assert adapter.limits.max_valuation_points == 300

    def test_mark_prices_ascending(self) -> None:
        adapter, fetcher = make(
            {f"{PUB}/get-valuations": fixture_route("get_valuations_mark_price.json")}
        )
        end = datetime.fromtimestamp(1792411200, UTC)
        marks = adapter.get_mark_prices("BTCUSD-PERP", end - timedelta(hours=1), end)
        assert fetcher.params[0]["valuation_type"] == "mark_price"
        assert fetcher.params[0]["end_ts"] == "1792411200000"
        assert [m.ts for m in marks] == sorted(m.ts for m in marks)
        assert marks[-1].mark == NEWEST_MARK
        assert len(marks) == 5

    def test_index_prices_carry_index_symbol(self) -> None:
        adapter, _ = make(
            {f"{PUB}/get-valuations": fixture_route("get_valuations_index_price.json")}
        )
        end = datetime.fromtimestamp(1792411200, UTC)
        points = adapter.get_index_prices("BTCUSD-PERP", end - timedelta(hours=1), end)
        assert {p.index_symbol for p in points} == {"BTCUSD-INDEX"}
        assert all(p.instrument_id.symbol == "BTCUSD-PERP" for p in points)

    def test_funding_history_pages_backwards_until_window_covered(self) -> None:
        page_size = 2
        data = fixture_json("cryptocom/get_valuations_funding_hist_page.json")
        rows = data["result"]["data"]  # 6 hourly points, newest first

        def route(params: dict[str, str]) -> Any:
            end_ts = int(params["end_ts"])
            start_ts = int(params["start_ts"])
            page = [r for r in rows if start_ts <= r["t"] < end_ts][: int(params["count"])]
            return ok(json.dumps({"code": 0, "result": {"data": page}}))

        fetcher = ScriptedFetcher(routes={f"{PUB}/get-valuations": route})
        t = FakeTime()
        adapter = CryptoComAdapter(
            fetcher=fetcher,
            clock=ManualClock(NOW),
            monotonic=t.monotonic,
            sleep=t.sleep,
            limits=replace(DEFAULT_LIMITS, max_valuation_points=page_size),
        )
        start = datetime.fromtimestamp(rows[-1]["t"] / 1000, UTC)
        end = datetime.fromtimestamp(rows[0]["t"] / 1000, UTC) + timedelta(hours=1)
        funding = adapter.get_funding_history("BTCUSD-PERP", start, end)
        assert len(funding) == 6
        assert [f.ts for f in funding] == sorted(f.ts for f in funding)
        assert len(fetcher.requests) == 4  # 3 full pages + 1 empty page
        assert all(f.interval == timedelta(hours=1) for f in funding)

    def test_pagination_without_progress_fails_closed(self) -> None:
        body = fixture_bytes("cryptocom/get_valuations_funding_hist_page.json")
        t = FakeTime()
        adapter = CryptoComAdapter(  # venue ignoring end_ts would loop forever without the guard
            fetcher=ScriptedFetcher(routes={f"{PUB}/get-valuations": lambda p: ok(body)}),
            clock=ManualClock(NOW),
            monotonic=t.monotonic,
            sleep=t.sleep,
            limits=replace(DEFAULT_LIMITS, max_valuation_points=6),
        )
        end = datetime.fromtimestamp(1792324800, UTC)  # newest fixture point is NOT < end
        with pytest.raises(ResponseFormatError, match="outside"):
            adapter.get_funding_history("BTCUSD-PERP", end - timedelta(hours=10), end)

    def test_spot_has_no_funding_history_and_makes_no_request(self) -> None:
        adapter, fetcher = make({})
        assert adapter.get_funding_history("BTC_USD", NOW - timedelta(days=1), NOW) == []
        assert fetcher.requests == []


def test_points_before_unaligned_start_are_filtered_not_fatal() -> None:
    """Observed live 2026-10-19: with an unaligned start_ts the venue also returns the funding
    point at the preceding hour boundary (start floored). Drop it; do not fail."""
    start = datetime(2026, 10, 19, 10, 1, 44, 495000, tzinfo=UTC)
    end = datetime(2026, 10, 19, 12, 30, tzinfo=UTC)
    rows = [
        {"t": int(datetime(2026, 10, 19, h, tzinfo=UTC).timestamp() * 1000), "v": "0.0001"}
        for h in (12, 11, 10)  # 10:00 is BEFORE start
    ]
    adapter, _ = make(
        {f"{PUB}/get-valuations": lambda p: ok(json.dumps({"code": 0, "result": {"data": rows}}))}
    )
    funding = adapter.get_funding_history("BTCUSD-PERP", start, end)
    assert [f.ts.hour for f in funding] == [11, 12]
