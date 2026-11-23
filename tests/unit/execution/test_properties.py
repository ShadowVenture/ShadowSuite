"""Property tests: random multi-bot order, cancel and market-event sequences (including
duplicated and replayed events, gaps and liquidations) preserve every invariant after every
command, replay exactly, and are deterministic (two runs are identical)."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.execution.model import TimeInForce
from shadow_trading_suite.streaming.events import StreamKind
from shadow_trading_suite.streaming.state import StreamKey
from tests.unit.execution.harness import PERP, Harness, limit_terms, market_terms

D = Decimal

price = st.integers(-40, 40).map(lambda k: 82000 + 5 * k)
qty = st.integers(1, 80).map(lambda n: str(D(n) / 10000))


@st.composite
def book_levels(draw: st.DrawFn) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    mid = draw(price)
    n_bid, n_ask = draw(st.integers(1, 4)), draw(st.integers(1, 4))
    size = st.integers(10, 5000).map(lambda n: str(D(n) / 10000))
    bids = [(str(mid - 1 - 3 * i), draw(size)) for i in range(n_bid)]
    asks = [(str(mid + 1 + 3 * i), draw(size)) for i in range(n_ask)]
    return bids, asks


accepts = st.tuples(
    st.just("accept"),
    st.sampled_from(["bot-03", "bot-04"]),
    st.sampled_from([Side.BUY, Side.SELL]),
    qty,
    st.sampled_from(["market", "ioc", "fok", "gtc", "post"]),
    price,
    st.sampled_from([False, False, False, True]),
)
books = st.tuples(st.just("book"), book_levels())
extras = st.one_of(
    st.tuples(st.just("cancel"), st.integers(0, 20)),
    st.tuples(st.just("trade"), price, qty, st.sampled_from([Side.BUY, Side.SELL, None])),
    st.tuples(st.just("mark"), st.integers(-30, 30).map(lambda k: 82000 + 700 * k)),
    st.tuples(st.just("advance"), st.sampled_from([50, 300, 1200, 6000])),
    st.tuples(st.just("duplicate"), st.sampled_from([StreamKind.BOOK, StreamKind.TRADES])),
    st.tuples(st.just("gap")),
    st.tuples(st.just("tick")),
    books,
)
# one round: new orders, a book after the submit latency, then other events
rounds = st.tuples(st.lists(accepts, min_size=1, max_size=3), books, st.lists(extras, max_size=4))


def flatten(rs: list[Any]) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    for orders, bk, more in rs:
        out += [*orders, bk, *more, ("tick",)]
    return out


def apply(h: Harness, step: tuple[Any, ...], n: int) -> None:
    kind = step[0]
    if kind == "accept":
        _, bot, side, q, tif, px, reduce = step
        if tif == "market":
            terms = market_terms()
        elif tif == "post":
            terms = limit_terms(str(px), post_only=True)
        else:
            terms = limit_terms(str(px), TimeInForce(tif))
        lev = "3" if bot == "bot-04" else "5"
        h.funding(PERP, "0.00001")  # refresh every required feed (strategies would wait)
        h.trade(PERP, "82000", "0.0001", Side.BUY, f"w{n}")
        h.mark(PERP, h.last_mark)
        h.accept(PERP, side, q, terms, bot=bot, rid=f"q{n}", leverage=lev, reduce_only=reduce)
    elif kind == "cancel":
        orders = sorted(h.engine.state.orders)
        if orders:
            h.engine.request_cancel(orders[step[1] % len(orders)])
    elif kind == "book":
        bids, asks = step[1]
        h.advance(300)  # past the submit latency: pending orders activate on this book
        h.book(PERP, bids, asks)
    elif kind == "trade":
        h.trade(PERP, str(step[1]), step[2], step[3], f"t{n % 7}")  # ids repeat: duplicates
    elif kind == "mark":
        h.last_mark = str(step[1])
        h.mark(PERP, h.last_mark)
    elif kind == "advance":
        h.advance(step[1])
    elif kind == "duplicate":
        key = StreamKey(PERP.id, step[1])
        if key in h.market.snapshot().entries:
            h.engine.on_market(h.market.snapshot(), key)
    elif kind == "gap":
        h.engine.on_gap(Venue.CRYPTOCOM, "GapNotice")
    elif kind == "tick":
        h.live(PERP)
        h.engine.tick(h.market.snapshot())


def run(tmp: Path, steps: list[tuple[Any, ...]]) -> tuple[str, str, list[Any]]:
    h = Harness(tmp)
    try:
        h.perp_ready(bids=[("81999", "0.5")], asks=[("82001", "0.5")])
        for n, step in enumerate(steps):
            apply(h, step, n)
            h.check_invariants()
        h.verify_all()
        audit = [(r.kind, r.subject, r.payload) for r in h.store.audit_records()]
        seen = {o["event"] for r in audit if r[0] == "exec.cmd" for o in r[2]["outputs"]}
        for name in sorted(
            seen
            & {
                "fill",
                "rested",
                "liq_fill",
                "liq_settlement",
                "duplicate",
                "duplicate_trade",
                "venue_blocked",
                "funding",
            }
        ):
            event(name)
        return h.ledger.state_digest(), h.engine.state.digest(), audit
    finally:
        h.close()


@settings(max_examples=30, deadline=None)
@given(st.lists(rounds, min_size=2, max_size=8).map(flatten))
def test_random_sequences_preserve_invariants_and_replay_deterministically(
    tmp_path_factory: pytest.TempPathFactory, steps: list[tuple[Any, ...]]
) -> None:
    first = run(tmp_path_factory.mktemp("a"), steps)
    second = run(tmp_path_factory.mktemp("b"), steps)
    assert first == second
