"""Coinbase Advanced Trade — public market data only (`/api/v3/brokerage/market/*`).

Docs: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/rest-api
Public endpoints need no authentication; responses are cached ~1s by the venue.
"""

import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
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
from shadow_trading_suite.exchanges.coinbase.details import parse_details
from shadow_trading_suite.exchanges.errors import ResponseFormatError
from shadow_trading_suite.exchanges.http import Fetcher, UrllibFetcher
from shadow_trading_suite.exchanges.policy import EndpointPolicy
from shadow_trading_suite.exchanges.ratelimit import RateLimiter, TokenBucket

_MARKET = "/api/v3/brokerage/market"
_PRODUCT = r"[A-Z0-9][A-Z0-9\-]{0,63}"

ENDPOINT_POLICY = EndpointPolicy(
    venue=Venue.COINBASE,
    base_url="https://api.coinbase.com",
    path_patterns=(
        r"/api/v3/brokerage/time",
        rf"{_MARKET}/products",
        rf"{_MARKET}/products/{_PRODUCT}",
        rf"{_MARKET}/products/{_PRODUCT}/candles",
        rf"{_MARKET}/products/{_PRODUCT}/ticker",
        rf"{_MARKET}/product_book",
    ),
    allowed_params=frozenset(
        {
            "product_type",
            "contract_expiry_type",
            "limit",
            "product_id",
            "granularity",
            "start",
            "end",
        }
    ),
)

# The venue does not document a public REST limit; stay well below typical per-IP limits.
RATE_PER_S = 5.0
BURST = 5
# Candle limit verified 2026-10-19 against docs AND live behaviour: start and end are BOTH
# inclusive; 350 buckets is the maximum; 351 returns HTTP 400 "should be less than 350",
# except a window of exactly 350 intervals (351 buckets) which returns 200 with the OLDEST
# candle silently dropped. We therefore send end = open time of the last wanted bucket.
DEFAULT_LIMITS = VenueLimits(
    max_candles_per_request=350,
    max_trades_per_request=100,  # conservative; not documented
    max_book_depth=500,  # conservative; not documented
)
CAPABILITIES = frozenset(
    {
        Capability.INSTRUMENTS,
        Capability.CANDLES,
        Capability.TRADES,
        Capability.ORDER_BOOK,
        Capability.FUNDING_CURRENT,
    }
)
LISTING_LIMIT = 10_000

_GRANULARITY = {
    CandleInterval.M1: "ONE_MINUTE",
    CandleInterval.M5: "FIVE_MINUTE",
    CandleInterval.M15: "FIFTEEN_MINUTE",
    CandleInterval.M30: "THIRTY_MINUTE",
    CandleInterval.H1: "ONE_HOUR",
    CandleInterval.H2: "TWO_HOUR",
    CandleInterval.H4: "FOUR_HOUR",
    CandleInterval.H6: "SIX_HOUR",
    CandleInterval.D1: "ONE_DAY",
}

_FUTURES_ASSET_CLASS = {
    "FUTURES_ASSET_TYPE_CRYPTO": AssetClass.CRYPTO,
    "FUTURES_ASSET_TYPE_STOCKS": AssetClass.EQUITY,
    "FUTURES_ASSET_TYPE_METALS": AssetClass.COMMODITY,
    "FUTURES_ASSET_TYPE_ENERGY": AssetClass.COMMODITY,
    "FUTURES_ASSET_TYPE_COMMODITIES": AssetClass.COMMODITY,
    "FUTURES_ASSET_TYPE_AGRICULTURE": AssetClass.COMMODITY,
    "FUTURES_ASSET_TYPE_INDICES": AssetClass.INDEX,
}


def _blocked(product: Mapping[str, Any]) -> bool:
    flags = ("is_disabled", "trading_disabled", "cancel_only", "view_only")
    return any(product.get(f) is not False for f in flags)


def _positive_or_none(value: Any) -> Decimal | None:
    d = p.opt_decimal(value)
    return d if d is not None and d > 0 else None


def _common(product: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": InstrumentId(venue=Venue.COINBASE, symbol=p.text(product.get("product_id"), "id")),
        "quote": p.text(product.get("quote_currency_id"), "quote_currency_id"),
        "tick_size": product.get("price_increment"),
        "lot_size": product.get("base_increment"),
        "min_qty": product.get("base_min_size"),
        "min_notional": _positive_or_none(product.get("quote_min_size")),
        "sub_venue": p.opt_text(product.get("product_venue")),
        "venue_details": parse_details(product),
    }


