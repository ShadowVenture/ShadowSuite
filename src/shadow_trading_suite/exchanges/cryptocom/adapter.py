"""Crypto.com Exchange v1 — public market data only (`/exchange/v1/public/*`).

Docs: https://exchange-docs.crypto.com/exchange/v1/rest-ws/index.html
Public REST methods are limited to 100 requests/s per IP; we stay far below that.
"""

import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from shadow_trading_suite.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentId,
    InstrumentKind,
    Venue,
)
from shadow_trading_suite.core.market import (
    BookLevel,
    Candle,
    FundingRate,
    IndexPrice,
    MarkPrice,
    OrderBookSnapshot,
    Trade,
)
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.core.time import Clock, SystemClock
from shadow_trading_suite.exchanges import parsing as p
from shadow_trading_suite.exchanges.base import (
    CandleInterval,
    Capability,
    DiscoveryResult,
    RejectedInstrument,
    UnsupportedRequestError,
    VenueLimits,
    require_aligned,
)
from shadow_trading_suite.exchanges.client import PublicRestClient
from shadow_trading_suite.exchanges.cryptocom.details import parse_details
from shadow_trading_suite.exchanges.errors import ResponseFormatError, VenueApiError
from shadow_trading_suite.exchanges.http import Fetcher, UrllibFetcher
from shadow_trading_suite.exchanges.policy import EndpointPolicy
from shadow_trading_suite.exchanges.ratelimit import RateLimiter, TokenBucket

_PUBLIC = "/exchange/v1/public"

ENDPOINT_POLICY = EndpointPolicy(
    venue=Venue.CRYPTOCOM,
    base_url="https://api.crypto.com",
    path_patterns=(rf"{_PUBLIC}/get-(instruments|book|candlestick|trades|valuations)",),
    allowed_params=frozenset(
        {"instrument_name", "depth", "timeframe", "count", "start_ts", "end_ts", "valuation_type"}
    ),
)

RATE_PER_S = 20.0  # documented limit: 100/s per IP
BURST = 10
# Verified 2026-10-19 (docs + live): candles start inclusive / end exclusive, max 300 per
# request; asking for more (or omitting `count`, default 25) is SILENTLY truncated to the
# newest rows, so `count` is always sent and bounded. Valuations: newest first, window
# honoured (end exclusive), 300 honoured per request; older pages via a smaller end_ts.
DEFAULT_LIMITS = VenueLimits(
    max_candles_per_request=300,
    max_trades_per_request=150,  # documented maximum
    max_book_depth=50,  # documented maximum
    max_valuation_points=300,
)
CAPABILITIES = frozenset(
    {
        Capability.INSTRUMENTS,
        Capability.CANDLES,
        Capability.TRADES,
        Capability.ORDER_BOOK,
        Capability.FUNDING_CURRENT,
        Capability.FUNDING_HISTORY,
        Capability.MARK_PRICE_HISTORY,
        Capability.INDEX_PRICE_HISTORY,
    }
)
MAX_VALUATION_WINDOW = timedelta(days=90)
MAX_VALUATION_PAGES = 1000
# Crypto.com perpetual funding settles hourly (confirmed by funding_hist spacing 2026-10-19).
FUNDING_INTERVAL = timedelta(hours=1)

_TIMEFRAME = {
    CandleInterval.M1: "1m",
    CandleInterval.M5: "5m",
    CandleInterval.M15: "15m",
    CandleInterval.M30: "30m",
    CandleInterval.H1: "1h",
    CandleInterval.H2: "2h",
    CandleInterval.H4: "4h",
    CandleInterval.H12: "12h",
    CandleInterval.D1: "1D",
}

_KIND = {
    "CCY_PAIR": InstrumentKind.SPOT,
    "PERPETUAL_SWAP": InstrumentKind.PERPETUAL,
    "FUTURE": InstrumentKind.DATED_FUTURE,
}

