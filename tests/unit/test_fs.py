"""Owner-only directory creation and the process umask."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shadow_trading_suite.core.fs import (
    PRIVATE_UMASK,
    owner_only_files,
    private_dirs,
    private_umask,
)


def _mode(p: Path) -> int:
    return p.stat().st_mode & 0o777


def test_every_created_level_is_owner_only(tmp_path: Path) -> None:
    previous = os.umask(0o002)  # the usual desktop umask that left 0775 parents before
    try:
        leaf = private_dirs(tmp_path / "a" / "b" / "c")
    finally:
        os.umask(previous)
    for p in (tmp_path / "a", tmp_path / "a" / "b", leaf):
        assert _mode(p) == 0o700, p


def test_existing_directories_are_never_re_moded(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o750)
    os.chmod(shared, 0o750)  # noqa: S103 - deliberately group-readable fixture
    private_dirs(shared / "store")
    assert _mode(shared) == 0o750
    assert _mode(shared / "store") == 0o700


def test_exist_ok(tmp_path: Path) -> None:
    d = private_dirs(tmp_path / "x")
    assert private_dirs(d) == d
    with pytest.raises(FileExistsError):
        private_dirs(d, exist_ok=False)
    assert private_dirs(tmp_path / "y" / "z", exist_ok=False).is_dir()


def test_relative_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    private_dirs(Path("rel") / "deep")
    assert _mode(tmp_path / "rel") == 0o700
    assert _mode(tmp_path / "rel" / "deep") == 0o700


def test_call_sites_create_owner_only_parents(tmp_path: Path) -> None:
    """Representative production writers: every level they create is 0700."""
    from shadow_trading_suite.exchanges.ratelimit import SharedFileTokenBucket
    from shadow_trading_suite.observability.status import write_status

    previous = os.umask(0o002)
    try:
        write_status(tmp_path / "v" / "reports", "runtime", {"x": 1}, datetime.now(UTC))
        SharedFileTokenBucket(tmp_path / "rl" / "shared", "coinbase", 1.0, 1)
    finally:
        os.umask(previous)
    for d in ("v", "v/reports", "rl", "rl/shared"):
        assert _mode(tmp_path / d) == 0o700, d


def _umask() -> int:
    current = os.umask(0o077)
    os.umask(current)
    return current


def test_private_umask_holds_0077_and_restores_the_callers_umask() -> None:
    previous = os.umask(0o002)
    try:
        with private_umask():
            assert _umask() == PRIVATE_UMASK == 0o077
        assert _umask() == 0o002
    finally:
        os.umask(previous)


def test_private_umask_never_loosens_a_stricter_caller() -> None:
    previous = os.umask(0o277)
    try:
        with private_umask():
            assert _umask() == 0o277
        assert _umask() == 0o277
    finally:
        os.umask(previous)


def test_owner_only_files_covers_the_whole_call_and_restores_after_an_error(
    tmp_path: Path,
) -> None:
    @owner_only_files
    def main(name: str, *, fail: bool = False) -> int:
        """Docstring kept."""
        (tmp_path / name).write_text("x")
        (tmp_path / f"{name}.d").mkdir()
        if fail:
            raise RuntimeError("boom")
        return 7

    previous = os.umask(0o002)
    try:
        assert main("a") == 7
        with pytest.raises(RuntimeError):
            main("b", fail=True)
        assert _umask() == 0o002
    finally:
        os.umask(previous)
    assert main.__name__ == "main"
    assert main.__doc__ == "Docstring kept."
    for n in ("a", "b"):
        assert _mode(tmp_path / n) == 0o600
        assert _mode(tmp_path / f"{n}.d") == 0o700
