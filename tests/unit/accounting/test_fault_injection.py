"""In-memory state never diverges from committed DuckDB state, even under write failures.

Random operation sequences run with injected exceptions at every database insertion point and
at COMMIT. After EVERY attempt (successful or failed) a ledger rebuilt from the database must
equal the live projection, the independent reference must still reconcile, and replay must
reproduce all committed operations.
"""

import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shadow_trading_suite.accounting.trading import OrderSpec, TradingLedger
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.storage.ledger import LedgerStore
from tests.unit.accounting.test_reconciliation import (
    BOTS,
    COST,
    KINDS,
    RATE,
    SPOT,
    VENUE,
    Runner,
    reconcile,
)

D = Decimal
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
FAULT_POINTS = (
    "_insert_operation",
    "_insert_event",
    "_insert_transition",
    "_insert_audit",
    "_commit",
)


ORDER = OrderSpec(
    order_id="o1",
    bot_id="bot-03",
    instrument=SPOT,
    side=Side.BUY,
    quantity=D("0.001"),
    reserve_price=D(82000),
    reserve_fee_rate=D("0.006"),
    created_at=NOW,
)


class InjectedFaultError(RuntimeError):
    pass


def raiser(fault: str) -> Callable[..., None]:
    def boom(*_: object, **__: object) -> None:
        raise InjectedFaultError(fault)

    return boom


steps = st.lists(
    st.tuples(
        st.sampled_from(KINDS),
        st.integers(0, 1),
        st.integers(0, 10**6),
        st.sampled_from((None, *FAULT_POINTS)),
    ),
    min_size=5,
    max_size=40,
)


@settings(max_examples=40, deadline=None)
@given(sequence=steps)
def test_memory_equals_database_after_every_attempt(
    sequence: list[tuple[str, int, int, str | None]],
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LedgerStore.open(Path(tmp) / "d", clock=ManualClock(NOW))
        try:
            ledger = TradingLedger(store, ManualClock(NOW), COST)
            runner = Runner(ledger)
            ledger.deposit_paper_capital("seed", D(5000))
            for bot in BOTS:
                ledger.allocate(f"alloc:{bot}", bot, VENUE, "USD", D(1500), RATE)
                runner.ref.allocate(bot, D(1500))
            for kind, b, seed, fault in sequence:
                if fault:
                    setattr(store, fault, raiser(fault))
                try:
                    runner.attempt((kind, b, seed))
                except InjectedFaultError:
                    pass  # nothing committed; the reference is not updated either
                finally:
                    if fault:
                        delattr(store, fault)
                rebuilt = TradingLedger(store, ManualClock(NOW), COST)
                assert rebuilt.state_digest() == ledger.state_digest()
                reconcile(ledger, runner.ref)
            assert ledger.verify_replay() == []
            assert store.verify_audit_chain() == len(store.audit_records())
        finally:
            store.close()


@pytest.mark.parametrize("fault", FAULT_POINTS)
def test_a_failed_unit_leaves_memory_and_database_untouched(tmp_path: Path, fault: str) -> None:
    store = LedgerStore.open(tmp_path / "d", clock=ManualClock(NOW))
    try:
        ledger = TradingLedger(store, ManualClock(NOW), COST)
        ledger.deposit_paper_capital("seed", D(5000))
        before, ops = ledger.state_digest(), len(store.operations())

        def unit() -> None:
            with store.unit_of_work():  # operation + postings + transition + audit
                ledger.allocate("a1", "bot-03", VENUE, "USD", D(1500), RATE)
                ledger.open_order(ORDER)
                store.append_audit("allocation", "a1", {})

        setattr(store, fault, raiser(fault))
        with pytest.raises(InjectedFaultError):
            unit()
        delattr(store, fault)
        assert ledger.state_digest() == before
        assert len(store.operations()) == ops
        assert TradingLedger(store, ManualClock(NOW), COST).state_digest() == before
        unit()  # the identical unit succeeds once the fault is gone
        assert len(store.operations()) == ops + 2
        assert len(store.transitions()) == 1
    finally:
        store.close()
