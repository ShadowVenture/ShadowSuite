"""Shared test configuration: deterministic Hypothesis profile; no real data access."""

from pathlib import Path
from typing import Any

import duckdb
import pytest
from hypothesis import HealthCheck, settings

settings.register_profile(
    "deterministic",
    derandomize=True,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("deterministic")


# -- no test may open a database under the project's real data directory ----------------
# Every market, ledger and research store is a DuckDB file opened through `duckdb.connect`
# (only `storage/` imports duckdb). The guard runs before DuckDB sees the path, so a test that
# points at the real `data/` fails without touching it.

REAL_DATA_DIR = (Path(__file__).resolve().parents[1] / "data").resolve()


class RealDataAccessError(AssertionError):
    """A test tried to open a database under the real data directory."""


def guarded(connect: Any) -> Any:
    def guarded_connect(database: Any = ":memory:", *args: Any, **kwargs: Any) -> Any:
        if isinstance(database, str | Path) and str(database) not in ("", ":memory:"):
            target = Path(database).resolve()
            if target == REAL_DATA_DIR or target.is_relative_to(REAL_DATA_DIR):
                raise RealDataAccessError(f"test opened the real data directory: {target}")
        return connect(database, *args, **kwargs)

    return guarded_connect


@pytest.fixture(autouse=True)
def _no_real_data_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duckdb, "connect", guarded(duckdb.connect))