_ASSET_CLASS = {
    "DIGITAL_CURRENCIES": AssetClass.CRYPTO,
    "EQUITY": AssetClass.EQUITY,
    "EQUITY_IND": AssetClass.EQUITY,
    "PRE_IPO": AssetClass.EQUITY,
    "COMMODITIES": AssetClass.COMMODITY,
    "CURRENCIES": AssetClass.FX,
}


def parse_instrument(raw: Mapping[str, Any]) -> Instrument:
    symbol = p.text(raw.get("symbol"), "symbol")
    inst_type = raw.get("inst_type")
    kind = _KIND.get(str(inst_type))
    if kind is None:
        raise ResponseFormatError(f"unsupported inst_type {inst_type!r}")
    quote = p.text(raw.get("quote_ccy"), "quote_ccy")
    fields: dict[str, Any] = {
        "id": InstrumentId(venue=Venue.CRYPTOCOM, symbol=symbol),
        "kind": kind,
        "asset_class": _ASSET_CLASS.get(str(raw.get("product_type")), AssetClass.UNKNOWN),
        "base": p.text(raw.get("base_ccy"), "base_ccy"),
        "quote": quote,
        "tick_size": raw.get("price_tick_size"),
        "lot_size": raw.get("qty_tick_size"),
        "min_qty": raw.get("qty_tick_size"),
        "sub_venue": symbol.split("@", 1)[1] if "@" in symbol else None,
        "listed_tradable": raw.get("tradable") is True,
        "venue_details": parse_details(raw),
    }
    if kind is not InstrumentKind.SPOT:
        fields.update(
            settle=quote,
            contract_size=raw.get("contract_size"),
            published_max_leverage=p.opt_decimal(raw.get("max_leverage")),
        )
    if kind is InstrumentKind.DATED_FUTURE:
        fields["expiry"] = p.from_unix_ms(raw.get("expiry_timestamp_ms"), "expiry_timestamp_ms")
    return Instrument.model_validate(fields)


