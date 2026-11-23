"""Append-only paper trading ledger and risk audit trail (hardened DuckDB, separate file).

Schema v2 (M6-A): operations (idempotent by `op_id`), their balanced double-entry postings,
order transitions and the hash-chained risk audit. All writes go through `unit_of_work()`:
everything added to one unit is written in ONE DuckDB transaction, so a crash never leaves a
partial operation, an operation without its audit record, or an audit record without its
operation. Units nest (inner units join the outer one) and in-memory callbacks run only after
COMMIT. Nothing is ever updated or deleted. The store is single-writer: DuckDB's file lock
refuses a second writer process, one re-entrant lock serialises writers in-process, and a unit
of work can only be joined by the thread that opened it.

Failure semantics: any exception before or during COMMIT rolls the transaction back and runs no
callback. If the commit outcome is unknown (ROLLBACK after a failed COMMIT also fails) or a
post-commit callback fails, memory may no longer match the database, so the store refuses all
further units until the process restarts and rebuilds its state from the database.

Schema policy: only v2 is supported. A v1 (M5) or unknown ledger is refused WITHOUT any
modification; the operator may archive it explicitly with `archive_ledger` (hard-link into
`archive/` + SHA-256 manifest, never overwritten or deleted) and then start a fresh v2 ledger.
No automatic migration exists (M5 ledgers only ever held synthetic test data).

Live ledgers (LTR-03) are a separate file kind (`live-v1`, `storage/live_ledger.py`). This store
never opens, creates next to, or archives one: a live file has no `ledger_meta` (so
`_schema_label` calls it "unknown"), and the paper store refuses a directory that holds
`live-ledger.duckdb` before creating anything.
"""

import hashlib
import json
import os
import threading
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from shadow_trading_suite.core.time import Clock, SystemClock
from shadow_trading_suite.storage.hardened import (
    DEC,
    StorageError,
    _db,
    _py,
    decimal_fits,
    open_hardened,
)

SCHEMA_VERSION = 2
_DDL = f"""
CREATE TABLE IF NOT EXISTS ledger_meta (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_ops (
    seq BIGINT PRIMARY KEY, op_id VARCHAR NOT NULL UNIQUE, kind VARCHAR NOT NULL,
    digest VARCHAR NOT NULL, payload VARCHAR NOT NULL, effects VARCHAR NOT NULL,
    ts TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_postings (
    seq BIGINT PRIMARY KEY, op_id VARCHAR NOT NULL, entry INTEGER NOT NULL, idx INTEGER NOT NULL,
    account VARCHAR NOT NULL, currency VARCHAR NOT NULL, amount {DEC} NOT NULL,
    UNIQUE (op_id, entry, idx));
CREATE TABLE IF NOT EXISTS order_transitions (
    order_id VARCHAR NOT NULL, n INTEGER NOT NULL, op_id VARCHAR NOT NULL,
    from_state VARCHAR NOT NULL, to_state VARCHAR NOT NULL, filled_qty {DEC} NOT NULL,
    ts TIMESTAMP NOT NULL, PRIMARY KEY (order_id, n));
CREATE TABLE IF NOT EXISTS risk_audit (
    seq BIGINT PRIMARY KEY, ts TIMESTAMP NOT NULL, kind VARCHAR NOT NULL, subject VARCHAR,
    payload VARCHAR NOT NULL, prev_hash VARCHAR NOT NULL, hash VARCHAR NOT NULL);
"""
GENESIS = "0" * 64
# LTR-03: the live ledger's file name and label table (defined here so the paper store can
# refuse them without importing the live module).
LIVE_LEDGER_FILE = "live-ledger.duckdb"
LIVE_META_TABLE = "live_ledger_meta"
_V2_TABLES = frozenset(
    {"ledger_meta", "ledger_ops", "ledger_postings", "order_transitions", "risk_audit"}
)


class UnsupportedLedgerSchemaError(StorageError):
    """The ledger file has a schema this version must not touch."""


PostingRow = tuple[str, str, Decimal]  # (account, currency, signed amount: + debit, - credit)


def canonical(value: Any) -> Any:
    """JSON default: numerically equal Decimals always serialise identically (750 == 750.00)."""
    if isinstance(value, Decimal):
        return f"{value.normalize():f}" if value else "0"
    return str(value)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=canonical)


@dataclass(frozen=True)
class LedgerEvent:
    """One persisted posting of the double-entry journal."""

    seq: int
    op_id: str
    entry: int
    idx: int
    account: str
    currency: str
    amount: Decimal


