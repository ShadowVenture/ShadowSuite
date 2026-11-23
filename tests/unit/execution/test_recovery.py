"""Execution recovery (B2): exact state after restart, tamper refusal, reconciliation, failures."""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.accounting.trading import OrderSpec, OrderState
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.execution.engine import ExecutionHaltedError, instrument_terms
from shadow_trading_suite.storage.hardened import StorageError
from shadow_trading_suite.storage.ledger import LedgerStore
from tests.unit.execution.harness import PERP, Harness, limit_terms, market_terms
from tests.unit.execution.test_engine import closed, events


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def test_restart_never_resurrects_consumed_liquidity(h: Harness) -> None:
    h.perp_ready(asks=[("82001", "0.006"), ("82002", "1")])
    h.accept(PERP, Side.BUY, "0.006", market_terms(), rid="a")
    h.advance(400)
    h.book(PERP, [("81999", "1")], [("82001", "0.006")])  # consumes the whole level
    consumed = h.engine.state.consumed
    r = h.restart()
    try:
        assert r.engine.state.consumed == consumed
        r.event_prefix = "2"
        r.live(PERP)
        r.mark(PERP, "82000")
        r.funding(PERP, "0.00001")
        r.book(PERP, [("81999", "1")], [("82001", "0.006"), ("82002", "1")])  # same level
        r.trade(PERP, "82000", "0.001", Side.BUY, "w2")
        r.unblock()
        assert r.accept(PERP, Side.BUY, "0.002", market_terms(), rid="b").accepted
        r.advance(400)  # still within replenish_ms of the consumption, same displayed size
        plan = r.book(PERP, [("81999", "1")], [("82001", "0.006")])
        assert r.fills(plan) == []
        assert closed(plan) == [("bot-03:b", "ioc_remainder:depth")]
        r.verify_all()
    finally:
        r.close()


def test_restart_restores_queue_and_pending_cancel_then_applies_the_gap_policy(
    h: Harness,
) -> None:
    h.perp_ready(bids=[("81999", "0.5")])
    h.accept(PERP, Side.BUY, "0.001", limit_terms("81999"), rid="a")
    h.accept(PERP, Side.BUY, "0.001", limit_terms("81990"), rid="b")
    h.advance(400)
    h.book(PERP, [("81999", "0.5")], [("82001", "1")])
    h.trade(PERP, "81999", "0.3", Side.SELL, "t1")
    h.engine.request_cancel("bot-03:b")
    before = h.engine.state
    r = h.restart(start=False)
    try:
        state = r.engine.state
        assert state.digest() == before.digest()
        assert state.orders["bot-03:a"].queue_ahead == before.orders["bot-03:a"].queue_ahead
        assert state.orders["bot-03:b"].cancel_effective_at is not None
        assert r.engine.recovery_issues == ()
        with pytest.raises(ExecutionHaltedError):
            r.engine.request_cancel("bot-03:a")  # nothing runs before the restart policy
        plan = r.engine.start()
        assert sorted(closed(plan)) == [("bot-03:a", "gap:restart"), ("bot-03:b", "gap:restart")]
        for oid in ("bot-03:a", "bot-03:b"):
            assert r.ledger.order(oid).state is OrderState.CANCELLED
        assert r.reserved() == 0
        r.verify_all()
    finally:
        r.close()


def test_liquidation_in_progress_continues_after_restart(h: Harness) -> None:
    h.perp_ready(asks=[("82001", "1")])
    h.accept(PERP, Side.BUY, "0.01", market_terms())
    h.advance(400)
    h.book(PERP, [("81999", "1")], [("82001", "1")])
    h.mark(PERP, "65900")
    h.advance(400)
    h.book(PERP, [("65800", "0.004")], [("65900", "1")])
    r = h.restart()
    try:
        assert set(r.engine.state.liquidations) == set(r.ledger.liquidations())
        r.event_prefix = "2"
        r.live(PERP)
        r.advance(100)
        plan = r.book(PERP, [("65700", "1")], [("65900", "1")])  # no unblock needed
        assert [str(o["quantity"]) for o in events(plan, "liq_fill")] == ["0.006"]
        assert events(plan, "liq_settlement")
        assert r.ledger.position("bot-03", PERP.id) is None
        r.verify_all()
    finally:
        r.close()


def test_tampered_audit_refuses_to_start(h: Harness) -> None:
    h.perp_ready()
    h.accept(PERP, Side.BUY, "0.01", limit_terms("81000"))
    h.store._con.execute(
        "UPDATE risk_audit SET payload = replace(payload, '81000', '81001') WHERE kind = 'exec.cmd'"
    )
    with pytest.raises(StorageError, match="audit chain broken"):
        h.restart()
    h.store = LedgerStore.open(h.tmp / "data", clock=h.clock)  # for the fixture teardown


def test_reconciliation_mismatch_makes_the_engine_read_only(h: Harness) -> None:
    h.perp_ready()
    h.ledger.open_order(  # an order the execution engine never accepted
        OrderSpec(
            order_id="rogue",
            bot_id="bot-03",
            instrument=instrument_terms(PERP),
            side=Side.BUY,
            quantity=PERP.lot_size,
            reserve_price=PERP.tick_size * 100,
            reserve_fee_rate=Decimal(0),
            created_at=h.now,
        )
    )
    r = h.restart(start=False)
    try:
        assert any("rogue" in i for i in r.engine.recovery_issues)
        assert r.engine.read_only
        assert r.supervisor.emergency_shutdown
        with pytest.raises(ExecutionHaltedError):
            r.engine.start()
    finally:
        r.close()


def test_a_failing_command_commits_nothing_and_halts(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.perp_ready()
    h.accept(PERP, Side.BUY, "0.01", market_terms())
    h.advance(400)
    before = (h.ledger.state_digest(), h.engine.state.digest())

    def boom(self: LedgerStore) -> None:
        raise RuntimeError("crash at COMMIT")

    monkeypatch.setattr(LedgerStore, "_commit", boom)
    with pytest.raises(RuntimeError):
        h.book(PERP, [("81999", "1")], [("82001", "1")])
    monkeypatch.undo()
    assert (h.ledger.state_digest(), h.engine.state.digest()) == before
    assert h.engine.read_only
    with pytest.raises(ExecutionHaltedError):
        h.engine.request_cancel("bot-03:r1")
    r = h.restart()
    try:
        assert r.engine.recovery_issues == ()
        assert r.ledger.order("bot-03:r1").state is OrderState.CANCELLED  # restart gap
        r.verify_all()
    finally:
        r.close()