class CryptoComAdapter:
    def __init__(
        self,
        fetcher: Fetcher | None = None,
        clock: Clock | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        limiter: RateLimiter | None = None,
        limits: VenueLimits = DEFAULT_LIMITS,
    ) -> None:
        """Create ONE adapter per venue per process (see exchanges.service)."""
        self._clock = clock or SystemClock()
        self._limits = limits
        self._client = PublicRestClient(
            policy=ENDPOINT_POLICY,
            fetcher=fetcher or UrllibFetcher(),
            limiter=limiter or TokenBucket(RATE_PER_S, BURST, monotonic=monotonic, sleep=sleep),
            sleep=sleep,
        )

    @property
    def venue(self) -> Venue:
        return Venue.CRYPTOCOM

    @property
    def capabilities(self) -> frozenset[Capability]:
        return CAPABILITIES

    @property
    def limits(self) -> VenueLimits:
        return self._limits

    def _iid(self, symbol: str) -> InstrumentId:
        return InstrumentId(venue=Venue.CRYPTOCOM, symbol=symbol)

    def _result(self, method: str, params: dict[str, str | int] | None = None) -> Mapping[str, Any]:
        envelope = p.obj(self._client.get_json(f"{_PUBLIC}/{method}", params), method)
        code = envelope.get("code")
        if not isinstance(code, int) or isinstance(code, bool):
            raise ResponseFormatError(f"{method}: missing response code")
        if code != 0:
            raise VenueApiError(f"{method}: venue error {code}: {envelope.get('message', '')}")
        return p.obj(envelope.get("result"), f"{method} result")

    # -- discovery -------------------------------------------------------------------------

    def list_instruments(self) -> DiscoveryResult:
        instruments: list[Instrument] = []
        rejected: list[RejectedInstrument] = []
        for raw in p.arr(self._result("get-instruments").get("data"), "instruments"):
            symbol = str(raw.get("symbol", "?")) if isinstance(raw, Mapping) else "?"
            try:
                instruments.append(parse_instrument(p.obj(raw, "instrument")))
            except (ResponseFormatError, ValueError) as exc:
                rejected.append(RejectedInstrument(symbol=symbol, reason=str(exc)[:300]))
        return DiscoveryResult(Venue.CRYPTOCOM, tuple(instruments), tuple(rejected))

    # -- market data -----------------------------------------------------------------------

    def get_candles(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        timeframe = _TIMEFRAME.get(interval)
        if timeframe is None:
            raise UnsupportedRequestError(f"crypto.com does not support {interval.value} candles")
        count = require_aligned(start, end, interval)
        maximum = self._limits.max_candles_per_request
        if not 1 <= count <= maximum:
            raise UnsupportedRequestError(f"window must cover 1..{maximum} candles, got {count}")
        result = self._result(
            "get-candlestick",
            {
                "instrument_name": symbol,
                "timeframe": timeframe,
                "start_ts": p.to_unix_ms(start),
                "end_ts": p.to_unix_ms(end),
                "count": count,
            },
        )
        now = self._clock.now()
        candles: list[Candle] = []
        for raw in p.arr(result.get("data"), "candles"):
            row = p.obj(raw, "candle")
            candle = p.build(
                Candle,
                "candle",
                instrument_id=self._iid(symbol),
                interval=interval.duration,
                open_time=p.from_unix_ms(row.get("t"), "t"),
                open=row.get("o"),
                high=row.get("h"),
                low=row.get("l"),
                close=row.get("c"),
                volume=row.get("v"),
            )
            if start <= candle.open_time < end and candle.close_time <= now:
                candles.append(candle)
        return sorted(candles, key=lambda c: c.open_time)

    def get_order_book(self, symbol: str, depth: int) -> OrderBookSnapshot:
        if not 1 <= depth <= self._limits.max_book_depth:
            raise UnsupportedRequestError(f"depth must be 1..{self._limits.max_book_depth}")
        result = self._result("get-book", {"instrument_name": symbol, "depth": depth})
        data = p.arr(result.get("data"), "book data")
        if len(data) != 1:
            raise ResponseFormatError(f"expected one book snapshot, got {len(data)}")
        book = p.obj(data[0], "book")

        def levels(side: str) -> tuple[BookLevel, ...]:
            out = []
            for row in p.arr(book.get(side), side):
                level = p.arr(row, side)
                if len(level) < 2:
                    raise ResponseFormatError(f"{side}: malformed level")
                out.append(p.build(BookLevel, side, price=level[0], quantity=level[1]))
            return tuple(out)

        return p.build(
            OrderBookSnapshot,
            "order book",
            instrument_id=self._iid(symbol),
            ts=p.from_unix_ms(book.get("t"), "t"),
            bids=levels("bids"),
            asks=levels("asks"),
        )

    def get_recent_trades(self, symbol: str, limit: int) -> list[Trade]:
        if not 1 <= limit <= self._limits.max_trades_per_request:
            raise UnsupportedRequestError(f"limit must be 1..{self._limits.max_trades_per_request}")
        result = self._result("get-trades", {"instrument_name": symbol, "count": limit})

        def side(value: Any) -> Side:
            try:
                return Side(str(value).lower())  # documented as the taker side
            except ValueError as exc:
                raise ResponseFormatError(f"unknown trade side {value!r}") from exc

        trades = [
            p.build(
                Trade,
                "trade",
                instrument_id=self._iid(symbol),
                trade_id=p.text(row.get("d"), "d"),
                ts=p.from_unix_ms(row.get("t"), "t"),
                price=row.get("p"),
                quantity=row.get("q"),
                aggressor=side(row.get("s")),
            )
            for row in (p.obj(r, "trade") for r in p.arr(result.get("data"), "trades"))
        ]
        return sorted(trades, key=lambda t: t.ts)

    def get_funding_rate(self, symbol: str) -> FundingRate | None:
        if not symbol.endswith("-PERP"):
            return None
        result = self._result(
            "get-valuations",
            {"instrument_name": symbol, "valuation_type": "funding_hist", "count": 1},
        )
        rows = [p.obj(r, "valuation") for r in p.arr(result.get("data"), "valuations")]
        if not rows:
            return None
        latest = max(rows, key=lambda r: p.integer(r.get("t"), "t"))
        rate = p.opt_decimal(latest.get("v"))
        if rate is None:
            raise ResponseFormatError("funding value missing")
        return p.build(
            FundingRate,
            "funding rate",
            instrument_id=self._iid(symbol),
            ts=p.from_unix_ms(latest.get("t"), "t"),
            rate=rate,
            interval=FUNDING_INTERVAL,
        )

    # -- valuation history -----------------------------------------------------------------

    def _valuations(
        self, symbol: str, valuation_type: str, start: datetime, end: datetime
    ) -> tuple[list[tuple[int, Any]], str | None]:
        """All points in [start, end), paging backwards (newest first per page)."""
        if end <= start or end - start > MAX_VALUATION_WINDOW:
            raise UnsupportedRequestError(f"window must be within {MAX_VALUATION_WINDOW}")
        page_size = self._limits.max_valuation_points
        start_ms, page_end = p.to_unix_ms(start), p.to_unix_ms(end)
        points: dict[int, Any] = {}
        name: str | None = None
        for _ in range(MAX_VALUATION_PAGES):
            result = self._result(
                "get-valuations",
                {
                    "instrument_name": symbol,
                    "valuation_type": valuation_type,
                    "start_ts": start_ms,
                    "end_ts": page_end,
                    "count": page_size,
                },
            )
            name = p.opt_text(result.get("instrument_name")) or name
            rows = [p.obj(r, "valuation") for r in p.arr(result.get("data"), "valuations")]
            page = [(p.integer(r.get("t"), "t"), r.get("v")) for r in rows]
            if any(t >= page_end for t, _ in page):
                # The venue ignored end_ts: paging backwards could never progress.
                raise ResponseFormatError(f"{valuation_type}: point outside requested window")
            # Observed: an unaligned start_ts is floored by the venue, so a point slightly
            # before `start` can be included. It is outside the request and simply dropped.
            points.update((t, v) for t, v in page if t >= start_ms)
            if len(page) < page_size:
                return sorted(points.items()), name
            page_end = min(t for t, _ in page)  # strictly older next page; always progresses
        raise ResponseFormatError(f"{valuation_type}: exceeded {MAX_VALUATION_PAGES} pages")

    def get_funding_history(self, symbol: str, start: datetime, end: datetime) -> list[FundingRate]:
        if not symbol.endswith("-PERP"):
            return []
        points, _ = self._valuations(symbol, "funding_hist", start, end)
        return [
            p.build(
                FundingRate,
                "funding rate",
                instrument_id=self._iid(symbol),
                ts=p.from_unix_ms(t, "t"),
                rate=p.opt_decimal(v),
                interval=FUNDING_INTERVAL,
            )
            for t, v in points
        ]

    def get_mark_prices(self, symbol: str, start: datetime, end: datetime) -> list[MarkPrice]:
        points, _ = self._valuations(symbol, "mark_price", start, end)
        return [
            p.build(
                MarkPrice,
                "mark price",
                instrument_id=self._iid(symbol),
                ts=p.from_unix_ms(t, "t"),
                mark=p.opt_decimal(v),
            )
            for t, v in points
        ]

    def get_index_prices(self, symbol: str, start: datetime, end: datetime) -> list[IndexPrice]:
        points, index_symbol = self._valuations(symbol, "index_price", start, end)
        return [
            p.build(
                IndexPrice,
                "index price",
                instrument_id=self._iid(symbol),
                ts=p.from_unix_ms(t, "t"),
                value=p.opt_decimal(v),
                index_symbol=index_symbol,
            )
            for t, v in points
        ]
