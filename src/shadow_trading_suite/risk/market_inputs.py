"""Build risk-engine MarketInputs from ONE validated MarketSnapshot (fail closed).

Book and mark come from `MarketSnapshot.require()` (missing, non-LIVE or stale -> refuse).
Coinbase publishes no mark price: the book mid is used and labelled `mid`. Instruments that
pay funding need fresh funding (WebSocket on Crypto.com; a validated REST snapshot passed as
`funding` for Coinbase FCM); otherwise the request is refused. Slippage is a depth walk of the
real book for the order size; insufficient depth is refused.
"""

from datetime import timedelta
from decimal import Decimal

from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import Instrument, InstrumentKind
from shadow_trading_suite.core.market import FundingRate, MarkPrice, OrderBookSnapshot
from shadow_trading_suite.exchanges.coinbase.details import CoinbaseDetails
from shadow_trading_suite.risk.engine import MarketInputs
from shadow_trading_suite.streaming.events import StreamKind
from shadow_trading_suite.streaming.state import MarketSnapshot, StreamKey


def book_slippage(book: OrderBookSnapshot, *, buy: bool, size: Decimal, floor: Decimal) -> Decimal:
    """Relative cost of walking the book for `size` (base units) versus the best price."""
    levels = book.asks if buy else book.bids
    if not levels:
        raise DataQualityError(f"{book.instrument_id}: empty book side")
    remaining, cost = size, Decimal(0)
    for level in levels:
        take = min(remaining, level.quantity)
        cost += take * level.price
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        raise DataQualityError(f"{book.instrument_id}: insufficient book depth for {size}")
    best = levels[0].price
    average = cost / size
    slip = average / best - 1 if buy else 1 - average / best
    return max(floor, slip)


def _pays_funding(inst: Instrument) -> bool:
    if inst.kind is InstrumentKind.PERPETUAL:
        return True
    details = inst.venue_details
    return (
        isinstance(details, CoinbaseDetails)
        and details.future is not None
        and details.future.funding_interval is not None
    )


def market_inputs_from_snapshot(
    snapshot: MarketSnapshot,
    inst: Instrument,
    *,
    quantity: Decimal,
    sigma: Decimal,
    eur_per_quote: Decimal,
    max_age: timedelta,
    min_slippage: Decimal,
    funding: FundingRate | None = None,
) -> MarketInputs:
    book_event = snapshot.require(StreamKey(inst.id, StreamKind.BOOK), max_age)
    book = book_event.payload
    if not isinstance(book, OrderBookSnapshot) or book.best_bid is None or book.best_ask is None:
        raise DataQualityError(f"{inst.id}: no two-sided book")
    size = quantity * inst.contract_size
    slippage = max(
        book_slippage(book, buy=True, size=size, floor=min_slippage),
        book_slippage(book, buy=False, size=size, floor=min_slippage),
    )
    mark_key = StreamKey(inst.id, StreamKind.MARK)
    if inst.kind is not InstrumentKind.SPOT and mark_key in snapshot.status:
        mark_event = snapshot.require(mark_key, max_age)
        if not isinstance(mark_event.payload, MarkPrice):
            raise DataQualityError(f"{inst.id}: invalid mark")
        mark, source = mark_event.payload.mark, "venue_mark"
    else:
        mark, source = (book.best_bid.price + book.best_ask.price) / 2, "mid"
    rate: Decimal | None = None
    interval_s: int | None = None
    if _pays_funding(inst):
        f = funding
        if f is None:
            fkey = StreamKey(inst.id, StreamKind.FUNDING)
            if fkey not in snapshot.status:
                raise DataQualityError(f"{inst.id}: funding required but not available")
            payload = snapshot.require(fkey, timedelta(hours=2)).payload
            f = payload if isinstance(payload, FundingRate) else None
        if f is None or snapshot.as_of - f.ts > 2 * f.interval:
            raise DataQualityError(f"{inst.id}: funding missing or stale")
        rate, interval_s = f.rate, int(f.interval.total_seconds())
    return MarketInputs(
        instrument_id=inst.id,
        snapshot_version=snapshot.version,
        as_of=book_event.received_at,
        bid=book.best_bid.price,
        ask=book.best_ask.price,
        mark=mark,
        mark_source=source,
        funding_rate=rate,
        funding_interval_s=interval_s,
        sigma=sigma,
        slippage=slippage,
        eur_per_quote=eur_per_quote,
    )