@dataclass(frozen=True)
class TransitionRow:
    order_id: str
    n: int
    from_state: str
    to_state: str
    filled_qty: Decimal
    ts: datetime


@dataclass(frozen=True)
class StoredOperation:
    seq: int
    op_id: str
    kind: str
    digest: str
    payload: dict[str, Any]
    effects: dict[str, Any]
    entries: tuple[tuple[PostingRow, ...], ...]
    transitions: tuple[TransitionRow, ...]


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    ts: datetime
    kind: str
    subject: str | None
    payload: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass
class _PendingOperation:
    op_id: str
    kind: str
    digest: str
    payload: str
    effects: str
    entries: list[tuple[PostingRow, ...]]
    transitions: list[TransitionRow]


@dataclass
class UnitOfWork:
    """Writes collected for ONE transaction, plus post-commit callbacks and drafts."""

    owner: int = field(default_factory=threading.get_ident)
    operations: list[_PendingOperation] = field(default_factory=list)
    audits: list[tuple[str, str | None, str]] = field(default_factory=list)
    callbacks: list[Callable[[], None]] = field(default_factory=list)
    drafts: dict[int, Any] = field(default_factory=dict)

    def add_operation(
        self,
        op_id: str,
        kind: str,
        payload: dict[str, Any],
        effects: dict[str, Any],
        entries: Sequence[Sequence[PostingRow]],
        transitions: Sequence[TransitionRow],
    ) -> None:
        if self.pending_digest(op_id) is not None:
            raise StorageError(f"operation {op_id!r} already in this unit of work")
        rows = [tuple(entry) for entry in entries]
        for entry in rows:
            _check_entry(op_id, entry)
        self.operations.append(
            _PendingOperation(
                op_id,
                kind,
                LedgerStore.digest_for(kind, payload),
                canonical_json(payload),
                canonical_json(effects),
                rows,
                list(transitions),
            )
        )

    def add_audit(self, kind: str, subject: str | None, payload: dict[str, Any]) -> None:
        self.audits.append((kind, subject, canonical_json(payload)))

    def pending_digest(self, op_id: str) -> str | None:
        return next((o.digest for o in self.operations if o.op_id == op_id), None)

    def on_commit(self, callback: Callable[[], None]) -> None:
        self.callbacks.append(callback)


def _check_entry(op_id: str, entry: Sequence[PostingRow]) -> None:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for _, currency, amount in entry:
        if amount == 0 or not decimal_fits(amount):
            raise StorageError(f"{op_id}: invalid posting amount {amount}")
        totals[currency] += amount
    if len(entry) < 2 or any(totals.values()):
        raise StorageError(f"{op_id}: journal entry does not balance per currency")


def _audit_hash(prev: str, seq: int, kind: str, subject: str | None, payload: str) -> str:
    return hashlib.sha256(f"{prev}|{seq}|{kind}|{subject}|{payload}".encode()).hexdigest()


