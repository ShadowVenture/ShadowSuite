"""Funding, unpaid funding (B5), multi-book liquidation (B3, B6), E4 gaps, stale/missing data
and the emergency shutdown, all through the paper execution engine."""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.accounting.journal import bot_account
from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.streaming.events import StreamKind
from shadow_trading_suite.streaming.state import StreamKey, StreamStatus
from tests.unit.execution.harness import PERP, Harness, limit_terms, market_terms
from tests.unit.execution.test_engine import closed, events

D = Decimal
VENUE = Venue.CRYPTOCOM
MARGIN = bot_account("bot-03", VENUE, "margin", "BTCUSD-PERP")
PAYABLE = bot_account("bot-03", VENUE, "payable", "funding:BTCUSD-PERP")
INSURANCE = bot_account("bot-03", VENUE, "sim", "insurance_absorption")
HOUR = 3600 * 1000


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def open_long(h: Harness, rid: str = "r1") -> None:
    """Long 0.01 BTCUSD-PERP at 82,001, 5x: margin 164.002, fee 0.492006."""
    h.perp_ready(asks=[("82001", "1")])
    assert h.accept(PERP, Side.BUY, "0.01", market_terms(), rid=rid).accepted
    h.advance(400)
    h.book(PERP, [("81999", "1")], [("82001", "1")])
    assert h.ledger.balance(MARGIN, "USD") == D("164.002")


def status(h: Harness, kind: StreamKind, st: StreamStatus) -> None:
    h.market.set_status(StreamKey(PERP.id, kind), st)


class TestFunding:
    def test_charged_once_at_the_interval_boundary(self, h: Harness) -> None:
        open_long(h)
        cash = h.cash()
        h.advance(HOUR)  # 13:00:01.3, funding_next was 13:00
        plan = h.mark(PERP, "82000")
        (charge,) = events(plan, "funding")
        assert charge["boundary"] == "2026-10-19T13:00:00+00:00"
        assert h.cash() == cash - D("0.0082")  # 0.01 x 82,000 x 0.00001, long pays
        h.mark(PERP, "82000")  # same interval again: nothing more
        assert h.cash() == cash - D("0.0082")
        h.verify_all()

    def test_stale_inputs_leave_funding_unsettled_and_block_the_instrument(
        self, h: Harness
    ) -> None:
        open_long(h)
        h.advance(HOUR)
        plan = h.book(PERP, [("81999", "1")], [("82001", "1")])  # mark is an hour old
        (unsettled,) = events(plan, "funding_unsettled")
        assert unsettled["reason"] == "no fresh mark"
        assert h.engine.state.funding_unsettled  # recorded, never silently dropped
        h.mark(PERP, "82000")
        h.funding(PERP, "0.00001")
        h.trade(PERP, "82000", "0.001", Side.BUY, "w2")
        result = h.accept(PERP, Side.BUY, "0.001", market_terms(), rid="r2")
        assert not result.accepted
        assert "funding could not be settled" in result.reason
        h.verify_all()

    def test_unpaid_funding_becomes_a_payable_and_is_swept_from_later_cash(
        self, h: Harness
    ) -> None:
        open_long(h)
        h.ledger.commit("lock", "bot-03", VENUE, "USD", h.cash())  # no free cash left
        h.funding(PERP, "0.25")
        h.advance(HOUR)
        plan = h.mark(PERP, "82000")  # 205.00 due: 164.002 margin + 40.998 unpaid
        assert events(plan, "funding")
        assert h.ledger.balance(MARGIN, "USD") == 0
        assert h.ledger.balance(PAYABLE, "USD") == D("-40.998")
        # the mark check in the SAME command sees the drained margin and the liability
        assert events(plan, "mm_breach")
        h.ledger.release("lock")  # cash comes back (not an engine command)
        plan = h.trade(PERP, "82000", "0.001", Side.BUY, "w9")
        assert [p["op"] for p in follow_ups(h)] == ["settle_payable"]
        assert h.ledger.balance(PAYABLE, "USD") == 0
        h.verify_all()

    def test_an_outstanding_payable_alone_can_trigger_stage_1(self, h: Harness) -> None:
        open_long(h)
        h.ledger.commit("lock", "bot-03", VENUE, "USD", h.cash())  # no free cash left
        h.funding(PERP, "0.4")
        h.advance(HOUR)
        plan = h.mark(PERP, "90000")  # unrealised gain ~80 USD, but margin drained to 0
        assert h.ledger.balance(MARGIN, "USD") == 0
        payable = -h.ledger.balance(PAYABLE, "USD")
        upnl = (D(90000) - D(82001)) * D("0.01")
        requirement = D(900) * (D("0.005") + D("0.0006"))
        assert upnl > requirement > upnl - payable  # breach ONLY because of the liability
        (breach,) = events(plan, "mm_breach")
        assert D(str(breach["position_equity"])) == upnl - payable
        h.ledger.release("lock")
        h.verify_all()


