"""Client-side rate limiting.

`TokenBucket` protects one process. `SharedFileTokenBucket` keeps one budget across every
process and container on the same host: its state lives in a file on a shared volume and is
read-modified-written under an exclusive `flock`. Timestamps use CLOCK_MONOTONIC, which is
system-wide on Linux and therefore shared by all containers on one kernel.

Limits: correct across processes/containers on ONE host (shared kernel + local volume). It is
not a distributed lock (no NFS / multi-host); the deployment rule is that only the collector
container talks to the exchanges (docs/architecture.md).
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from shadow_trading_suite.core.fs import private_dirs

_KEY = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
# Float refill can leave 0.9999999999995 tokens; the resulting ~1e-14 s wait cannot advance a
# monotonic clock value of ~1e3 s, so without a tolerance acquire() would spin forever.
_EPSILON = 1e-9
# State is ONE fixed-size record, rewritten in place with a single pwrite and protected by a
# checksum. The file is never truncated, so a crash mid-update can only leave a torn record
# (checksum mismatch -> fail closed), never an empty file (which would mean a fresh burst).
_RECORD_BYTES = 160
# If another process holds the lock, do not block (a stuck holder must not freeze callers such
# as the asyncio stream loop): report a short wait and take nothing.
_LOCK_BUSY_WAIT_S = 0.01


class RateLimiter(Protocol):
    def try_acquire(self) -> float:
        """Take a token and return 0.0, or return the seconds to wait (nothing taken)."""
        ...

    def acquire(self) -> None:
        """Block until a token is taken."""
        ...


def _validate(rate_per_s: float, burst: int) -> None:
    if rate_per_s <= 0 or burst < 1:
        raise ValueError("rate_per_s must be > 0 and burst >= 1")


class TokenBucket:
    def __init__(
        self,
        rate_per_s: float,
        burst: int,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        _validate(rate_per_s, burst)
        self._rate = rate_per_s
        self._burst = float(burst)
        self._tokens = float(burst)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last = monotonic()

    def try_acquire(self) -> float:
        now = self._monotonic()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1 - _EPSILON:
            self._tokens -= 1
            return 0.0
        return (1 - self._tokens) / self._rate

    def acquire(self) -> None:
        while (wait := self.try_acquire()) > 0:
            self._sleep(wait)


class SharedFileTokenBucket:
    """Token bucket whose state is shared through a locked file (cross-process, one host)."""

    def __init__(
        self,
        directory: Path,
        key: str,
        rate_per_s: float,
        burst: int,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        _validate(rate_per_s, burst)
        if not _KEY.fullmatch(key):
            raise ValueError(f"invalid rate-limit key {key!r}")
        private_dirs(directory)
        os.chmod(directory, 0o700)
        self._path = directory / f"{key}.bucket"
        self._rate = rate_per_s
        self._burst = float(burst)
        self._monotonic = monotonic
        self._sleep = sleep

    def _read(self, fd: int, now: float) -> tuple[float, float]:
        raw = os.pread(fd, _RECORD_BYTES, 0)
        if not raw:
            return self._burst, now  # genuinely first use: full bucket
        try:
            body, digest = raw.decode().rstrip().rsplit("|", 1)
            if hashlib.sha256(body.encode()).hexdigest()[:16] != digest:
                raise ValueError("checksum mismatch")
            state = json.loads(body)
            tokens, last = float(state["tokens"]), float(state["last"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            return 0.0, now  # corrupt or torn state: fail closed (empty bucket)
        if last > now:
            return 0.0, now  # clock went backwards (e.g. host reboot): fail closed
        return min(self._burst, tokens + (now - last) * self._rate), now

    def try_acquire(self) -> float:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return _LOCK_BUSY_WAIT_S
            now = self._monotonic()
            tokens, last = self._read(fd, now)
            wait = 0.0
            if tokens >= 1 - _EPSILON:
                tokens -= 1
            else:
                wait = (1 - tokens) / self._rate
            body = json.dumps({"tokens": tokens, "last": last})
            record = f"{body}|{hashlib.sha256(body.encode()).hexdigest()[:16]}"
            if len(record) > _RECORD_BYTES:
                raise ValueError("rate-limit record exceeds its fixed size")
            os.pwrite(fd, record.ljust(_RECORD_BYTES).encode(), 0)
            return wait
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def acquire(self) -> None:
        while (wait := self.try_acquire()) > 0:
            self._sleep(wait)
