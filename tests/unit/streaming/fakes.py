"""Deterministic fakes for streaming tests: scripted connections and a shared fake timeline."""

import asyncio
import json
import math
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.streaming.transport import (
    GuardedConnection,
    WsClosedError,
    WsConnection,
    WsEndpoint,
)
from tests.unit.exchanges.fakes import fixture_json

T0 = datetime(2026, 10, 19, 14, 43, 52, tzinfo=UTC)


class Timeline:
    """One fake time source for datetimes, monotonic seconds and async sleeps."""

    def __init__(self, start: datetime = T0) -> None:
        self.clock = ManualClock(start)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.clock.now().timestamp()

    def sync_sleep(self, seconds: float) -> None:
        # Like a real sleep, always make progress: round UP to the clock's 1 µs resolution.
        self.sleeps.append(seconds)
        self.clock.advance(timedelta(microseconds=max(1, math.ceil(seconds * 1_000_000))))

    async def sleep(self, seconds: float) -> None:
        self.sync_sleep(seconds)


SILENCE = "SILENCE"
HANG = "HANG"


class _Raw:
    def __init__(self, script: Iterable[Any], timeline: Timeline) -> None:
        self.items: deque[Any] = deque(script)
        self.sent: list[str] = []
        self.timeline = timeline
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        while self.items:
            item = self.items.popleft()
            if isinstance(item, tuple) and item[0] == "advance":
                self.timeline.clock.advance(timedelta(seconds=item[1]))
                continue
            if item == SILENCE:
                raise TimeoutError("no frame within the silence bound")
            if item == HANG:
                await asyncio.Event().wait()  # a live connection that never sends anything
            if isinstance(item, BaseException):
                raise item
            return str(item)
        raise WsClosedError("server closed the connection")

    async def close(self) -> None:
        self.closed = True


class ScriptedConnector:
    """Each connect() consumes the next script; an exception entry means connect fails."""

    def __init__(self, scripts: Iterable[Any], timeline: Timeline) -> None:
        self.scripts: deque[Any] = deque(scripts)
        self.timeline = timeline
        self.connections: list[_Raw] = []
        self.attempts = 0

    async def connect(self, endpoint: WsEndpoint) -> WsConnection:
        self.attempts += 1
        if not self.scripts:
            raise WsClosedError("no more scripted connections")
        script = self.scripts.popleft()
        if isinstance(script, BaseException):
            raise script
        raw = _Raw(script, self.timeline)
        self.connections.append(raw)
        return GuardedConnection(raw, endpoint)  # real outbound policy enforcement


def ws(rel: str, **overrides: Any) -> str:
    data = fixture_json(f"ws/{rel}")
    data.update(overrides)
    return json.dumps(data)


def cc_result(rel: str, mutate: Any = None) -> str:
    data = fixture_json(f"ws/cryptocom/{rel}")
    if mutate is not None:
        mutate(data)
    return json.dumps(data)