def follow_ups(h: Harness) -> list[dict[str, object]]:
    last = [r for r in h.store.audit_records() if r.kind == "exec.cmd"][-1]
    return list(last.payload["follow_ups"])


class TestLiquidation:
    def test_five_stages_over_successive_books(self, h: Harness) -> None:
        open_long(h)
        assert h.accept(PERP, Side.BUY, "0.001", limit_terms("60000"), rid="r2").accepted
        h.advance(400)
        h.book(PERP, [("81999", "1")], [("82001", "1")])  # r2 rests
        assert not events(h.mark(PERP, "66100"), "mm_breach")  # equity 4.992 > 3.7016
        plan = h.mark(PERP, "65900")  # equity 2.992 < 659 x 0.56% = 3.6904
        (breach,) = events(plan, "mm_breach")
        assert D(str(breach["position_equity"])) == D("2.992")
        assert "ASSUMPTION" in str(breach["model"])
        assert events(plan, "liq_trigger")
        assert closed(plan) == [("bot-03:r2", "liquidation")]
        refused = h.accept(PERP, Side.BUY, "0.001", market_terms(), rid="r3")
        assert not refused.accepted
        # stage 3 waits for the liquidation latency
        h.advance(100)
        assert not events(h.book(PERP, [("65800", "1")], [("65900", "1")]), "liq_fill")
        h.advance(300)
        plan = h.book(PERP, [("65800", "0.004"), ("65700", "0.004")], [("65900", "1")])
        assert [str(o["quantity"]) for o in events(plan, "liq_fill")] == ["0.004", "0.004"]
        assert events(plan, "liq_depth_exhausted")
        assert h.ledger.position("bot-03", PERP.id).quantity == D("0.002")  # type: ignore[union-attr]
        h.advance(100)
        plan = h.book(PERP, [("65600", "1")], [("65900", "1")])
        assert [str(o["quantity"]) for o in events(plan, "liq_fill")] == ["0.002"]
        assert events(plan, "liq_settlement")
        # reference: X = 164.002 - 162.81 PnL - 3.286 fees = -2.094 -> absorbed
        assert h.ledger.position("bot-03", PERP.id) is None
        assert h.ledger.balance(MARGIN, "USD") == 0
        assert -h.ledger.balance(INSURANCE, "USD") == D("2.094")
        assert h.ledger.bad_debt("bot-03", VENUE, "USD") == D("2.094")
        assert h.supervisor.exposures() == {}
        h.verify_all()

    def test_liquidation_never_uses_a_one_sided_book(self, h: Harness) -> None:
        # crossed/locked books are already rejected by OrderBookSnapshot (core model)
        open_long(h)
        h.mark(PERP, "65000")
        h.advance(400)
        one_sided = h.book(PERP, [], [("65800", "1")])
        assert not events(one_sided, "liq_fill")
        assert events(one_sided, "data_quality")
        assert h.ledger.liquidation("bot-03", PERP.id) is not None  # still in progress
        h.verify_all()


