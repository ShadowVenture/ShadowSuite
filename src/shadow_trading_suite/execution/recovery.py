"""Start-up recovery of the execution state (B2) and its reconciliation with the ledger (C1).

Order: (1) verify the audit hash chain — a broken chain raises and the engine refuses to start;
(2) fold every `exec.cmd` record's state changes in sequence, checking contiguity, version and
the recorded state digest after each record; (3) reconcile the rebuilt state with the ledger
projection and the supervisor. Any mismatch makes the engine read-only and triggers the
emergency shutdown. Consumed liquidity, queue positions, pending cancellations, checkpoints and
liquidation progress come back exactly as committed, so nothing consumed is ever handed out
again for the same book level.
"""

from shadow_trading_suite.accounting.trading import OPEN_STATES, OrderState, TradingLedger
from shadow_trading_suite.execution.model import STATE_VERSION, ExecState, apply_effects
from shadow_trading_suite.risk.supervisor import PortfolioSupervisor
from shadow_trading_suite.storage.ledger import LedgerStore

AUDIT_KIND = "exec.cmd"


class ExecutionRecoveryError(RuntimeError):
    """The recorded execution history is inconsistent; the engine must not start."""


def restore_state(store: LedgerStore) -> ExecState:
    store.verify_audit_chain()  # raises StorageError on tampering
    state = ExecState()
    for rec in store.audit_records():
        if rec.kind != AUDIT_KIND:
            continue
        p = rec.payload
        if p.get("v") != STATE_VERSION:
            raise ExecutionRecoveryError(f"audit seq {rec.seq}: unsupported version {p.get('v')}")
        if p["seq"] != state.seq + 1:
            raise ExecutionRecoveryError(
                f"audit seq {rec.seq}: execution seq {p['seq']} after {state.seq}"
            )
        state = apply_effects(state, p["effects"])
        if state.seq != p["seq"] or state.digest() != p["digest"]:
            raise ExecutionRecoveryError(f"execution state digest mismatch at seq {p['seq']}")
    return state


def reconcile(
    state: ExecState, ledger: TradingLedger, supervisor: PortfolioSupervisor
) -> list[str]:
    issues = [f"supervisor: {i}" for i in supervisor.recovery_issues]
    orders = ledger.orders()
    open_ledger = {oid for oid, r in orders.items() if r.state in OPEN_STATES}
    for oid in sorted(open_ledger - set(state.orders)):
        issues.append(f"open ledger order {oid} unknown to execution")
    for oid, order in sorted(state.orders.items()):
        record = orders.get(oid)
        if record is None or oid not in open_ledger:
            issues.append(f"execution order {oid} is not open in the ledger")
            continue
        if record.filled_qty != order.filled:
            issues.append(f"order {oid}: filled {order.filled} vs ledger {record.filled_qty}")
        pending_cancel = record.state is OrderState.CANCEL_REQUESTED
        if pending_cancel != (order.cancel_effective_at is not None):
            issues.append(f"order {oid}: cancel request state differs from the ledger")
    in_ledger = set(ledger.liquidations())
    if in_ledger != set(state.liquidations):
        issues.append(
            f"liquidations differ: ledger {sorted(in_ledger)} vs execution "
            f"{sorted(state.liquidations)}"
        )
    return issues
