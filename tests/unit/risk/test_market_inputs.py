"""Risk inputs come only from a validated snapshot; anything stale or thin fails closed."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import Instrument
from shadow_trading_suite.core.market import BookLevel, FundingRate, MarkPrice, OrderBookSnapshot
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.exchanges.coinbase.adapter import parse_product
from shadow_trading_suite.exchanges.cryptocom.adapter import parse_instrument
from shadow_trading_suite.risk.engine import MarketInputs
from shadow_trading_suite.risk.market_inputs import book_slippage, market_inputs_from_snapshot
from shadow_trading_suite.streaming.events import Payload, StreamEvent, StreamKind
from shadow_trading_suite.streaming.state import MarketState, StreamKey, StreamStatus
from tests.unit.exchanges.fakes import fixture_json

D = Decimal
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
PERP = parse_instrument(
    next(
        r
        for r in fixture_json("cryptocom/get_instruments.json")["result"]["data"]
        if r["symbol"] == "BTCUSD-PERP"
    )
)
CB_SPOT = parse_product(
    next(
        r
        for r in fixture_json("coinbase/products_spot.json")["products"]
        if r["product_id"] == "BTC-USD"
    )
)


def book(inst: Instrument, ts: datetime) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        instrument_id=inst.id,
        ts=ts,
        bids=(
            BookLevel(price=D(81999), quantity=D("0.5")),
            BookLevel(price=D(81990), quantity=D(2)),
        ),
        asks=(
            BookLevel(price=D(82001), quantity=D("0.5")),
            BookLevel(price=D(82010), quantity=D(2)),
        ),
    )


def put(
    state: MarketState, inst: Instrument, kind: StreamKind, payload: Payload, ts: datetime
) -> None:
    key = StreamKey(inst.id, kind)
    state.set_status(key, StreamStatus.LIVE)
    state.apply(
        StreamEvent(
            event_id=f"{inst.id}:{kind}:{ts.timestamp()}",
            kind=kind,
            instrument_id=inst.id,
            exchange_ts=ts,
            received_at=ts,
            epoch=1,
            payload=payload,
        )
    )


def live_perp_state(clock: ManualClock, mark_age: timedelta = timedelta(0)) -> MarketState:
    state = MarketState(clock, stale_after={})
    put(state, PERP, StreamKind.BOOK, book(PERP, NOW), NOW)
    put(
        state,
        PERP,
        StreamKind.MARK,
        MarkPrice(instrument_id=PERP.id, ts=NOW - mark_age, mark=D(82000)),
        NOW - mark_age,
    )
    put(
        state,
        PERP,
        StreamKind.FUNDING,
        FundingRate(instrument_id=PERP.id, ts=NOW, rate=D("0.00001"), interval=timedelta(hours=1)),
        NOW,
    )
    return state


def build(state: MarketState, inst: Instrument = PERP, qty: str = "0.1") -> MarketInputs:
    return market_inputs_from_snapshot(
        state.snapshot(),
        inst,
        quantity=D(qty),
        sigma=D("0.002"),
        eur_per_quote=D("0.86"),
        max_age=timedelta(seconds=5),
        min_slippage=D("0.0002"),
    )


def test_perp_inputs_from_live_snapshot() -> None:
    m = build(live_perp_state(ManualClock(NOW)))
    assert m.mark == D(82000)
    assert m.mark_source == "venue_mark"
    assert m.bid == D(81999)
    assert m.ask == D(82001)
    assert m.funding_rate == D("0.00001")


def test_stale_mark_fails_closed() -> None:
    clock = ManualClock(NOW)
    state = live_perp_state(clock)
    clock.advance(timedelta(seconds=6))
    with pytest.raises(DataQualityError, match="stale"):
        build(state)


def test_missing_funding_for_funded_instrument_fails_closed() -> None:
    state = MarketState(ManualClock(NOW), stale_after={})
    put(state, PERP, StreamKind.BOOK, book(PERP, NOW), NOW)
    put(state, PERP, StreamKind.MARK, MarkPrice(instrument_id=PERP.id, ts=NOW, mark=D(82000)), NOW)
    with pytest.raises(DataQualityError, match="funding"):
        build(state)


def test_book_too_thin_for_order_fails_closed() -> None:
    with pytest.raises(DataQualityError, match="depth"):
        build(live_perp_state(ManualClock(NOW)), qty="5")


def test_coinbase_spot_uses_labelled_mid_as_mark() -> None:
    state = MarketState(ManualClock(NOW), stale_after={})
    put(state, CB_SPOT, StreamKind.BOOK, book(CB_SPOT, NOW), NOW)
    m = build(state, CB_SPOT, qty="0.1")
    assert m.mark_source == "mid"
    assert m.mark == D(82000)
    assert m.funding_rate is None


def test_book_walk_slippage() -> None:
    b = book(PERP, NOW)
    # 0.5 @ 82001 + 0.5 @ 82010 -> avg 82005.5 -> (82005.5/82001 - 1)
    assert book_slippage(b, buy=True, size=D(1), floor=D(0)) == D("82005.5") / D(82001) - 1
    assert book_slippage(b, buy=True, size=D("0.1"), floor=D("0.0002")) == D("0.0002")