def parse_product(product: Mapping[str, Any]) -> Instrument:
    product_type = product.get("product_type")
    if product_type == "SPOT":
        return Instrument.model_validate(
            {
                **_common(product),
                "kind": InstrumentKind.SPOT,
                "base": p.text(product.get("base_currency_id"), "base_currency_id"),
                "listed_tradable": product.get("status") == "online" and not _blocked(product),
            }
        )
    if product_type == "FUTURE":
        details = p.obj(product.get("future_product_details"), "future_product_details")
        expiry_type = details.get("contract_expiry_type")
        if expiry_type == "PERPETUAL":
            kind, expiry = InstrumentKind.PERPETUAL, None
        elif expiry_type == "EXPIRING":
            kind = InstrumentKind.DATED_FUTURE
            expiry = p.from_iso(details.get("contract_expiry"), "contract_expiry")
        else:
            raise ResponseFormatError(f"unknown contract_expiry_type {expiry_type!r}")
        common = _common(product)
        future = common["venue_details"].future
        return Instrument.model_validate(
            {
                **common,
                "kind": kind,
                "asset_class": _FUTURES_ASSET_CLASS.get(
                    str(details.get("futures_asset_type")), AssetClass.UNKNOWN
                ),
                "base": p.opt_text(details.get("contract_root_unit"))
                or p.text(product.get("base_currency_id"), "base"),
                "settle": common["quote"],
                "contract_size": details.get("contract_size"),
                # Only a venue-published figure; never derived or defaulted.
                "published_max_leverage": future.published_max_leverage if future else None,
                "expiry": expiry,
                "listed_tradable": not _blocked(product),
            }
        )
    raise ResponseFormatError(f"unsupported product_type {product_type!r}")


