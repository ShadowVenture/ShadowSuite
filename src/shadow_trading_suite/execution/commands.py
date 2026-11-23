"""FIFO command queue in front of the single-writer engine (E1, §5.1).

Bots and market events enqueue commands from any thread; exactly one writer drains them in
arrival order, so concurrent requests are totally ordered and each one sees every previously
committed command (no double-spend of capital or liquidity).
"""

import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any


class CommandQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: deque[tuple[int, Callable[[], Any], Future[Any]]] = deque()
        self._next = 0
        self._drain = threading.Lock()

    def submit(self, command: Callable[[], Any]) -> Future[Any]:
        future: Future[Any] = Future()
        with self._lock:
            self._next += 1
            self._items.append((self._next, command, future))
        return future

    def pending(self) -> int:
        with self._lock:
            return len(self._items)

    def drain(self) -> int:
        """Run every queued command in FIFO order on the calling (writer) thread."""
        done = 0
        with self._drain:
            while True:
                with self._lock:
                    if not self._items:
                        return done
                    _, command, future = self._items.popleft()
                try:
                    future.set_result(command())
                except Exception as exc:
                    future.set_exception(exc)
                done += 1
