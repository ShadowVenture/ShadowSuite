"""Ledger store v2: one transaction per unit of work, idempotent operations, audit in-txn."""

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.storage.hardened import StorageError
from shadow_trading_suite.storage.ledger import LedgerStore, TransitionRow, UnitOfWork

D = Decimal
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
ENTRY = (("a@cryptocom:cash", "USD", D("-5")), ("a@cryptocom:reserved", "USD", D("5")))
TRANSITION = TransitionRow("o2", 1, "new", "accepted", D(0), NOW)


class AbortError(RuntimeError):
    pass


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LedgerStore]:
    s = LedgerStore.open(tmp_path / "data", clock=ManualClock(NOW))
    yield s
    s.close()


def add(store: LedgerStore, op_id: str, n_audit: int = 1) -> None:
    with store.unit_of_work() as uow:
        uow.add_operation(
            op_id,
            "hold",
            {"x": 1},
            {"result": None},
            [ENTRY],
            [TransitionRow("o1", 1, "new", "accepted", D(0), NOW)],
        )
        for i in range(n_audit):
            uow.add_audit("k", op_id, {"i": i})


def run_unit(store: LedgerStore, body: Callable[[UnitOfWork], None]) -> None:
    """One complete unit of work (so `pytest.raises` wraps a single call)."""
    with store.unit_of_work() as uow:
        body(uow)


def abort(_: UnitOfWork) -> None:
    raise AbortError("abort")


def test_unit_of_work_commits_everything_together(store: LedgerStore) -> None:
    add(store, "op1")
    ops = store.operations()
    assert [o.op_id for o in ops] == ["op1"]
    assert ops[0].entries == (ENTRY,)
    assert store.transitions()[0].to_state == "accepted"
    assert [r.kind for r in store.audit_records()] == ["k"]
    assert store.verify_audit_chain() == 1


@pytest.mark.parametrize(
    "target",
    ["_insert_operation", "_insert_event", "_insert_transition", "_insert_audit", "_commit"],
)
def test_failure_at_any_insert_or_commit_rolls_back_everything(
    store: LedgerStore, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store, "op0")
    committed: list[str] = []

    def boom(*args: object, **kwargs: object) -> None:
        raise AbortError("crash")

    def body(uow: UnitOfWork) -> None:
        uow.add_operation("op1", "hold", {}, {}, [ENTRY], [TRANSITION])
        uow.add_audit("k", "op1", {})
        uow.on_commit(lambda: committed.append("op1"))

    monkeypatch.setattr(store, target, boom)
    with pytest.raises(AbortError):
        run_unit(store, body)
    monkeypatch.undo()
    assert [o.op_id for o in store.operations()] == ["op0"]
    assert len(store.events()) == 2
    assert len(store.transitions()) == 1
    assert len(store.audit_records()) == 1
    assert committed == []  # in-memory callbacks only after COMMIT
    run_unit(store, body)  # the same unit succeeds once the fault is gone
    assert [o.op_id for o in store.operations()] == ["op0", "op1"]
    assert committed == ["op1"]


def test_exception_in_body_writes_nothing(store: LedgerStore) -> None:
    def body(uow: UnitOfWork) -> None:
        uow.add_audit("k", None, {})
        abort(uow)

    with pytest.raises(AbortError):
        run_unit(store, body)
    assert store.audit_records() == []


def test_nested_units_join_the_outer_transaction(store: LedgerStore) -> None:
    def body(uow: UnitOfWork) -> None:
        store.append_audit("inner", None, {})
        abort(uow)

    with pytest.raises(AbortError):
        run_unit(store, body)
    assert store.audit_records() == []
    with store.unit_of_work():
        store.append_audit("inner", None, {})
        assert store.audit_records() == []  # not yet committed
    assert len(store.audit_records()) == 1


def test_duplicate_operation_id_refused_atomically(store: LedgerStore) -> None:
    add(store, "op1")
    with pytest.raises(StorageError, match="op1"):
        add(store, "op1")
    assert len(store.audit_records()) == 1


def test_identical_duplicate_in_a_later_unit_is_refused_atomically(store: LedgerStore) -> None:
    add(store, "op1")

    def body(uow: UnitOfWork) -> None:
        uow.add_audit("would-be-orphan", None, {})
        uow.add_operation("op1", "hold", {"x": 1}, {"result": None}, [ENTRY], [])

    with pytest.raises(StorageError, match="already"):
        run_unit(store, body)
    assert [r.kind for r in store.audit_records()] == ["k"]


def test_duplicate_inside_one_unit_is_refused(store: LedgerStore) -> None:
    def body(uow: UnitOfWork) -> None:
        uow.add_operation("op1", "hold", {}, {}, [ENTRY], [])
        uow.add_operation("op1", "hold", {}, {}, [ENTRY], [])

    with pytest.raises(StorageError, match="already"):
        run_unit(store, body)
    assert store.operations() == []


