"""Req. 4: rate limiting that stays correct across processes/containers on one host."""

import multiprocessing
import os
import stat
import time
from pathlib import Path

import pytest

from shadow_trading_suite.exchanges.ratelimit import RateLimiter, SharedFileTokenBucket, TokenBucket
from tests.unit.exchanges.fakes import FakeTime


class TestTryAcquire:
    def test_token_bucket_try_acquire_reports_wait_without_consuming(self) -> None:
        t = FakeTime()
        bucket = TokenBucket(rate_per_s=2, burst=1, monotonic=t.monotonic, sleep=t.sleep)
        assert bucket.try_acquire() == 0.0
        wait = bucket.try_acquire()
        assert wait == pytest.approx(0.5)
        t.now += wait
        assert bucket.try_acquire() == 0.0

    def test_both_implementations_satisfy_protocol(self, tmp_path: Path) -> None:
        t = FakeTime()
        limiters: list[RateLimiter] = [
            TokenBucket(1, 1, monotonic=t.monotonic, sleep=t.sleep),
            SharedFileTokenBucket(tmp_path / "rl", "coinbase-rest", 1, 1, t.monotonic, t.sleep),
        ]
        assert all(lim.try_acquire() == 0.0 for lim in limiters)


class TestSharedFileTokenBucket:
    def make(self, path: Path, t: FakeTime, key: str = "coinbase-rest") -> SharedFileTokenBucket:
        return SharedFileTokenBucket(
            path, key, rate_per_s=10, burst=2, monotonic=t.monotonic, sleep=t.sleep
        )

    def test_two_instances_share_one_budget(self, tmp_path: Path) -> None:
        t = FakeTime()
        collector, engine = self.make(tmp_path, t), self.make(tmp_path, t)
        collector.acquire()
        engine.acquire()  # burst of 2 spent across BOTH instances
        assert t.sleeps == []
        collector.acquire()
        assert t.sleeps == [pytest.approx(0.1)]

    def test_separate_keys_are_independent(self, tmp_path: Path) -> None:
        t = FakeTime()
        a, b = self.make(tmp_path, t, "coinbase-rest"), self.make(tmp_path, t, "cryptocom-rest")
        for _ in range(2):
            a.acquire()
            b.acquire()
        assert t.sleeps == []

    def test_state_file_is_private(self, tmp_path: Path) -> None:
        t = FakeTime()
        self.make(tmp_path / "shared", t).acquire()
        assert stat.S_IMODE(os.stat(tmp_path / "shared").st_mode) == 0o700
        [state] = (tmp_path / "shared").iterdir()
        assert stat.S_IMODE(os.stat(state).st_mode) == 0o600

    @pytest.mark.parametrize("key", ["", "../x", "a/b", "x" * 65])
    def test_unsafe_keys_rejected(self, tmp_path: Path, key: str) -> None:
        with pytest.raises(ValueError, match="key"):
            SharedFileTokenBucket(tmp_path, key, 1, 1)

    def test_corrupt_state_fails_closed_to_empty_bucket(self, tmp_path: Path) -> None:
        t = FakeTime()
        bucket = self.make(tmp_path, t)
        bucket.acquire()
        (tmp_path / "coinbase-rest.bucket").write_text("not json")
        assert bucket.try_acquire() > 0  # corrupt state never grants a fresh burst


def _worker(path: str, n: int, out: "multiprocessing.Queue[float]") -> None:
    bucket = SharedFileTokenBucket(Path(path), "venue", rate_per_s=20, burst=1)
    for _ in range(n):
        bucket.acquire()
        out.put(time.monotonic())


def test_real_processes_never_exceed_the_shared_rate(tmp_path: Path) -> None:
    """3 separate OS processes x 10 requests at 20/s (burst 1): ≥ 29/20 s overall."""
    ctx = multiprocessing.get_context("spawn")
    out: multiprocessing.Queue[float] = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(str(tmp_path), 10, out)) for _ in range(3)]
    for p in procs:
        p.start()
    stamps = sorted(out.get(timeout=30) for _ in range(30))
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0
    assert stamps[-1] - stamps[0] >= 29 / 20 * 0.95
    # no 1-second window may contain more than rate + burst grants
    for i, s in enumerate(stamps):
        assert sum(1 for x in stamps[i:] if x - s < 1.0) <= 21


class TestCrashSafety:
    def test_state_is_rewritten_in_place_never_truncated(self, tmp_path: Path) -> None:
        """A crash mid-update must never leave an empty file (which would grant a burst)."""
        t = FakeTime()
        bucket = SharedFileTokenBucket(tmp_path, "venue", 10, 2, t.monotonic, t.sleep)
        bucket.acquire()
        size = (tmp_path / "venue.bucket").stat().st_size
        for _ in range(5):
            bucket.try_acquire()
            assert (tmp_path / "venue.bucket").stat().st_size == size

    def test_partially_written_state_fails_closed(self, tmp_path: Path) -> None:
        t = FakeTime()
        bucket = SharedFileTokenBucket(tmp_path, "venue", 10, 5, t.monotonic, t.sleep)
        bucket.acquire()
        path = tmp_path / "venue.bucket"
        path.write_bytes(path.read_bytes()[:7])  # torn write
        assert bucket.try_acquire() > 0


def _hold_lock_then_die(path: str) -> None:
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.kill(os.getpid(), 9)  # SIGKILL while holding the lock


def test_lock_held_by_a_crashed_process_is_released(tmp_path: Path) -> None:
    bucket = SharedFileTokenBucket(tmp_path, "venue", rate_per_s=100, burst=1)
    bucket.acquire()
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_hold_lock_then_die, args=(str(tmp_path / "venue.bucket"),))
    proc.start()
    proc.join(timeout=30)
    assert proc.exitcode == -9
    start = time.monotonic()
    bucket.acquire()  # the kernel released the dead process's flock: no stale lock
    assert time.monotonic() - start < 1.0


def test_crash_during_state_write_never_grants_a_fresh_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = FakeTime()
    bucket = SharedFileTokenBucket(tmp_path, "venue", 10, 5, t.monotonic, t.sleep)
    for _ in range(5):
        bucket.acquire()  # budget fully spent

    def crash(*args: object) -> int:
        raise SystemExit("killed mid-write")

    monkeypatch.setattr(os, "pwrite", crash)  # the module calls os.pwrite
    with pytest.raises(SystemExit):
        bucket.try_acquire()
    monkeypatch.undo()
    other_process = SharedFileTokenBucket(tmp_path, "venue", 10, 5, t.monotonic, t.sleep)
    assert other_process.try_acquire() > 0  # must not see an empty file and grant a burst


def test_busy_lock_never_blocks_and_never_grants(tmp_path: Path) -> None:
    """A live-but-stuck lock holder must not freeze callers (e.g. the asyncio stream loop)."""
    import fcntl

    bucket = SharedFileTokenBucket(tmp_path, "venue", rate_per_s=100, burst=5)
    bucket.acquire()
    fd = os.open(tmp_path / "venue.bucket", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)  # simulate a stuck holder (separate open file description)
    try:
        start = time.monotonic()
        wait = bucket.try_acquire()
        assert time.monotonic() - start < 0.1
        assert wait > 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert bucket.try_acquire() == 0.0
