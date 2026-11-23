"""Pure matching rules (no I/O, no clock): book walks over the shared liquidity view, limit
crossing, IOC/FOK/post-only and the conservative queue model (E2).

Fills never exceed the published, still-unconsumed displayed depth; nothing is extrapolated.
Quantities: book levels are in base units; orders are in contracts (base / contract_size),
floored to the lot size.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.market import BookLevel, OrderBookSnapshot, Trade
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.execution.model import Consumption, ExecOrder, instrument_key


def consumption_key(instrument_id: InstrumentId, book_side: str, price: Decimal) -> str:
    return f"{instrument_key(instrument_id)}:{book_side}:{price.normalize():f}"


def live_consumption(
    c: Consumption | None, level: BookLevel, now: datetime, replenish: timedelta
) -> Decimal:
    """Base units of this level already used by paper orders (0 once replenished/updated)."""
    if c is None or c.level_quantity != level.quantity or now - c.ts >= replenish:
        return Decimal(0)
    return min(c.consumed, level.quantity)


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(ROUND_FLOOR) * step


@dataclass(frozen=True)
class Take:
    price: Decimal
    quantity: Decimal  # contracts
    base: Decimal  # base units consumed
    level_quantity: Decimal


@dataclass(frozen=True)
class Walk:
    takes: tuple[Take, ...]
    stop: str  # filled | limit | price_band | depth

    @property
    def quantity(self) -> Decimal:
        return sum((t.quantity for t in self.takes), Decimal(0))


def walk(
    book: OrderBookSnapshot,
    consumed: Mapping[str, Consumption],
    *,
    buy: bool,
    quantity: Decimal,
    contract_size: Decimal,
    lot_size: Decimal,
    limit: Decimal | None,
    band: tuple[Decimal, Decimal] | None,
    now: datetime,
    replenish: timedelta,
) -> Walk:
    levels = book.asks if buy else book.bids
    side = "ask" if buy else "bid"
    remaining = quantity
    takes: list[Take] = []
    for level in levels:
        if remaining <= 0:
            break
        if limit is not None and (level.price > limit if buy else level.price < limit):
            return Walk(tuple(takes), "limit")
        if band is not None and not band[0] <= level.price <= band[1]:
            return Walk(tuple(takes), "price_band")
        key = consumption_key(book.instrument_id, side, level.price)
        used = live_consumption(consumed.get(key), level, now, replenish)
        contracts = floor_to((level.quantity - used) / contract_size, lot_size)
        take = min(remaining, contracts)
        if take > 0:
            takes.append(Take(level.price, take, take * contract_size, level.quantity))
            remaining -= take
    return Walk(tuple(takes), "filled" if remaining <= 0 else "depth")


def crosses(book: OrderBookSnapshot, *, buy: bool, limit: Decimal) -> bool:
    """Would a limit order at `limit` take liquidity right now?"""
    if buy:
        return book.best_ask is not None and book.best_ask.price <= limit
    return book.best_bid is not None and book.best_bid.price >= limit


def displayed_at(book: OrderBookSnapshot, *, buy: bool, price: Decimal) -> Decimal:
    for level in book.bids if buy else book.asks:
        if level.price == price:
            return level.quantity
    return Decimal(0)


def band_bounds(mid: Decimal, band: Decimal) -> tuple[Decimal, Decimal]:
    return mid * (1 - band), mid * (1 + band)


def two_sided_uncrossed(book: OrderBookSnapshot) -> bool:
    return (
        book.best_bid is not None
        and book.best_ask is not None
        and book.best_bid.price < book.best_ask.price
    )


@dataclass(frozen=True)
class QueueResult:
    fill: Decimal  # contracts at the order's limit price
    queue_ahead: Decimal  # base units
    trade_left: Decimal  # base units of the trade still available to later orders


def queue_step(order: ExecOrder, trade: Trade, available: Decimal, venue: Venue) -> QueueResult:
    """Conservative queue model (E2, ASSUMPTION).

    A resting order fills only from trades at or through its price: volume at the price first
    works off the queue ahead (displayed size when the order rested plus earlier paper orders),
    then fills; a trade through the price fills up to the traded volume. Crypto.com documents
    the taker side, so only opposite-side aggressors count. Coinbase's aggressor is unknown:
    at-price prints only reduce the queue ahead, and only trades through the price fill.
    """
    limit = order.terms.limit_price
    queue = order.queue_ahead or Decimal(0)
    if limit is None or available <= 0:
        return QueueResult(Decimal(0), queue, available)
    opposite = Side.SELL if order.buy else Side.BUY
    through = trade.price < limit if order.buy else trade.price > limit
    at_price = trade.price == limit
    if not (through or at_price):
        return QueueResult(Decimal(0), queue, available)
    known = venue is Venue.CRYPTOCOM
    if known and trade.aggressor is not opposite:
        return QueueResult(Decimal(0), queue, available)
    left = available
    if at_price:
        worked = min(queue, left)
        queue -= worked
        left -= worked
        if not known:  # Coinbase: never fill on an at-price print of unknown direction
            return QueueResult(Decimal(0), queue, left)
    contracts = floor_to(
        min(left, order.remaining * order.contract_size) / order.contract_size, order.lot_size
    )
    contracts = min(contracts, order.remaining)
    return QueueResult(contracts, queue, left - contracts * order.contract_size)
