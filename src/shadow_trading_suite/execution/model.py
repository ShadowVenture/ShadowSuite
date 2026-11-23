"""Execution-layer value types: paper order terms, the execution state and its records.

All state is a JSON-serialisable `ExecState`; every command stores the changed entries
(`state_effects`) plus the state digest in its hash-chained audit record, so recovery rebuilds
open orders, queue positions, consumed liquidity, pending cancellations, checkpoints and
in-progress liquidations exactly (B2), and a restart can never resurrect consumed liquidity.
"""

import hashlib
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from shadow_trading_suite.core.decimal import Dec
from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.model import DomainModel, LeverageDec, NonNegativeDec, PositiveDec
from shadow_trading_suite.core.orders import Liquidity, OrderType, Side
from shadow_trading_suite.core.time import UtcDatetime
from shadow_trading_suite.storage.ledger import canonical_json

STATE_VERSION = 1


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderTerms(DomainModel):
    """How a risk-approved proposal is to be worked (E7). No stops, amends or OCO."""

    order_type: OrderType
    time_in_force: TimeInForce = TimeInForce.GTC
    limit_price: PositiveDec | None = None
    post_only: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.order_type is OrderType.MARKET:
            if self.limit_price is not None or self.post_only:
                raise ValueError("market orders take no limit price and cannot be post-only")
            if self.time_in_force is not TimeInForce.IOC:
                raise ValueError("market orders are IOC")
        elif self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.post_only and self.time_in_force is not TimeInForce.GTC:
            raise ValueError("post-only orders rest (GTC)")
        return self


class Phase(StrEnum):
    PENDING = "pending"  # accepted; submit latency not yet elapsed
    RESTING = "resting"  # on the simulated book, filled only by the queue model


class ExecOrder(DomainModel):
    order_id: str
    request_id: str
    bot_id: str
    instrument_id: InstrumentId
    side: Side
    quantity: PositiveDec
    terms: OrderTerms
    reduce_only: bool
    leverage: LeverageDec
    contract_size: PositiveDec
    lot_size: PositiveDec
    is_spot: bool
    decision_mid: PositiveDec
    band: NonNegativeDec
    profile_id: str
    accepted_at: UtcDatetime
    effective_at: UtcDatetime
    expires_at: UtcDatetime | None = None
    phase: Phase = Phase.PENDING
    filled: NonNegativeDec = Decimal(0)
    queue_ahead: NonNegativeDec | None = None
    rested_at: UtcDatetime | None = None
    rest_seq: int | None = None
    cancel_effective_at: UtcDatetime | None = None
    fills: int = Field(default=0, ge=0)

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled

    @property
    def buy(self) -> bool:
        return self.side is Side.BUY


class Consumption(DomainModel):
    """Displayed size we consumed at one price level (E8). Valid until the venue changes that
    level's size or `replenish_ms` of market time passes, whichever comes first."""

    instrument_id: InstrumentId
    book_side: str = Field(pattern=r"^(bid|ask)$")
    price: PositiveDec
    level_quantity: PositiveDec  # displayed size when consumed (base units)
    consumed: PositiveDec  # base units
    ts: UtcDatetime  # venue market time
    book_event_id: str


class LiquidationStage(StrEnum):
    TRIGGERED = "triggered"  # waiting for the liquidation latency
    LIQUIDATING = "liquidating"  # executing over successive LIVE books


class ExecLiquidation(DomainModel):
    bot_id: str
    instrument_id: InstrumentId
    stage: LiquidationStage
    triggered_at: UtcDatetime
    effective_at: UtcDatetime
    fills: int = Field(default=0, ge=0)


class FundingQuote(DomainModel):
    rate: Dec
    ts: UtcDatetime
    interval_s: int = Field(ge=1)
    event_id: str


class MarkQuote(DomainModel):
    mark: PositiveDec
    ts: UtcDatetime
    event_id: str


class ExecState(DomainModel):
    version: int = STATE_VERSION
    seq: int = 0
    orders: dict[str, ExecOrder] = Field(default_factory=dict)
    consumed: dict[str, Consumption] = Field(default_factory=dict)
    checkpoints: dict[str, str] = Field(default_factory=dict)  # stream -> last event_id
    trades_seen: dict[str, tuple[str, ...]] = Field(default_factory=dict)  # bounded window
    market_time: dict[str, UtcDatetime] = Field(default_factory=dict)  # venue -> time
    down_since: dict[str, UtcDatetime] = Field(default_factory=dict)  # venue -> wall time
    blocked: dict[str, str] = Field(default_factory=dict)  # venue -> reason (E4)
    marks: dict[str, MarkQuote] = Field(default_factory=dict)
    mark_missing_since: dict[str, UtcDatetime] = Field(default_factory=dict)
    funding: dict[str, FundingQuote] = Field(default_factory=dict)
    funding_next: dict[str, UtcDatetime] = Field(default_factory=dict)
    funding_unsettled: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    liquidations: dict[str, ExecLiquidation] = Field(default_factory=dict)
    decided: dict[str, str] = Field(default_factory=dict)  # request -> order id, "" refused
    counters: dict[str, int] = Field(default_factory=dict)  # e.g. rest order sequence

    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode()).hexdigest()


_MAPS = tuple(
    name
    for name, f in ExecState.model_fields.items()
    if f.default_factory is not None  # every dict field
)


def state_effects(before: ExecState, after: ExecState) -> dict[str, Any]:
    """Changed entries only (JSON form): scalars in full, maps as {key: value | None}, where
    None means removed (map values are therefore never None)."""
    b, a = before.model_dump(mode="json"), after.model_dump(mode="json")
    out: dict[str, Any] = {}
    for name in ExecState.model_fields:
        if name in _MAPS:
            keys = set(b[name]) | set(a[name])
            changed = {k: a[name].get(k) for k in sorted(keys) if b[name].get(k) != a[name].get(k)}
            if changed:
                out[name] = changed
        elif b[name] != a[name]:
            out[name] = a[name]
    return out


def apply_effects(state: ExecState, effects: dict[str, Any]) -> ExecState:
    data = state.model_dump(mode="json")
    for name, change in effects.items():
        if name not in ExecState.model_fields:
            raise ValueError(f"unknown execution state field {name!r}")
        if name in _MAPS:
            for key, value in change.items():
                if value is None:
                    data[name].pop(key, None)
                else:
                    data[name][key] = value
        else:
            data[name] = change
    return ExecState.model_validate(data)


def stream_key(instrument_id: InstrumentId, kind: str) -> str:
    return f"{instrument_id.venue.value}:{instrument_id.symbol}:{kind}"


def instrument_key(instrument_id: InstrumentId) -> str:
    return f"{instrument_id.venue.value}:{instrument_id.symbol}"


def position_key(bot_id: str, instrument_id: InstrumentId) -> str:
    return f"{bot_id}|{instrument_key(instrument_id)}"


def venue_key(venue: Venue) -> str:
    return venue.value


class FillRecord(DomainModel):
    """One simulated fill as audited (never a venue execution report)."""

    fill_id: str
    order_id: str
    bot_id: str
    instrument_id: InstrumentId
    side: Side
    price: PositiveDec
    quantity: PositiveDec
    liquidity: Liquidity
    fee_rate: NonNegativeDec
    ts: UtcDatetime
    book_event_id: str | None = None
    trade_id: str | None = None
    decision_mid: PositiveDec
    best_price: PositiveDec | None = None
    basis: str  # "book_walk" (taker over displayed depth) | "queue_model" (ASSUMPTION)
    simulated: Literal[True] = True


def ts_key(value: datetime) -> str:
    return value.isoformat()
