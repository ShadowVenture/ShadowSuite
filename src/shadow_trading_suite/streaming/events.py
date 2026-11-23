"""Normalised real-time events and the control signals venue protocols emit."""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from shadow_trading_suite.core.instrument import InstrumentId
from shadow_trading_suite.core.market import (
    Candle,
    FundingRate,
    IndexPrice,
    MarkPrice,
    OrderBookSnapshot,
    Ticker,
    Trade,
)
from shadow_trading_suite.core.model import DomainModel
from shadow_trading_suite.core.time import UtcDatetime


class StreamKind(StrEnum):
    TRADES = "trades"
    BOOK = "book"
    TICKER = "ticker"
    CANDLES = "candles"
    MARK = "mark"
    INDEX = "index"
    FUNDING = "funding"
    ESTIMATED_FUNDING = "estimated_funding"


Payload = Trade | Ticker | OrderBookSnapshot | Candle | MarkPrice | IndexPrice | FundingRate


class StreamEvent(DomainModel):
    """One normalised market-data event.

    `event_id` is stable and deterministic (venue:kind:symbol:discriminator) so duplicates can
    be dropped. `exchange_ts` is the venue's timestamp; `received_at` the local receive time;
    `epoch` the connection generation it arrived on.
    """

    event_id: str = Field(min_length=1)
    kind: StreamKind
    instrument_id: InstrumentId
    exchange_ts: UtcDatetime
    received_at: UtcDatetime
    epoch: int = Field(ge=1)
    sequence: int | None = None
    payload: Payload
    candle_closed: bool | None = None


@dataclass(frozen=True)
class Subscription:
    instrument_id: InstrumentId
    kind: StreamKind
    venue_symbol: str | None = None  # e.g. BTCUSD-INDEX for an index stream of BTCUSD-PERP


# -- control signals ------------------------------------------------------------------------


@dataclass(frozen=True)
class Heartbeat:
    pass


@dataclass(frozen=True)
class HeartbeatReply:
    text: str


@dataclass(frozen=True)
class Ack:
    channel: str
    ok: bool
    message: str = ""


@dataclass(frozen=True)
class ConnectionGap:
    """The connection-level state can no longer be trusted: reconnect and resubscribe."""

    detail: str


@dataclass(frozen=True)
class BookSnapshotMsg:
    subscription: Subscription
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    exchange_ts: UtcDatetime
    sequence: int | None


@dataclass(frozen=True)
class BookDeltaMsg:
    subscription: Subscription
    changes: tuple[tuple[str, Decimal, Decimal], ...]
    exchange_ts: UtcDatetime
    sequence: int | None
    previous: int | None


Control = Heartbeat | HeartbeatReply | Ack | ConnectionGap | BookSnapshotMsg | BookDeltaMsg


@dataclass
class ParseResult:
    events: list[StreamEvent] = field(default_factory=list)
    controls: list[Control] = field(default_factory=list)