class TestGaps:
    def test_gap_notice_cancels_resting_orders_and_blocks_new_ones(self, h: Harness) -> None:
        open_long(h)
        h.accept(PERP, Side.BUY, "0.001", limit_terms("70000"), rid="r2")
        h.advance(400)
        h.book(PERP, [("81999", "1")], [("82001", "1")])
        plan = h.engine.on_gap(VENUE, "GapNotice")
        assert closed(plan) == [("bot-03:r2", "gap:GapNotice")]
        assert h.ledger.position("bot-03", PERP.id) is not None  # positions are kept
        refused = h.accept(PERP, Side.BUY, "0.001", limit_terms("70000"), rid="r3")
        assert "venue blocked" in refused.reason
        assert events(h.unblock(), "venue_unblocked")
        h.verify_all()

    def test_venue_down_longer_than_five_seconds(self, h: Harness) -> None:
        open_long(h)
        h.accept(PERP, Side.BUY, "0.001", limit_terms("70000"), rid="r2")
        h.advance(400)
        h.book(PERP, [("81999", "1")], [("82001", "1")])
        status(h, StreamKind.BOOK, StreamStatus.DOWN)
        assert not closed(h.engine.tick(h.market.snapshot()))
        h.advance(4000)
        assert not closed(h.engine.tick(h.market.snapshot()))  # 4 s: not yet
        h.advance(1500)
        plan = h.engine.tick(h.market.snapshot())
        assert closed(plan) == [("bot-03:r2", "gap:data not LIVE > gap threshold")]
        h.verify_all()

    def test_missing_mark_triggers_emergency_leaving_only_reduce_only(self, h: Harness) -> None:
        open_long(h)
        h.accept(PERP, Side.BUY, "0.001", limit_terms("70000"), rid="r2")
        h.advance(400)
        h.book(PERP, [("81999", "1")], [("82001", "1")])
        status(h, StreamKind.MARK, StreamStatus.DOWN)
        h.engine.tick(h.market.snapshot())
        h.advance(5500)
        plan = h.engine.tick(h.market.snapshot())
        assert events(plan, "mark_unavailable")
        assert h.supervisor.emergency_shutdown
        assert h.engine.state.orders == {}
        h.live(PERP)
        h.mark(PERP, "82000")
        h.funding(PERP, "0.00001")
        h.book(PERP, [("81999", "1")], [("82001", "1")])
        h.trade(PERP, "82000", "0.001", Side.BUY, "w3")
        h.unblock()
        assert not h.accept(PERP, Side.BUY, "0.001", market_terms(), rid="r3").accepted
        assert h.accept(
            PERP, Side.SELL, "0.005", market_terms(), rid="r4", reduce_only=True
        ).accepted
        h.verify_all()


class TestMarketDataIntegrity:
    def test_stale_event_never_fills(self, h: Harness) -> None:
        h.perp_ready()
        h.accept(PERP, Side.BUY, "0.01", market_terms())
        h.advance(400)
        from shadow_trading_suite.core.market import BookLevel, OrderBookSnapshot
        from shadow_trading_suite.streaming.events import StreamEvent

        ev = StreamEvent(
            event_id="cryptocom:book:BTCUSD-PERP:1:stale",
            kind=StreamKind.BOOK,
            instrument_id=PERP.id,
            exchange_ts=h.now,
            received_at=h.now,
            epoch=1,
            payload=OrderBookSnapshot(
                instrument_id=PERP.id,
                ts=h.now,
                bids=(BookLevel(price=D(81999), quantity=D(1)),),
                asks=(BookLevel(price=D(82001), quantity=D(1)),),
            ),
        )
        h.market.apply(ev)
        h.advance(6000)  # older than the 5 s derivatives bound
        plan = h.engine.on_market(h.market.snapshot(), StreamKey(PERP.id, StreamKind.BOOK))
        assert h.fills(plan) == []
        assert "stale" in str(events(plan, "data_quality")[0]["reason"])
        h.verify_all()

    def test_duplicate_and_non_live_events_never_fill_twice(self, h: Harness) -> None:
        h.perp_ready(bids=[("81999", "0.1")])
        h.accept(PERP, Side.BUY, "0.01", limit_terms("81990"))
        h.advance(400)
        h.book(PERP, [("81999", "0.1")], [("82001", "1")])
        h.trade(PERP, "81980", "0.004", Side.SELL, "t1")
        key = StreamKey(PERP.id, StreamKind.TRADES)
        again = h.engine.on_market(h.market.snapshot(), key)  # same event re-delivered
        assert events(again, "duplicate")
        assert h.ledger.order("bot-03:r1").filled_qty == D("0.004")
        status(h, StreamKind.TRADES, StreamStatus.SYNCING)
        h.trade(PERP, "81980", "1", Side.SELL, "t2")  # refused by MarketState (not LIVE)
        assert h.ledger.order("bot-03:r1").filled_qty == D("0.004")
        status(h, StreamKind.TRADES, StreamStatus.LIVE)
        plan = h.trade(PERP, "81980", "0.004", Side.SELL, "t1")  # replayed trade id
        assert events(plan, "duplicate_trade")
        assert h.ledger.order("bot-03:r1").filled_qty == D("0.004")
        h.verify_all()
