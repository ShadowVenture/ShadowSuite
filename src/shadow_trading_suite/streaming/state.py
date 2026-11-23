"""Shared real-time market state, consistent snapshots and bounded fan-out to consumers.

All bots read the same `MarketState`: every accepted event bumps a global version, and
`snapshot()` captures an immutable view atomically, so readers at the same version see
identical data. Duplicates (same event_id), out-of-order events (older exchange timestamp
than the current value) and events for streams that are not LIVE are never current.
"""

import asyncio
from collections import Counter, OrderedDict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.time import Clock
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind


class StreamStatus(StrEnum):
    SYNCING = "syncing"  # (re)connected, required state not yet restored
    LIVE = "live"
    STALE = "stale"  # LIVE but no update within its freshness bound (snapshot-time view)
    DOWN = "down"  # connection lost
    UNSUPPORTED = "unsupported"  # venue rejected or does not publish it


@dataclass(frozen=True)
class StreamKey:
    instrument_id: InstrumentId
    kind: StreamKind


class ApplyOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    NOT_LIVE = "not_live"


@dataclass(frozen=True)
class MarketSnapshot:
    version: int
    as_of: datetime
    entries: Mapping[StreamKey, StreamEvent]
    status: Mapping[StreamKey, StreamStatus]

    def require(
        self, key: StreamKey, max_age: timedelta, *, allow_incomplete: bool = False
    ) -> StreamEvent:
        """The current value, or DataQualityError if missing, not live, older than max_age,
        or (unless explicitly allowed) an in-progress candle."""
        status = self.status.get(key, StreamStatus.DOWN)
        if status is not StreamStatus.LIVE:
            raise DataQualityError(f"{key.instrument_id} {key.kind.value}: stream {status.value}")
        event = self.entries.get(key)
        if event is None:
            raise DataQualityError(f"{key.instrument_id} {key.kind.value}: no data")
        if self.as_of - event.received_at > max_age:
            raise DataQualityError(f"{key.instrument_id} {key.kind.value}: stale")
        if event.candle_closed is False and not allow_incomplete:
            raise DataQualityError(f"{key.instrument_id} candle in progress (not closed)")
        return event


class MarketState:
    def __init__(
        self,
        clock: Clock,
        stale_after: Mapping[StreamKind, timedelta],
        dedupe_window: int = 50_000,
    ) -> None:
        self._clock = clock
        self._stale_after = dict(stale_after)
        self._latest: dict[StreamKey, StreamEvent] = {}
        self._status: dict[StreamKey, StreamStatus] = {}
        self._recent: OrderedDict[str, None] = OrderedDict()
        self._window = dedupe_window
        self.version = 0
        self.counters: Counter[str] = Counter()

    def set_status(self, key: StreamKey, status: StreamStatus) -> None:
        self._status[key] = status
        self.version += 1

    def status(self, key: StreamKey) -> StreamStatus:
        return self._status.get(key, StreamStatus.DOWN)

    def mark_venue(self, venue: Venue, status: StreamStatus) -> None:
        for key in list(self._status):
            if (
                key.instrument_id.venue is venue
                and self._status[key] is not StreamStatus.UNSUPPORTED
            ):
                self._status[key] = status
        self.version += 1

    def apply(self, event: StreamEvent) -> ApplyOutcome:
        key = StreamKey(event.instrument_id, event.kind)
        if event.event_id in self._recent:
            self.counters["duplicate"] += 1
            return ApplyOutcome.DUPLICATE
        if self.status(key) is not StreamStatus.LIVE:
            self.counters["not_live"] += 1
            return ApplyOutcome.NOT_LIVE
        current = self._latest.get(key)
        if current is not None and event.exchange_ts < current.exchange_ts:
            self.counters["out_of_order"] += 1
            return ApplyOutcome.OUT_OF_ORDER
        self._recent[event.event_id] = None
        if len(self._recent) > self._window:
            self._recent.popitem(last=False)
        self._latest[key] = event
        self.version += 1
        self.counters["accepted"] += 1
        return ApplyOutcome.ACCEPTED

    def snapshot(self, keys: Iterable[StreamKey] | None = None) -> MarketSnapshot:
        now = self._clock.now()
        wanted = list(keys) if keys is not None else list(self._status | self._latest)
        status: dict[StreamKey, StreamStatus] = {}
        for key in wanted:
            st = self.status(key)
            event = self._latest.get(key)
            bound = self._stale_after.get(key.kind)
            fresh_bound_exceeded = (
                event is not None and bound is not None and now - event.received_at > bound
            )
            if st is StreamStatus.LIVE and fresh_bound_exceeded:
                st = StreamStatus.STALE
            status[key] = st
        entries = {k: self._latest[k] for k in wanted if k in self._latest}
        return MarketSnapshot(
            self.version, now, MappingProxyType(entries), MappingProxyType(status)
        )