def test_pending_digest_visible_inside_the_unit(store: LedgerStore) -> None:
    with store.unit_of_work() as uow:
        uow.add_operation("op1", "hold", {"x": 1}, {}, [ENTRY], [])
        assert store.operation_digest("op1") == LedgerStore.digest_for("hold", {"x": 1})
    assert store.operation_digest("op1") is not None


def test_unbalanced_rows_refused_by_store(store: LedgerStore) -> None:
    unbalanced = (("a@cryptocom:cash", "USD", D("1")), ("a@cryptocom:reserved", "USD", D("-2")))
    with pytest.raises(StorageError, match="balance"), store.unit_of_work() as uow:
        uow.add_operation("bad", "x", {}, {}, [unbalanced], [])
    assert store.operations() == []


def test_survives_reopen(tmp_path: Path) -> None:
    s = LedgerStore.open(tmp_path / "d", clock=ManualClock(NOW))
    add(s, "op1")
    s.close()
    s2 = LedgerStore.open(tmp_path / "d", clock=ManualClock(NOW))
    try:
        assert [o.op_id for o in s2.operations()] == ["op1"]
        assert s2.verify_audit_chain() == 1
        assert s2.schema_version() == 2
    finally:
        s2.close()


def test_failed_commit_rolls_back_runs_no_callback_and_store_stays_usable(
    store: LedgerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    def commit_fails() -> None:
        raise AbortError("COMMIT failed")

    def body(uow: UnitOfWork) -> None:
        uow.add_operation("op1", "hold", {}, {}, [ENTRY], [])
        uow.on_commit(lambda: ran.append("x"))

    monkeypatch.setattr(store, "_commit", commit_fails)
    with pytest.raises(AbortError, match="COMMIT"):
        run_unit(store, body)
    monkeypatch.undo()
    assert ran == []
    assert store.operations() == []
    add(store, "op2")  # the connection is not left inside an aborted transaction
    assert [o.op_id for o in store.operations()] == ["op2"]


def test_unknown_commit_outcome_poisons_the_store(
    store: LedgerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def commit_then_fail() -> None:
        store._con.execute("COMMIT")
        raise AbortError("connection lost after COMMIT")  # ROLLBACK will now fail

    monkeypatch.setattr(store, "_commit", commit_then_fail)
    with pytest.raises(AbortError):
        add(store, "op1")
    monkeypatch.undo()
    with pytest.raises(StorageError, match="restart"):
        add(store, "op2")


def test_failing_callback_poisons_the_store_until_restart(tmp_path: Path) -> None:
    store = LedgerStore.open(tmp_path / "d", clock=ManualClock(NOW))

    def broken() -> None:
        raise AbortError("projection update failed")

    def body(uow: UnitOfWork) -> None:
        uow.add_audit("k", None, {})
        uow.on_commit(broken)

    try:
        with pytest.raises(StorageError, match="restart"):
            run_unit(store, body)
        assert len(store.audit_records()) == 1  # committed: memory may now be stale
        with pytest.raises(StorageError, match="restart"):
            add(store, "op1")
    finally:
        store.close()
    reopened = LedgerStore.open(tmp_path / "d", clock=ManualClock(NOW))
    try:
        add(reopened, "op1")  # a restart rebuilds from the database and clears the condition
        assert len(reopened.operations()) == 1
    finally:
        reopened.close()


def test_other_threads_never_join_an_open_unit_of_work(store: LedgerStore) -> None:
    entered = threading.Event()
    errors: list[BaseException] = []

    def other() -> None:
        entered.wait()
        try:
            with store.unit_of_work() as uow:  # blocks until the first unit is finished
                uow.add_audit("other-thread", None, {})
        except BaseException as exc:  # pragma: no cover - reported via `errors`
            errors.append(exc)

    t = threading.Thread(target=other)
    t.start()

    def body(uow: UnitOfWork) -> None:
        uow.add_audit("first", None, {})
        entered.set()
        t.join(timeout=0.5)
        assert t.is_alive()  # waiting on the writer lock, not adopting this unit
        abort(uow)

    with pytest.raises(AbortError):
        run_unit(store, body)
    t.join(timeout=10)
    assert errors == []
    assert [r.kind for r in store.audit_records()] == ["other-thread"]


def test_joining_a_unit_from_another_thread_is_refused(store: LedgerStore) -> None:
    result: list[BaseException] = []
    with store.unit_of_work() as uow:
        t = threading.Thread(target=lambda: result.append(_try_join(store, uow)))
        t.start()
        t.join()
    assert isinstance(result[0], StorageError)


def _try_join(store: LedgerStore, uow: UnitOfWork) -> BaseException:
    try:
        store._join(uow)
    except StorageError as exc:
        return exc
    return AssertionError("joined from another thread")
