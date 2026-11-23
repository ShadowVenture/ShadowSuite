"""Hardened DuckDB connections (the only place besides `storage/ledger.py` that imports duckdb).

Security: extensions cannot be installed or loaded, file access is confined to the data
directory, network/HTTP file access is impossible and the configuration is locked. The
database file is mode 600 inside a mode-700 directory.

Timestamps: columns are TIMESTAMP holding UTC by contract; `_db` / `_py` convert explicitly and
naive datetimes are rejected on input.
"""

import importlib.util
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# DuckDB's Python client probes `import pandas` for every bound parameter. pandas is not a
# dependency, so each probe is a fresh, uncached filesystem search. Caching the negative import
# result is Python's standard mechanism and only applies when pandas is genuinely not installed.
if "pandas" not in sys.modules and importlib.util.find_spec("pandas") is None:
    sys.modules["pandas"] = None  # type: ignore[assignment]

import duckdb

from shadow_trading_suite.core.fs import private_dirs, private_umask
from shadow_trading_suite.core.time import ensure_utc

MAX_SCALE = 18
MAX_INTEGER_DIGITS = 20
DEC = "DECIMAL(38,18)"


class StorageError(Exception):
    pass


def decimal_fits(value: Decimal) -> bool:
    """True if DECIMAL(38,18) stores `value` exactly (no silent rounding or overflow)."""
    _sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        return False
    scale = max(0, -exponent)
    integer_digits = max(0, len(digits) + exponent)
    return scale <= MAX_SCALE and integer_digits <= MAX_INTEGER_DIGITS


def _db(value: Any) -> Any:
    """Python -> DuckDB: aware datetimes become naive UTC; everything else unchanged."""
    if isinstance(value, datetime):
        return ensure_utc(value).replace(tzinfo=None)
    return value


def _py(value: Any) -> Any:
    """DuckDB -> Python: naive TIMESTAMP values are UTC by contract."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC)
    return value


_HARDENED: dict[str, str | bool | int | float | list[str]] = {
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "allow_community_extensions": False,
    "allow_unsigned_extensions": False,
}


def open_hardened(
    data_dir: Path, db_name: str, *, read_only: bool = False
) -> tuple[duckdb.DuckDBPyConnection, Path, Path]:
    """Hardened DuckDB connection confined to `data_dir`.

    `read_only`: the file must exist; nothing is created, re-moded or written."""
    data_dir = data_dir.resolve()
    path = (data_dir / db_name).resolve()
    if not path.is_relative_to(data_dir) or path.parent != data_dir:
        raise StorageError("database file must be inside the data directory")
    if read_only:
        if not path.is_file():
            raise StorageError(f"no database {db_name} in {data_dir}")
        con = duckdb.connect(str(path), read_only=True, config=dict(_HARDENED))
        _lock_down(con, data_dir)
        return con, path, data_dir
    private_dirs(data_dir)
    os.chmod(data_dir, 0o700)
    # DuckDB creates `<db>.wal` at the first write and `<db>.tmp/` when it spills, later.
    # Command line entry points therefore hold `private_umask` for their whole run
    # (`core.fs.owner_only_files`).
    with private_umask():
        con = duckdb.connect(str(path), config=dict(_HARDENED))
    _lock_down(con, data_dir)
    os.chmod(path, 0o600)
    return con, path, data_dir


def _lock_down(con: duckdb.DuckDBPyConnection, data_dir: Path) -> None:
    con.execute("SET TimeZone = 'UTC'")
    con.execute("SET allowed_directories = [?]", [f"{data_dir}/"])
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
