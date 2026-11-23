"""Paper execution engine: order types, latency, shared liquidity, queue model, B1, refusals.

Every test ends with `verify_all()`: accounting invariants, journal replay, execution replay
from the audited inputs, M5 decision replay and the audit hash chain.
"""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.accounting.trading import OrderState
from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.execution.model import TimeInForce
from tests.unit.execution.harness import (
    CB_SPOT,
    CC_SPOT,
    PERP,
    Harness,
    limit_terms,
    market_terms,
)
from tests.unit.risk.test_engine import FCM, cc

D = Decimal


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def events(plan: object, name: str) -> list[dict[str, object]]:
    return [o for o in plan.outputs if o["event"] == name]  # type: ignore[attr-defined]


def closed(plan: object) -> list[tuple[object, object]]:
    return [(o["order_id"], o["reason"]) for o in events(plan, "order_closed")]


class TestMarketOrders:
    def test_walks_depth_and_cancels_the_unfillable_remainder(self, h: Harness) -> None:
        h.perp_ready(asks=[("82001", "0.004"), ("82002", "1")])
        assert h.accept(PERP, Side.BUY, "0.01", market_terms()).accepted
        h.advance(400)
        plan = h.book(PERP, [("81999", "1")], [("82001", "0.004")])
        assert [(f["price"], f["quantity"]) for f in h.fills(plan)] == [("82001", "0.004")]
        assert closed(plan) == [("bot-03:r1", "ioc_remainder:depth")]
        record = h.ledger.order("bot-03:r1")
        assert (record.state, record.filled_qty, record.reserved) == (
            OrderState.CANCELLED,
            D("0.004"),
            D(0),
        )
        assert events(plan, "depth")
        h.verify_all()

    def test_price_band_stops_the_walk(self, h: Harness) -> None:
        h.perp_ready(asks=[("82001", "0.004"), ("83000", "1")])
        assert h.accept(PERP, Side.BUY, "0.01", market_terms()).accepted
        h.advance(400)
        plan = h.book(PERP, [("81999", "1")], [("82001", "0.004"), ("84000", "1")])
        assert [f["price"] for f in h.fills(plan)] == ["82001"]
        assert closed(plan) == [("bot-03:r1", "ioc_remainder:price_band")]
        assert events(plan, "price_band")
        h.verify_all()

    def test_no_fill_before_the_submit_latency_elapses(self, h: Harness) -> None:
        h.perp_ready()
        h.accept(PERP, Side.BUY, "0.01", market_terms())
        h.advance(100)  # retail_eu: 250 ms +- 50 ms
        assert h.fills(h.book(PERP, [("81999", "1")], [("82001", "1")])) == []
        h.advance(300)
        assert len(h.fills(h.book(PERP, [("81999", "1")], [("82001", "1")]))) == 1
        h.verify_all()

    def test_ideal_profile_fills_on_the_next_book_version(self, tmp_path: Path) -> None:
        h = Harness(tmp_path, profiles={"bot-03": "ideal"})
        try:
            h.perp_ready()
            h.accept(PERP, Side.BUY, "0.01", market_terms())
            assert h.engine.state.orders["bot-03:r1"].effective_at == h.now
            plan = h.book(PERP, [("81999", "1")], [("82001", "1")])
            assert [f["price"] for f in h.fills(plan)] == ["82001"]
            h.verify_all()
        finally:
            h.close()


