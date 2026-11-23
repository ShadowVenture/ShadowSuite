"""The bot plugin contract (plugin API version 1).

A bot is an external plugin. The suite ships none. A plugin:

- describes itself with a `PluginManifest` (id, version, venue, the instruments it trades);
- receives its configuration once (`configure`), then a read-only `MarketView` per market
  update (`on_market`) and an `OrderUpdate` for each change of its own orders;
- answers `on_market` with a `Decision`: informational `Signal`s and `OrderRequest`s.

A plugin never talks to an exchange, the ledger or the execution engine. Every `OrderRequest`
goes through the host's central controls, in this order:

1. host checks (plugin state, own instruments only, request id, request rate);
2. the risk engine and the portfolio supervisor (per-bot limits, portfolio limits, kill state);
3. the PAPER execution engine (fills only on fresh live books, within depth and price band).

A refusal at any stage is returned to the plugin as a REJECTED `OrderUpdate` with a reason and
is never retried. The risk engine may REDUCE an order to the bot's limits (RESIZE); the accepted
quantity is in `OrderUpdate.quantity`. Nothing ever increases a request. There is no real order
path: the suite is paper-only.

Errors: an exception from any plugin callback moves the plugin to FAILED. The host then
requests the cancellation of its resting paper orders and calls nothing else on it except
`on_stop`. A stopped or failed plugin never submits again.

Plugins run IN-PROCESS with the host's privileges. The contract is an API boundary, not a
sandbox: install only plugins you trust (see SECURITY.md).
"""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import Field, model_validator

from shadow_trading_suite.core.decimal import Dec
from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.model import DomainModel
from shadow_trading_suite.core.orders import OrderType, Side
from shadow_trading_suite.core.time import UtcDatetime
from shadow_trading_suite.execution.model import TimeInForce

PLUGIN_API_VERSION = 1
PLUGIN_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
ConfigValue = str | int | bool | Decimal
PluginConfig = Mapping[str, ConfigValue]


class PluginConfigError(ValueError):
    """Raised by `configure` for a configuration the plugin cannot accept."""


class PluginManifest(DomainModel):
    plugin_id: str = Field(pattern=PLUGIN_ID_PATTERN)
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+([.-][0-9A-Za-z.]+)?$")
    api_version: Literal[1] = 1
    venue: Venue
    instruments: tuple[InstrumentId, ...] = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _own_venue(self) -> Self:
        if any(i.venue is not self.venue for i in self.instruments):
            raise ValueError("every instrument must be on the plugin's venue")
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError("duplicate instrument")
        return self


class Quote(DomainModel):
    """Best bid / ask from a fresh LIVE two-sided book (absent when there is none)."""

    instrument_id: InstrumentId
    bid: Dec
    ask: Dec
    as_of: UtcDatetime


class Holding(DomainModel):
    instrument_id: InstrumentId
    quantity: Dec


class OpenOrder(DomainModel):
    order_id: str
    request_id: str
    instrument_id: InstrumentId
    side: Side
    quantity: Dec
    filled: Dec
    state: str


class MarketView(DomainModel):
    """What a plugin may see: its own instruments, holdings and orders only (read-only)."""

    plugin_id: str
    now: UtcDatetime
    quotes: dict[str, Quote]  # key: str(InstrumentId) ("venue:symbol"); missing = no fresh book
    holdings: tuple[Holding, ...]
    open_orders: tuple[OpenOrder, ...]
    last_trade_price: dict[str, Dec]


class SignalKind(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    INFO = "info"


class Signal(DomainModel):
    """An informational signal: recorded in the status, never executed by itself."""

    instrument_id: InstrumentId
    kind: SignalKind
    strength: Dec = Field(default=Decimal(0), ge=-1, le=1)
    note: str = Field(default="", max_length=200)


class OrderRequest(DomainModel):
    """A request for a PAPER order. It may be refused; the risk engine may REDUCE its quantity
    to the bot's limits (reported in `OrderUpdate.quantity`), never increase it. Nothing is
    retried."""

    request_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")  # unique per plugin
    instrument_id: InstrumentId
    side: Side
    quantity: Dec = Field(gt=0)
    order_type: OrderType = OrderType.LIMIT
    limit_price: Dec | None = Field(default=None, gt=0)
    time_in_force: TimeInForce | None = None  # None: IOC for market, GTC for limit orders
    post_only: bool = False
    reduce_only: bool = False


class Decision(DomainModel):
    signals: tuple[Signal, ...] = ()
    orders: tuple[OrderRequest, ...] = Field(default=(), max_length=16)
    cancel_order_ids: tuple[str, ...] = Field(default=(), max_length=16)


class OrderStatus(StrEnum):
    REJECTED = "rejected"  # refused by host checks, risk or execution (see `reason`)
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    CANCEL_REQUESTED = "cancel_requested"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class OrderUpdate(DomainModel):
    plugin_id: str
    request_id: str
    order_id: str | None
    status: OrderStatus
    quantity: Dec | None = None  # the ACCEPTED quantity (risk may reduce it, never increase it)
    filled: Dec = Decimal(0)
    reason: str = ""
    at: UtcDatetime


class StopReason(StrEnum):
    REQUESTED = "requested"  # operator or host stop
    ERROR = "error"  # the plugin raised
    SHUTDOWN = "shutdown"  # host shutdown


@runtime_checkable
class BotPlugin(Protocol):
    """Implement this to write a bot. All callbacks are synchronous and must return quickly."""

    manifest: PluginManifest

    def configure(self, config: PluginConfig) -> None:
        """Validate and keep the configuration; raise `PluginConfigError` to refuse it."""

    def on_start(self, now: datetime) -> None:
        """Called once after the host started, before the first `on_market`."""

    def on_market(self, view: MarketView) -> Decision:
        """Called after market updates for the plugin's instruments."""
        ...

    def on_order_update(self, update: OrderUpdate) -> None:
        """Called for every state change of the plugin's own requests and orders."""

    def on_stop(self, reason: StopReason) -> None:
        """Called once when the plugin is stopped (also after an error)."""
