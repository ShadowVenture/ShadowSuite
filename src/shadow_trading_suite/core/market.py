"""Normalised public market-data records."""

from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Self

from pydantic import Field, field_validator, model_validator

from shadow_trading_suite.core.decimal import Dec
from shadow_trading_suite.core.instrument import InstrumentId
from shadow_trading_suite.core.model import DomainModel, NonNegativeDec, PositiveDec
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.core.time import UtcDatetime


class Candle(DomainModel):
    instrument_id: InstrumentId
    interval: timedelta
    open_time: UtcDatetime
    open: PositiveDec
    high: PositiveDec
    low: PositiveDec
    close: PositiveDec
    volume: NonNegativeDec

    @field_validator("interval")
    @classmethod
    def _positive_interval(cls, v: timedelta) -> timedelta:
        if v <= timedelta(0):
            raise ValueError("interval must be positive")
        return v

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> Self:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be >= open, close and low")
        if self.low > min(self.open, self.close):
            raise ValueError("low must be <= open and close")
        return self

    @property
    def close_time(self) -> datetime:
        return self.open_time + self.interval


class Trade(DomainModel):
    instrument_id: InstrumentId
    trade_id: str = Field(min_length=1)
    ts: UtcDatetime
    price: PositiveDec
    quantity: PositiveDec
    aggressor: Side | None = None

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


class BookLevel(DomainModel):
    price: PositiveDec
    quantity: PositiveDec


class OrderBookSnapshot(DomainModel):
    """Bids sorted best (highest) first, asks best (lowest) first; never crossed."""

    instrument_id: InstrumentId
    ts: UtcDatetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    sequence: int | None = None

    @model_validator(mode="after")
    def _sorted_and_uncrossed(self) -> Self:
        bid_prices = [lvl.price for lvl in self.bids]
        ask_prices = [lvl.price for lvl in self.asks]
        if any(a <= b for a, b in pairwise(bid_prices)):
            raise ValueError("bids must be sorted strictly descending")
        if any(a >= b for a, b in pairwise(ask_prices)):
            raise ValueError("asks must be sorted strictly ascending")
        if bid_prices and ask_prices and bid_prices[0] >= ask_prices[0]:
            raise ValueError("order book is crossed or locked")
        return self

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread(self) -> Decimal | None:
        if not self.bids or not self.asks:
            return None
        return self.asks[0].price - self.bids[0].price


class FundingRate(DomainModel):
    """Perpetual funding rate per `interval`. Positive: longs pay shorts."""

    instrument_id: InstrumentId
    ts: UtcDatetime
    rate: Dec
    interval: timedelta
    next_funding_time: UtcDatetime | None = None


class MarkPrice(DomainModel):
    instrument_id: InstrumentId
    ts: UtcDatetime
    mark: PositiveDec
    index: PositiveDec | None = None


class IndexPrice(DomainModel):
    """Venue index price associated with an instrument (e.g. BTCUSD-PERP → BTCUSD-INDEX)."""

    instrument_id: InstrumentId
    ts: UtcDatetime
    value: PositiveDec
    index_symbol: str | None = None


class Ticker(DomainModel):
    """Last price and best bid/ask as published by a venue ticker. Fields may be absent."""

    instrument_id: InstrumentId
    ts: UtcDatetime
    last: PositiveDec | None = None
    bid: PositiveDec | None = None
    bid_qty: NonNegativeDec | None = None
    ask: PositiveDec | None = None
    ask_qty: NonNegativeDec | None = None

    @model_validator(mode="after")
    def _uncrossed(self) -> Self:
        if self.bid is not None and self.ask is not None and self.bid >= self.ask:
            raise ValueError("ticker bid must be below ask")
        return self