class TestLimitOrders:
    def test_ioc_fills_up_to_the_limit_only(self, h: Harness) -> None:
        h.perp_ready(asks=[("82001", "0.004"), ("82050", "1")])
        h.accept(PERP, Side.BUY, "0.01", limit_terms("82010", TimeInForce.IOC))
        h.advance(400)
        plan = h.book(PERP, [("81999", "1")], [("82001", "0.004"), ("82050", "1")])
        assert [f["quantity"] for f in h.fills(plan)] == ["0.004"]
        assert closed(plan) == [("bot-03:r1", "ioc_remainder:limit")]
        h.verify_all()

    @pytest.mark.parametrize(("depth", "filled"), [("0.009", False), ("0.01", True)])
    def test_fok_is_all_or_nothing(self, h: Harness, depth: str, filled: bool) -> None:
        h.perp_ready(asks=[("82001", "1")])
        h.accept(PERP, Side.BUY, "0.01", limit_terms("82001", TimeInForce.FOK))
        h.advance(400)
        plan = h.book(PERP, [("81999", "1")], [("82001", depth)])
        assert bool(h.fills(plan)) is filled
        if not filled:
            assert closed(plan) == [("bot-03:r1", "fok_unfillable:depth")]
            assert h.reserved() == 0
        h.verify_all()

    def test_post_only_that_would_cross_is_cancelled(self, h: Harness) -> None:
        h.perp_ready()
        h.accept(PERP, Side.BUY, "0.01", limit_terms("82001", post_only=True))
        h.advance(400)
        plan = h.book(PERP, [("81999", "1")], [("82001", "1")])
        assert h.fills(plan) == []
        assert closed(plan) == [("bot-03:r1", "post_only_would_cross")]
        h.verify_all()

    def test_resting_order_queue_model_on_cryptocom(self, h: Harness) -> None:
        h.perp_ready(bids=[("81999", "0.5")])
        h.accept(PERP, Side.BUY, "0.01", limit_terms("81999", post_only=True))
        h.advance(400)
        plan = h.book(PERP, [("81999", "0.5")], [("82001", "1")])
        (rested,) = events(plan, "rested")
        assert D(str(rested["queue_ahead"])) == D("0.5")
        # buyer-initiated prints at our price never fill a resting bid (documented taker side)
        assert h.fills(h.trade(PERP, "81999", "5", Side.BUY, "t1")) == []
        # seller-initiated: 0.3 works off the queue ahead, no fill yet
        assert h.fills(h.trade(PERP, "81999", "0.3", Side.SELL, "t2")) == []
        assert h.engine.state.orders["bot-03:r1"].queue_ahead == D("0.2")
        # 0.205: 0.2 queue, then 0.005 fills at our limit as MAKER
        (fill,) = h.fills(h.trade(PERP, "81999", "0.205", Side.SELL, "t3"))
        assert (fill["quantity"], fill["liquidity"], fill["basis"]) == (
            "0.005",
            "maker",
            "queue_model",
        )
        assert fill["fee_rate"] == "0.0002"
        partial = h.engine.state.orders["bot-03:r1"]  # still resting, partially filled
        assert (partial.filled, partial.remaining) == (D("0.005"), D("0.005"))
        assert h.ledger.order("bot-03:r1").filled_qty == partial.filled
        h.check_invariants()
        # a trade through our price fills the rest
        (fill,) = h.fills(h.trade(PERP, "81998", "1", Side.SELL, "t4"))
        assert fill["quantity"] == "0.005"
        assert fill["price"] == "81999"
        assert h.ledger.order("bot-03:r1").state is OrderState.FILLED
        h.verify_all()

    def test_coinbase_unknown_aggressor_fills_only_on_trade_through(self, h: Harness) -> None:
        h.spot_ready(CB_SPOT, [("81999.99", "0.5")], [("82000.01", "1")])
        assert h.accept(CB_SPOT, Side.BUY, "0.001", limit_terms("81999.99"), bot="bot-01").accepted
        h.advance(400)
        h.book(CB_SPOT, [("81999.99", "0.5")], [("82000.01", "1")])
        for n in range(3):  # at-price prints of unknown direction: queue only, never a fill
            assert h.fills(h.trade(CB_SPOT, "81999.99", "1", None, f"c{n}")) == []
        assert h.engine.state.orders["bot-01:r1"].queue_ahead == 0
        (fill,) = h.fills(h.trade(CB_SPOT, "81999.98", "0.0004", None, "c9"))
        assert fill["quantity"] == "0.0004"
        h.verify_all()

    def test_cancel_takes_effect_after_latency_and_can_race_a_fill(self, h: Harness) -> None:
        h.perp_ready(bids=[("81999", "0.1")])
        h.accept(PERP, Side.BUY, "0.01", limit_terms("81990"))
        h.advance(400)
        h.book(PERP, [("81999", "0.1")], [("82001", "1")])
        h.engine.request_cancel("bot-03:r1")
        assert h.ledger.order("bot-03:r1").state is OrderState.CANCEL_REQUESTED
        h.advance(50)
        (fill,) = h.fills(h.trade(PERP, "81980", "0.004", Side.SELL, "t1"))  # race: still live
        assert fill["quantity"] == "0.004"
        h.advance(400)
        plan = h.book(PERP, [("81999", "0.1")], [("82001", "1")])
        assert closed(plan) == [("bot-03:r1", "cancel_request")]
        record = h.ledger.order("bot-03:r1")
        assert (record.state, record.filled_qty, record.reserved) == (
            OrderState.CANCELLED,
            D("0.004"),
            D(0),
        )
        h.verify_all()

    def test_gtc_expires_after_its_lifetime(self, h: Harness) -> None:
        h.perp_ready()
        h.accept(PERP, Side.BUY, "0.01", limit_terms("81000"))
        h.advance(400)
        h.book(PERP, [("81999", "1")], [("82001", "1")])
        h.advance(24 * 3600 * 1000)
        h.live(PERP)
        plan = h.mark(PERP, "82000")
        assert closed(plan) == [("bot-03:r1", "gtc_lifetime")]
        assert h.ledger.order("bot-03:r1").state is OrderState.EXPIRED
        h.verify_all()