class LedgerStore:
    def __init__(self, con: duckdb.DuckDBPyConnection, path: Path, clock: Clock) -> None:
        self._con = con
        self.path = path
        self._clock = clock
        self.lock = threading.RLock()  # the single in-process writer lock (shared by users)
        self._active: UnitOfWork | None = None
        self._poisoned: str | None = None

    @classmethod
    def open(
        cls, data_dir: Path, *, db_name: str = "ledger.duckdb", clock: Clock | None = None
    ) -> "LedgerStore":
        _refuse_live(data_dir, db_name)
        con, path, _ = open_hardened(data_dir, db_name)
        _refuse_live_file(con, path)
        label = _schema_label(con)
        if label is None:  # empty file: initialise v2
            con.execute(_DDL)
            con.execute("INSERT INTO ledger_meta VALUES (?)", [SCHEMA_VERSION])
        elif label != f"v{SCHEMA_VERSION}":
            con.close()
            raise UnsupportedLedgerSchemaError(
                f"{path}: ledger schema {label} is not supported (this version needs "
                f"v{SCHEMA_VERSION}). Nothing was modified. To start a fresh paper ledger, "
                f"archive it explicitly with archive_ledger({data_dir!s}): the file is moved "
                "to archive/ with a SHA-256 manifest and is never deleted."
            )
        return cls(con, path, clock or SystemClock())

    @classmethod
    def open_read_only(
        cls, data_dir: Path, *, db_name: str = "ledger.duckdb", clock: Clock | None = None
    ) -> "LedgerStore":
        """Open an EXISTING v2 ledger read-only (reports: tax, audits). Never creates,
        initialises or migrates anything; every write fails in DuckDB itself."""
        _refuse_live(data_dir, db_name)
        data_dir = data_dir.resolve()
        path = (data_dir / db_name).resolve()
        if path.parent != data_dir or not path.is_file():
            raise StorageError(f"{path}: no ledger to read")
        con = duckdb.connect(
            str(path),
            read_only=True,
            config={
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "allow_community_extensions": False,
                "allow_unsigned_extensions": False,
            },
        )
        con.execute("SET TimeZone = 'UTC'")
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        _refuse_live_file(con, path)
        label = _schema_label(con)
        if label != f"v{SCHEMA_VERSION}":
            con.close()
            raise UnsupportedLedgerSchemaError(
                f"{path}: ledger schema {label} is not supported read-only (needs "
                f"v{SCHEMA_VERSION}); nothing was modified"
            )
        return cls(con, path, clock or SystemClock())

    def close(self) -> None:
        with self.lock:
            self._con.close()

    def schema_version(self) -> int:
        with self.lock:
            row = self._con.execute("SELECT version FROM ledger_meta").fetchone()
        return int(row[0]) if row else 0

    # -- unit of work ----------------------------------------------------------------------

    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        with self.lock:
            if self._active is not None:  # nested: join the outer unit (same thread only)
                yield self._join(self._active)
                return
            if self._poisoned is not None:
                raise StorageError(f"ledger store requires a restart: {self._poisoned}")
            uow = UnitOfWork()
            self._active = uow
            try:
                yield uow
            finally:
                self._active = None
            self._flush(uow)
            try:
                for callback in uow.callbacks:
                    callback()
            except BaseException as exc:
                self._poisoned = f"post-commit state update failed ({type(exc).__name__})"
                raise StorageError(
                    "committed, but the in-memory state update failed; restart required to "
                    "rebuild state from the database"
                ) from exc

    def _join(self, uow: UnitOfWork) -> UnitOfWork:
        if uow.owner != threading.get_ident():
            raise StorageError("a unit of work can only be joined by the thread that opened it")
        return uow

    def _flush(self, uow: UnitOfWork) -> None:
        if not uow.operations and not uow.audits:
            return
        now = _db(self._clock.now())
        self._con.execute("BEGIN TRANSACTION")
        try:
            op_seq = self._max("ledger_ops")
            posting_seq = self._max("ledger_postings")
            for op in uow.operations:
                if self._committed_digest(op.op_id) is not None:
                    raise StorageError(f"operation {op.op_id!r} already recorded")
                op_seq += 1
                self._insert_operation(op_seq, op, now)
                for entry_idx, entry in enumerate(op.entries):
                    for idx, row in enumerate(entry):
                        posting_seq += 1
                        self._insert_event(posting_seq, op.op_id, entry_idx, idx, row)
                for t in op.transitions:
                    self._insert_transition(op.op_id, t)
            last = self._con.execute(
                "SELECT seq, hash FROM risk_audit ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            seq, prev = (int(last[0]), str(last[1])) if last else (0, GENESIS)
            for kind, subject, body in uow.audits:
                seq += 1
                prev = self._insert_audit(seq, now, kind, subject, body, prev)
            self._commit()
        except BaseException:
            try:
                self._con.execute("ROLLBACK")
            except Exception:
                self._poisoned = "commit outcome unknown"
            raise

    def _commit(self) -> None:
        self._con.execute("COMMIT")

    def _max(self, table: str) -> int:
        sql = f"SELECT coalesce(max(seq), 0) FROM {table}"  # noqa: S608 - fixed table names
        row = self._con.execute(sql).fetchone()
        return int(row[0]) if row else 0

    def _insert_operation(self, seq: int, op: _PendingOperation, now: datetime) -> None:
        self._con.execute(
            "INSERT INTO ledger_ops VALUES (?, ?, ?, ?, ?, ?, ?)",
            [seq, op.op_id, op.kind, op.digest, op.payload, op.effects, now],
        )

    def _insert_event(self, seq: int, op_id: str, entry: int, idx: int, row: PostingRow) -> None:
        account, currency, amount = row
        self._con.execute(
            "INSERT INTO ledger_postings VALUES (?, ?, ?, ?, ?, ?, ?)",
            [seq, op_id, entry, idx, account, currency, amount],
        )

    def _insert_transition(self, op_id: str, t: TransitionRow) -> None:
        if not decimal_fits(t.filled_qty):
            raise StorageError("filled quantity exceeds DECIMAL(38,18)")
        self._con.execute(
            "INSERT INTO order_transitions VALUES (?, ?, ?, ?, ?, ?, ?)",
            [t.order_id, t.n, op_id, t.from_state, t.to_state, t.filled_qty, _db(t.ts)],
        )

    def _insert_audit(
        self, seq: int, now: datetime, kind: str, subject: str | None, body: str, prev: str
    ) -> str:
        digest = _audit_hash(prev, seq, kind, subject, body)
        self._con.execute(
            "INSERT INTO risk_audit VALUES (?, ?, ?, ?, ?, ?, ?)",
            [seq, now, kind, subject, body, prev, digest],
        )
        return digest

    # -- operations ------------------------------------------------------------------------

    @staticmethod
    def digest_for(kind: str, payload: dict[str, Any]) -> str:
        body = canonical_json(payload)
        return hashlib.sha256(f"{kind}|{body}".encode()).hexdigest()

    def _committed_digest(self, op_id: str) -> str | None:
        row = self._con.execute("SELECT digest FROM ledger_ops WHERE op_id = ?", [op_id]).fetchone()
        return str(row[0]) if row else None

    def operation_digest(self, op_id: str) -> str | None:
        """Digest of a committed or (inside the current unit of work) pending operation."""
        with self.lock:
            if self._active is not None and (pending := self._active.pending_digest(op_id)):
                return pending
            return self._committed_digest(op_id)

    def operation_effects(self, op_id: str) -> dict[str, Any] | None:
        with self.lock:
            if self._active is not None:
                for op in self._active.operations:
                    if op.op_id == op_id:
                        return dict(json.loads(op.effects))
            row = self._con.execute(
                "SELECT effects FROM ledger_ops WHERE op_id = ?", [op_id]
            ).fetchone()
        return dict(json.loads(row[0])) if row else None

    def operations(self) -> list[StoredOperation]:
        with self.lock:
            ops = self._con.execute(
                "SELECT seq, op_id, kind, digest, payload, effects FROM ledger_ops ORDER BY seq"
            ).fetchall()
            postings = self.events()
            transitions = self.transitions_by_op()
        entries: dict[str, dict[int, list[PostingRow]]] = defaultdict(lambda: defaultdict(list))
        for e in postings:
            entries[e.op_id][e.entry].append((e.account, e.currency, e.amount))
        return [
            StoredOperation(
                seq,
                op_id,
                kind,
                digest,
                json.loads(payload),
                json.loads(effects),
                tuple(tuple(rows) for _, rows in sorted(entries[op_id].items())),
                tuple(transitions.get(op_id, ())),
            )
            for seq, op_id, kind, digest, payload, effects in ops
        ]

    def payloads_after(self, kind: str, seq: int) -> list[tuple[int, dict[str, Any]]]:
        """Read-only: (seq, payload) of the operations of one kind after `seq` (LM1-B B11
        reads new fills incrementally instead of every operation each tick)."""
        with self.lock:
            rows = self._con.execute(
                "SELECT seq, payload FROM ledger_ops WHERE kind = ? AND seq > ? ORDER BY seq",
                [kind, seq],
            ).fetchall()
        return [(s, json.loads(p)) for s, p in rows]

    def events(self) -> list[LedgerEvent]:
        with self.lock:
            rows = self._con.execute(
                "SELECT seq, op_id, entry, idx, account, currency, amount "
                "FROM ledger_postings ORDER BY seq"
            ).fetchall()
        return [LedgerEvent(*r) for r in rows]

    def transitions(self) -> list[TransitionRow]:
        return [t for ts in self.transitions_by_op().values() for t in ts]

    def transitions_by_op(self) -> dict[str, list[TransitionRow]]:
        with self.lock:
            rows = self._con.execute(
                "SELECT t.op_id, t.order_id, t.n, t.from_state, t.to_state, t.filled_qty, t.ts "
                "FROM order_transitions t JOIN ledger_ops o ON o.op_id = t.op_id "
                "ORDER BY o.seq, t.order_id, t.n"
            ).fetchall()
        result: dict[str, list[TransitionRow]] = defaultdict(list)
        for op_id, order_id, n, from_state, to_state, qty, ts in rows:
            result[op_id].append(TransitionRow(order_id, n, from_state, to_state, qty, _py(ts)))
        return dict(result)

    # -- risk audit trail ------------------------------------------------------------------

    def append_audit(self, kind: str, subject: str | None, payload: dict[str, Any]) -> None:
        """Audit record in the current unit of work (or its own transaction)."""
        with self.unit_of_work() as uow:
            uow.add_audit(kind, subject, payload)

    def audit_records(self) -> list[AuditRecord]:
        with self.lock:
            rows = self._con.execute(
                "SELECT seq, ts, kind, subject, payload, prev_hash, hash "
                "FROM risk_audit ORDER BY seq"
            ).fetchall()
        return [
            AuditRecord(r[0], _py(r[1]), r[2], r[3], json.loads(r[4]), r[5], r[6]) for r in rows
        ]

    def verify_audit_chain(self) -> int:
        """Recompute the hash chain; raises StorageError on any tampering. Returns length."""
        prev = GENESIS
        records = self.audit_records()
        for rec in records:
            body = canonical_json(rec.payload)
            if rec.prev_hash != prev or rec.hash != _audit_hash(
                prev, rec.seq, rec.kind, rec.subject, body
            ):
                raise StorageError(f"audit chain broken at seq {rec.seq}")
            prev = rec.hash
        return len(records)


def _refuse_live(data_dir: Path, db_name: str) -> None:
    """A paper ledger is never opened under the live file name or next to a live ledger."""
    if db_name == LIVE_LEDGER_FILE:
        raise UnsupportedLedgerSchemaError(
            f"{db_name} is the live ledger file name; the paper store never opens it"
        )
    live = data_dir / LIVE_LEDGER_FILE
    if live.exists() or live.is_symlink():
        raise UnsupportedLedgerSchemaError(
            f"{data_dir}: the directory holds a LIVE ledger ({LIVE_LEDGER_FILE}); a paper "
            "ledger is never opened or created next to it. Nothing was modified."
        )


def _tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }


def _refuse_live_file(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    if LIVE_META_TABLE in _tables(con):
        con.close()
        raise UnsupportedLedgerSchemaError(
            f"{path}: this is a LIVE ledger (live-v1); the paper store never opens it. "
            "Nothing was modified."
        )


def _schema_label(con: duckdb.DuckDBPyConnection) -> str | None:
    """None for an empty database, else "v<N>" or "unknown"."""
    tables = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    if not tables:
        return None
    if "ledger_meta" not in tables:
        return "unknown"
    row = con.execute("SELECT max(version) FROM ledger_meta").fetchone()
    version = row[0] if row else None
    if version == SCHEMA_VERSION and not tables >= _V2_TABLES:
        return "unknown"
    return f"v{version}" if version is not None else "unknown"


def archive_ledger(
    data_dir: Path, *, db_name: str = "ledger.duckdb", clock: Clock | None = None
) -> Path:
    """EXPLICIT operator action: move an unsupported ledger into `archive/`, never deleting or
    overwriting anything. Returns the archived path (a `.sha256` manifest is written next to it).
    """
    if db_name == LIVE_LEDGER_FILE:
        raise ValueError(f"{db_name} is the live ledger file name; refusing to archive it")
    path = data_dir / db_name
    con = duckdb.connect(
        str(path),
        read_only=True,
        config={
            "enable_external_access": False,
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
        },
    )
    try:
        live = LIVE_META_TABLE in _tables(con)
        label = _schema_label(con)
    finally:
        con.close()
    if live:
        raise ValueError(f"{path} is a LIVE ledger (live-v1); refusing to archive it")
    if label == f"v{SCHEMA_VERSION}":
        raise ValueError(f"{path} is a current v{SCHEMA_VERSION} ledger; refusing to archive it")
    archive = data_dir / "archive"
    archive.mkdir(mode=0o700, exist_ok=True)
    stamp = (clock or SystemClock()).now().strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for n in range(1000):
        target = archive / f"ledger-{label}-{stamp}{f'-{n}' if n else ''}.duckdb"
        try:
            os.link(path, target)  # fails instead of overwriting an existing archive
        except FileExistsError:
            continue
        break
    else:
        raise StorageError("could not find a free archive name")
    os.chmod(target, 0o600)
    manifest = target.with_suffix(target.suffix + ".sha256")
    manifest.write_text(f"{digest}  {target.name}  (archived from {path.name})\n")
    os.chmod(manifest, 0o600)
    wal = path.with_suffix(path.suffix + ".wal")
    if wal.exists():
        os.link(wal, target.with_suffix(target.suffix + ".wal"))
        wal.unlink()
    path.unlink()
    return target