class CoinbaseAdapter:
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
        return Venue.COINBASE

    @property
    def capabilities(self) -> frozenset[Capability]:
        return CAPABILITIES

    @property
    def limits(self) -> VenueLimits:
        return self._limits

    def _iid(self, symbol: str) -> InstrumentId:
        return InstrumentId(venue=Venue.COINBASE, symbol=symbol)

    # -- discovery -------------------------------------------------------------------------

    def _listing(self, params: dict[str, str | int]) -> list[Any]:
        data = p.obj(self._client.get_json(f"{_MARKET}/products", params), "products response")
        pagination = data.get("pagination")
        if isinstance(pagination, Mapping) and pagination.get("has_next"):
            raise ResponseFormatError("product listing is paginated: incomplete listing refused")
        return p.arr(data.get("products"), "products")

    def list_instruments(self) -> DiscoveryResult:
        listings = [
            {"product_type": "SPOT"},
            {"product_type": "FUTURE", "contract_expiry_type": "PERPETUAL"},
            {"product_type": "FUTURE", "contract_expiry_type": "EXPIRING"},
        ]
        instruments: list[Instrument] = []
        rejected: list[RejectedInstrument] = []
        for params in listings:
            for raw in self._listing({**params, "limit": LISTING_LIMIT}):
                symbol = str(raw.get("product_id", "?")) if isinstance(raw, Mapping) else "?"
                try:
                    instruments.append(parse_product(p.obj(raw, "product")))
                except (ResponseFormatError, ValueError) as exc:
                    rejected.append(RejectedInstrument(symbol=symbol, reason=str(exc)[:300]))
        return DiscoveryResult(Venue.COINBASE, tuple(instruments), tuple(rejected))

    # -- market data -----------------------------------------------------------------------

    def get_candles(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        granularity = _GRANULARITY.get(interval)
        if granularity is None:
            raise UnsupportedRequestError(f"coinbase does not support {interval.value} candles")
        count = require_aligned(start, end, interval)
        maximum = self._limits.max_candles_per_request
        if not 1 <= count <= maximum:
            raise UnsupportedRequestError(f"window must cover 1..{maximum} candles, got {count}")
        data = p.obj(
            self._client.get_json(
                f"{_MARKET}/products/{symbol}/candles",
                {
                    "granularity": granularity,
                    "start": p.to_unix_s(start),
                    "end": p.to_unix_s(end - interval.duration),  # inclusive last bucket
                    "limit": count,
                },
            ),
            "candles response",
        )
        now = self._clock.now()
        candles: list[Candle] = []
        for raw in p.arr(data.get("candles"), "candles"):
            row = p.obj(raw, "candle")
            candle = p.build(
                Candle,
                "candle",
                instrument_id=self._iid(symbol),
                interval=interval.duration,
                open_time=p.from_unix_s(row.get("start"), "start"),
                open=row.get("open"),
                high=row.get("high"),
                low=row.get("low"),
                close=row.get("close"),
                volume=row.get("volume"),
            )
            if start <= candle.open_time < end and candle.close_time <= now:
                candles.append(candle)
        return sorted(candles, key=lambda c: c.open_time)

    def get_order_book(self, symbol: str, depth: int) -> OrderBookSnapshot:
        if not 1 <= depth <= self._limits.max_book_depth:
            raise UnsupportedRequestError(f"depth must be 1..{self._limits.max_book_depth}")
        data = p.obj(
            self._client.get_json(
                f"{_MARKET}/product_book", {"product_id": symbol, "limit": depth}
            ),
            "product_book response",
        )
        book = p.obj(data.get("pricebook"), "pricebook")

        def levels(side: str) -> tuple[BookLevel, ...]:
            rows = [p.obj(r, side) for r in p.arr(book.get(side), side)]
            return tuple(
                p.build(BookLevel, side, price=r.get("price"), quantity=r.get("size")) for r in rows
            )

        return p.build(
            OrderBookSnapshot,
            "order book",
            instrument_id=self._iid(symbol),
            ts=p.from_iso(book.get("time"), "time"),
            bids=levels("bids"),
            asks=levels("asks"),
        )

    def get_recent_trades(self, symbol: str, limit: int) -> list[Trade]:
        if not 1 <= limit <= self._limits.max_trades_per_request:
            raise UnsupportedRequestError(f"limit must be 1..{self._limits.max_trades_per_request}")
        data = p.obj(
            self._client.get_json(f"{_MARKET}/products/{symbol}/ticker", {"limit": limit}),
            "ticker response",
        )
        trades = [
            p.build(
                Trade,
                "trade",
                instrument_id=self._iid(symbol),
                trade_id=p.text(row.get("trade_id"), "trade_id"),
                ts=p.from_iso(row.get("time"), "time"),
                price=row.get("price"),
                quantity=row.get("size"),
                # Coinbase's public `side` semantics (maker vs taker) are not documented
                # unambiguously; leave the aggressor unknown rather than guess.
                aggressor=None,
            )
            for row in (p.obj(r, "trade") for r in p.arr(data.get("trades"), "trades"))
        ]
        return sorted(trades, key=lambda t: t.ts)

    def get_funding_rate(self, symbol: str) -> FundingRate | None:
        product = p.obj(self._client.get_json(f"{_MARKET}/products/{symbol}"), "product")
        details = product.get("future_product_details")
        if product.get("product_type") != "FUTURE" or not isinstance(details, Mapping):
            return None
        rate = p.opt_decimal(details.get("funding_rate"))
        if rate is None or not details.get("funding_time"):
            return None
        interval_raw = p.text(details.get("funding_interval"), "funding_interval")
        if not interval_raw.endswith("s") or not interval_raw[:-1].isdigit():
            raise ResponseFormatError(f"unexpected funding_interval {interval_raw!r}")
        return p.build(
            FundingRate,
            "funding rate",
            instrument_id=self._iid(symbol),
            ts=p.from_iso(details.get("funding_time"), "funding_time"),
            rate=rate,
            interval=timedelta(seconds=int(interval_raw[:-1])),
        )

    def _unsupported(self, what: str) -> UnsupportedRequestError:
        return UnsupportedRequestError(f"coinbase public API does not publish {what} history")

    def get_funding_history(self, symbol: str, start: datetime, end: datetime) -> list[FundingRate]:
        raise self._unsupported("funding")

    def get_mark_prices(self, symbol: str, start: datetime, end: datetime) -> list[MarkPrice]:
        raise self._unsupported("mark price")

    def get_index_prices(self, symbol: str, start: datetime, end: datetime) -> list[IndexPrice]:
        raise self._unsupported("index price")