class TestReduceOnly:
    def test_reduce_only_reduces_and_never_flips(self, h: Harness) -> None:
        h.perp_ready(asks=[("82001", "1")], bids=[("81999", "1")])
        h.accept(PERP, Side.BUY, "0.01", market_terms())
        h.advance(400)
        h.book(PERP, [("81999", "1")], [("82001", "1")])
        too_big = h.accept(PERP, Side.SELL, "0.02", market_terms(), rid="r2", reduce_only=True)
        assert not too_big.accepted
        assert h.accept(
            PERP, Side.SELL, "0.004", market_terms(), rid="r3", reduce_only=True
        ).accepted
        h.advance(400)
        h.book(PERP, [("81999", "1")], [("82001", "1")])
        position = h.ledger.position("bot-03", PERP.id)
        assert position is not None
        assert (position.side, position.quantity) == (Side.BUY, D("0.006"))
        h.verify_all()

    def test_spot_reduce_only_is_refused(self, h: Harness) -> None:
        h.spot_ready(CC_SPOT, [("81999", "1")], [("82001", "1")])
        result = h.accept(CC_SPOT, Side.SELL, "0.001", market_terms(), reduce_only=True)
        assert not result.accepted
        assert "reduce-only" in result.reason
        h.verify_all()


class TestSharedLiquidityAndB1:
    def test_bots_never_consume_the_same_displayed_size(self, h: Harness) -> None:
        h.perp_ready(asks=[("82001", "0.006"), ("82002", "1")])
        assert h.accept(PERP, Side.BUY, "0.005", market_terms(), rid="a").accepted
        assert h.accept(
            PERP, Side.BUY, "0.005", market_terms(), bot="bot-04", rid="b", leverage="3"
        ).accepted
        h.advance(600)
        plan = h.book(PERP, [("81999", "1")], [("82001", "0.006")])
        by_order = {(f["order_id"], f["quantity"]) for f in h.fills(plan)}
        total = sum(D(f["quantity"]) for f in h.fills(plan))
        assert total == D("0.006")  # never more than displayed
        assert len(by_order) == 2
        # same displayed level again within replenish_ms: still consumed
        h.accept(PERP, Side.BUY, "0.001", market_terms(), rid="c")
        h.advance(400)
        again = h.book(PERP, [("81999", "1")], [("82001", "0.006")])
        assert h.fills(again) == []
        h.verify_all()

    def test_replenishment_and_level_updates_restore_liquidity(self, h: Harness) -> None:
        h.perp_ready(asks=[("82001", "0.006"), ("82002", "1")])
        h.accept(PERP, Side.BUY, "0.006", market_terms(), rid="a")
        h.advance(400)
        h.book(PERP, [("81999", "1")], [("82001", "0.006")])
        h.accept(PERP, Side.BUY, "0.002", market_terms(), rid="b")
        h.advance(400)
        plan = h.book(PERP, [("81999", "1")], [("82001", "0.007")])  # venue changed the level
        assert [f["quantity"] for f in h.fills(plan)] == ["0.002"]
        h.verify_all()

    def test_supervisor_hold_moves_into_the_order_reservation(self, h: Harness) -> None:
        h.perp_ready()
        start = h.cash() + h.reserved()
        assert h.accept(PERP, Side.BUY, "0.01", limit_terms("81000")).accepted
        hold = h.ledger.hold("r1")
        assert hold is not None
        assert hold.released  # B1: never reserved twice
        record = h.ledger.order("bot-03:r1")
        assert h.reserved() == record.reserved  # exactly one reservation
        assert record.reserved == D(162) + D("0.486")  # 810 notional / 5 + 0.06% taker fee
        assert h.cash() + h.reserved() == start
        assert h.supervisor.backing("r1") == "bot-03:r1"
        h.verify_all()

    def test_opposing_orders_across_bots_only_one_is_accepted(self, h: Harness) -> None:
        h.perp_ready()
        assert h.accept(PERP, Side.BUY, "0.005", limit_terms("81000"), rid="a").accepted
        second = h.accept(
            PERP, Side.SELL, "0.005", limit_terms("83000"), bot="bot-04", rid="b", leverage="3"
        )
        assert not second.accepted
        assert "opposing" in second.reason
        h.verify_all()

    def test_accept_is_idempotent_per_request(self, h: Harness) -> None:
        h.perp_ready()
        first = h.accept(PERP, Side.BUY, "0.01", limit_terms("81000"))
        audits = len(h.store.audit_records())
        again = h.accept(PERP, Side.BUY, "0.01", limit_terms("81000"))
        assert (again.order_id, again.accepted) == (first.order_id, True)
        assert len(h.store.audit_records()) == audits
        assert len(h.ledger.orders()) == 1
        h.verify_all()


