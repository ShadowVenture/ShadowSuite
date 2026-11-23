"""Order-book reconstruction: snapshot + deltas, gap and crossed-book detection, resync."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.market import BookLevel
from shadow_trading_suite.streaming.orderbook import BookSyncError, LocalOrderBook
from tests.unit.exchanges.fakes import fixture_json

IID = InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTC_USD")
T0 = datetime(2026, 10, 19, 14, 43, tzinfo=UTC)


def d(x: str | int) -> Decimal:
    return Decimal(x)


def synced_book() -> LocalOrderBook:
    book = LocalOrderBook(IID)
    book.apply_snapshot(
        bids=[(d("100"), d("1")), (d("99"), d("2"))],
        asks=[(d("101"), d("1")), (d("102"), d("3"))],
        ts=T0,
        sequence=10,
    )
    return book


class TestReconstruction:
    def test_snapshot(self) -> None:
        top = synced_book().top(5)
        assert [lvl.price for lvl in top.bids] == [d("100"), d("99")]
        assert [lvl.price for lvl in top.asks] == [d("101"), d("102")]
        assert top.sequence == 10

    def test_deltas_update_insert_and_delete(self) -> None:
        book = synced_book()
        book.apply_delta(
            [("bid", d("100"), d("0")), ("bid", d("99.5"), d("4")), ("ask", d("101"), d("7"))],
            ts=T0 + timedelta(seconds=1),
            sequence=11,
            previous=10,
        )
        top = book.top(5)
        assert [(lvl.price, lvl.quantity) for lvl in top.bids] == [(d("99.5"), d(4)), (d(99), d(2))]
        assert top.asks[0].quantity == d(7)
        assert top.ts == T0 + timedelta(seconds=1)

    def test_depth_limit(self) -> None:
        assert len(synced_book().top(1).bids) == 1

    def test_zero_quantity_levels_in_snapshot_ignored(self) -> None:
        book = LocalOrderBook(IID)
        book.apply_snapshot([(d(100), d(0)), (d(99), d(1))], [(d(101), d(1))], T0)
        assert [lvl.price for lvl in book.top(5).bids] == [d(99)]


class TestIntegrity:
    def test_delta_before_snapshot_refused(self) -> None:
        with pytest.raises(BookSyncError, match="not synced"):
            LocalOrderBook(IID).apply_delta([("bid", d(1), d(1))], T0)

    def test_sequence_gap_desyncs(self) -> None:
        book = synced_book()
        with pytest.raises(BookSyncError, match="gap"):
            book.apply_delta([("bid", d(98), d(1))], T0, sequence=13, previous=12)
        assert not book.synced
        with pytest.raises(BookSyncError, match="not synced"):
            book.top(5)

    def test_crossing_delta_desyncs(self) -> None:
        book = synced_book()
        with pytest.raises(BookSyncError, match="crossed"):
            book.apply_delta([("bid", d("101.5"), d(1))], T0, sequence=11, previous=10)
        assert not book.synced

    def test_crossed_snapshot_refused(self) -> None:
        with pytest.raises(BookSyncError, match="crossed"):
            LocalOrderBook(IID).apply_snapshot([(d(102), d(1))], [(d(101), d(1))], T0)

    def test_resync_after_gap_with_fresh_snapshot(self) -> None:
        book = synced_book()
        with pytest.raises(BookSyncError):
            book.apply_delta([], T0, sequence=20, previous=19)
        book.apply_snapshot([(d(90), d(1))], [(d(91), d(1))], T0, sequence=30)
        assert book.synced
        assert book.top(1).bids[0].price == d(90)

    def test_unknown_side_refused(self) -> None:
        with pytest.raises(BookSyncError, match="side"):
            synced_book().apply_delta([("middle", d(1), d(1))], T0, sequence=11, previous=10)


def test_real_cryptocom_snapshot_and_delta_chain() -> None:
    snap = fixture_json("ws/cryptocom/book_snapshot.json")["result"]["data"][0]
    book = LocalOrderBook(IID)
    book.apply_snapshot(
        [(d(p), d(q)) for p, q, *_ in snap["bids"]],
        [(d(p), d(q)) for p, q, *_ in snap["asks"]],
        datetime.fromtimestamp(snap["t"] / 1000, UTC),
        sequence=snap["u"],
    )
    for name in ("book_update_1.json", "book_update_2.json"):
        upd = fixture_json(f"ws/cryptocom/{name}")["result"]["data"][0]
        changes = [("bid", d(p), d(q)) for p, q, *_ in upd["update"]["bids"]] + [
            ("ask", d(p), d(q)) for p, q, *_ in upd["update"]["asks"]
        ]
        book.apply_delta(
            changes,
            datetime.fromtimestamp(upd["t"] / 1000, UTC),
            sequence=upd["u"],
            previous=upd["pu"],
        )
    top = book.top(10)
    assert top.best_bid is not None
    assert top.best_ask is not None
    assert top.best_bid.price < top.best_ask.price


def test_a_requoted_deleted_and_readded_level_is_never_served_stale() -> None:
    book = synced_book()
    assert book.top(2).bids[0].quantity == d("1")
    book.apply_delta([("bid", d("100"), d("5"))], T0)
    assert book.top(2).bids[0].quantity == d("5")
    book.apply_delta([("ask", d("101"), d("0"))], T0)
    assert [lv.price for lv in book.top(2).asks] == [d("102")]
    book.apply_delta([("ask", d("101"), d("7"))], T0)
    assert book.top(2).asks[0] == BookLevel(price=d("101"), quantity=d("7"))


class Reference:
    """Naive model of the documented book semantics (full scans, no index)."""

    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.synced = False

    def crossed(self) -> bool:
        return bool(self.bids and self.asks and max(self.bids) >= min(self.asks))

    def top(self, depth: int) -> tuple[list[tuple[Decimal, Decimal]], ...]:
        bids = sorted(self.bids.items(), reverse=True)[:depth]
        return bids, sorted(self.asks.items())[:depth]  # depth 0 => no levels


prices = st.integers(21, 40).map(Decimal)  # narrow: changes hit visible, cached levels
qty = st.sampled_from([d(0), d("0.5"), d(1), d(3)])
change = st.one_of(  # mostly in-book re-quotes (they exercise the index), sometimes crossing
    st.tuples(st.just("bid"), st.integers(21, 30).map(Decimal), qty),
    st.tuples(st.just("ask"), st.integers(31, 40).map(Decimal), qty),
    st.tuples(st.sampled_from(["bid", "ask"]), prices, qty),
)
book_ops = st.lists(
    st.one_of(
        st.tuples(
            st.just("snapshot"),
            st.lists(st.tuples(st.integers(21, 30).map(Decimal), qty), max_size=12),
            st.lists(st.tuples(st.integers(31, 40).map(Decimal), qty), max_size=12),
        ),
        st.tuples(st.just("delta"), st.lists(change)),
    ),
    max_size=30,
)


@given(ops=book_ops, depth=st.integers(0, 8))
def test_book_matches_the_reference_model(ops: list[tuple[Any, ...]], depth: int) -> None:
    """Indexing the book (performance) must not change a single observable result."""
    book, ref = LocalOrderBook(IID), Reference()
    for op in ops:
        if op[0] == "snapshot":
            book.apply_snapshot(op[1], op[2], T0)
            ref.bids = {p: q for p, q in op[1] if q > 0}
            ref.asks = {p: q for p, q in op[2] if q > 0}
            ref.synced = True
        else:
            if not ref.synced:
                continue
            new_b, new_a = dict(ref.bids), dict(ref.asks)
            for side, p, q in op[1]:
                levels = new_b if side == "bid" else new_a
                if q == 0:
                    levels.pop(p, None)
                else:
                    levels[p] = q
            ref.bids, ref.asks = new_b, new_a
            try:
                book.apply_delta(op[1], T0)
            except BookSyncError:
                assert ref.crossed()
                ref.synced, ref.bids, ref.asks = False, {}, {}
                assert not book.synced
                continue
        assert not ref.crossed()
        snap = book.top(depth)
        assert (
            [(lv.price, lv.quantity) for lv in snap.bids],
            [(lv.price, lv.quantity) for lv in snap.asks],
        ) == ref.top(depth)
