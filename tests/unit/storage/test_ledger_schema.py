"""Ledger schema policy: v2 only; an M5 (v1) or unknown ledger is never modified or discarded.

Policy (M6-A): there is no automatic migration. Opening a v1 or unknown ledger is refused
WITHOUT touching the file; the operator may archive it explicitly with `archive_ledger`
(atomic rename into `archive/` plus a SHA-256 manifest, never deleted), then start a fresh v2
ledger. M5 ledgers only ever held synthetic test data, so no migration is provided.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.storage.ledger import (
    SCHEMA_VERSION,
    LedgerStore,
    UnsupportedLedgerSchemaError,
    archive_ledger,
)

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
M5_DDL = """
CREATE TABLE ledger_meta (version INTEGER NOT NULL);
INSERT INTO ledger_meta VALUES (1);
CREATE TABLE capital_ops (op_id VARCHAR PRIMARY KEY, kind VARCHAR NOT NULL,
    digest VARCHAR NOT NULL, payload VARCHAR NOT NULL, ts TIMESTAMP NOT NULL);
INSERT INTO capital_ops VALUES ('seed', 'deposit', 'abc', '{"eur": "5000"}', '2026-10-19 00:00');
CREATE TABLE capital_events (seq BIGINT PRIMARY KEY, op_id VARCHAR NOT NULL, idx INTEGER,
    kind VARCHAR, bot_id VARCHAR, venue VARCHAR, currency VARCHAR, amount DECIMAL(38,18),
    reference VARCHAR, ts TIMESTAMP);
"""


def make_db(path: Path, ddl: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(ddl)
    con.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tables(path: Path) -> set[str]:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {
            r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()
        }
    finally:
        con.close()


def test_fresh_ledger_is_created_at_the_current_version(tmp_path: Path) -> None:
    store = LedgerStore.open(tmp_path / "d", clock=ManualClock(NOW))
    try:
        assert store.schema_version() == SCHEMA_VERSION == 2
    finally:
        store.close()


@pytest.mark.parametrize(
    ("ddl", "label"),
    [
        (M5_DDL, "v1"),
        ("CREATE TABLE ledger_meta (version INTEGER); INSERT INTO ledger_meta VALUES (3);", "v3"),
        (
            "CREATE TABLE something_else (x INTEGER); INSERT INTO something_else VALUES (1);",
            "unknown",
        ),
    ],
)
def test_unsupported_ledger_is_refused_without_any_modification(
    tmp_path: Path, ddl: str, label: str
) -> None:
    path = tmp_path / "d" / "ledger.duckdb"
    before_hash = make_db(path, ddl)
    before_tables = tables(path)
    with pytest.raises(UnsupportedLedgerSchemaError, match=label) as exc:
        LedgerStore.open(tmp_path / "d", clock=ManualClock(NOW))
    assert "archive_ledger" in str(exc.value)  # the explicit procedure is stated
    assert tables(path) == before_tables  # no v2 tables were added
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash


def test_explicit_archive_preserves_the_old_ledger_and_allows_a_fresh_start(tmp_path: Path) -> None:
    path = tmp_path / "d" / "ledger.duckdb"
    digest = make_db(path, M5_DDL)
    archived = archive_ledger(tmp_path / "d", clock=ManualClock(NOW))
    assert not path.exists()
    assert archived.parent == tmp_path / "d" / "archive"
    assert hashlib.sha256(archived.read_bytes()).hexdigest() == digest
    manifest = archived.with_suffix(archived.suffix + ".sha256").read_text()
    assert digest in manifest
    assert archived.stat().st_mode & 0o777 == 0o600
    con = duckdb.connect(str(archived), read_only=True)
    assert con.execute("SELECT op_id FROM capital_ops").fetchall() == [("seed",)]  # data intact
    con.close()
    store = LedgerStore.open(tmp_path / "d", clock=ManualClock(NOW))
    try:
        assert store.schema_version() == 2
        assert store.operations() == []
    finally:
        store.close()


def test_archive_never_overwrites_an_existing_archive(tmp_path: Path) -> None:
    make_db(tmp_path / "d" / "ledger.duckdb", M5_DDL)
    first = archive_ledger(tmp_path / "d", clock=ManualClock(NOW))
    make_db(tmp_path / "d" / "ledger.duckdb", M5_DDL)
    second = archive_ledger(tmp_path / "d", clock=ManualClock(NOW))
    assert first != second
    assert first.exists()
    assert second.exists()


def test_archive_refuses_a_current_ledger(tmp_path: Path) -> None:
    LedgerStore.open(tmp_path / "d", clock=ManualClock(NOW)).close()
    with pytest.raises(ValueError, match="current"):
        archive_ledger(tmp_path / "d", clock=ManualClock(NOW))