# -- fan-out --------------------------------------------------------------------------------


class OverflowPolicy(StrEnum):
    DROP_OLDEST = "drop_oldest"  # keep newest; consumer receives a GapNotice and must resync
    DISCONNECT = "disconnect"  # consumer is cut off; it must re-register and resync


class ConsumerOverflowError(Exception):
    pass


@dataclass(frozen=True)
class GapNotice:
    consumer_id: str
    dropped: int


class Consumer:
    def __init__(
        self,
        consumer_id: str,
        maxsize: int,
        policy: OverflowPolicy,
        interests: frozenset[StreamKey] | None,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.consumer_id = consumer_id
        self._maxsize = maxsize
        self._policy = policy
        self._interests = interests
        self._items: deque[StreamEvent] = deque()
        self._ready = asyncio.Event()
        self._gap = 0
        self.dropped = 0
        self.disconnected = False

    @property
    def pending(self) -> int:
        return len(self._items)

    def offer(self, event: StreamEvent) -> None:
        if (
            self._interests is not None
            and StreamKey(event.instrument_id, event.kind) not in self._interests
        ):
            return
        if self.disconnected:
            self.dropped += 1
            return
        if len(self._items) >= self._maxsize:
            self.dropped += 1
            if self._policy is OverflowPolicy.DISCONNECT:
                self.disconnected = True
                self._items.clear()
                self._ready.set()
                return
            self._items.popleft()
            self._gap += 1
        self._items.append(event)
        self._ready.set()

    def get_nowait(self) -> StreamEvent | GapNotice | None:
        """The next item if one is ready (same order and gap semantics as `get`), else None."""
        if self.disconnected:
            raise ConsumerOverflowError(f"{self.consumer_id}: disconnected after queue overflow")
        if self._gap:
            notice, self._gap = GapNotice(self.consumer_id, self._gap), 0
            return notice
        if self._items:
            event = self._items.popleft()
            if not self._items:
                self._ready.clear()
            return event
        return None

    async def get(self) -> StreamEvent | GapNotice:
        while True:
            if self.disconnected:
                raise ConsumerOverflowError(
                    f"{self.consumer_id}: disconnected after queue overflow"
                )
            if self._gap:
                notice, self._gap = GapNotice(self.consumer_id, self._gap), 0
                return notice
            if self._items:
                event = self._items.popleft()
                if not self._items:
                    self._ready.clear()
                return event
            self._ready.clear()
            await self._ready.wait()


class Fanout:
    """Non-blocking publish to bounded per-consumer queues: a slow bot never stalls others."""

    def __init__(self) -> None:
        self._consumers: dict[str, Consumer] = {}

    def register(
        self,
        consumer_id: str,
        maxsize: int,
        policy: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
        interests: frozenset[StreamKey] | None = None,
    ) -> Consumer:
        if consumer_id in self._consumers:
            raise ValueError(f"consumer {consumer_id} already registered")
        consumer = Consumer(consumer_id, maxsize, policy, interests)
        self._consumers[consumer_id] = consumer
        return consumer

    def publish(self, event: StreamEvent) -> None:
        for consumer in self._consumers.values():
            consumer.offer(event)