class TestRefusals:
    def test_coinbase_derivatives_are_refused_with_the_reason(self, h: Harness) -> None:
        h.engine._instruments[FCM.id] = FCM
        result = h.accept(FCM, Side.BUY, "1", market_terms(), bot="bot-01")
        assert not result.accepted
        assert "Coinbase derivatives are disabled" in result.reason

    def test_eur_quoted_pairs_are_refused(self, h: Harness) -> None:
        eur = cc("BTC_EUR")
        h.engine._instruments[eur.id] = eur
        result = h.accept(eur, Side.BUY, "0.001", market_terms())
        assert not result.accepted
        assert "USD" in result.reason

    def test_missing_mark_refuses_derivative_orders(self, h: Harness) -> None:
        h.perp_ready()
        from shadow_trading_suite.streaming.events import StreamKind
        from shadow_trading_suite.streaming.state import StreamKey, StreamStatus

        h.market.set_status(StreamKey(PERP.id, StreamKind.MARK), StreamStatus.DOWN)
        result = h.accept(PERP, Side.BUY, "0.01", market_terms())
        assert not result.accepted
        assert "required market data unverified" in result.reason
        assert not h.ledger.orders()
        h.verify_all()

    def test_missing_trades_feed_is_refused_before_any_risk_hold(self, h: Harness) -> None:
        h.live(PERP)
        h.mark(PERP, "82000")
        h.funding(PERP, "0.00001")
        h.book(PERP, [("81999", "1")], [("82001", "1")])
        h.unblock()  # no trade yet: the TRADES stream has no data
        result = h.accept(PERP, Side.BUY, "0.01", market_terms())
        assert not result.accepted
        assert "trades: no data" in result.reason
        assert h.ledger.holds() == {}
        assert h.supervisor.exposures() == {}
        h.verify_all()

    def test_orders_are_refused_until_the_restart_gap_is_cleared(self, h: Harness) -> None:
        h.live(PERP)
        h.mark(PERP, "82000")
        h.funding(PERP, "0.00001")
        h.book(PERP, [("81999", "1")], [("82001", "1")])
        h.trade(PERP, "82000", "0.001", Side.BUY, "w1")
        result = h.accept(PERP, Side.BUY, "0.01", market_terms())
        assert not result.accepted
        assert "restart" in result.reason
        h.unblock()
        assert h.accept(PERP, Side.BUY, "0.01", market_terms(), rid="r2").accepted
        h.verify_all()


def test_venue_enum_values_are_used_as_state_keys() -> None:
    assert {v.value for v in Venue} == {"coinbase", "cryptocom"}
