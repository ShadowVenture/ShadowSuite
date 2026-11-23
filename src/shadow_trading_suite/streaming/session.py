"""One reconnecting public WebSocket session per venue, shared by every bot.

Lifecycle per connection (epoch):
  connect → all streams DOWN→SYNCING → venue connect delay → rate-limited subscribe →
  read loop. A stream turns LIVE only once its required state is restored: a fresh book
  snapshot for books, the first new message for every other stream. Any connection-level
  problem (close, silence, malformed frame, venue error, Coinbase sequence gap) marks every
  stream DOWN and reconnects with capped exponential backoff and jitter. A Crypto.com book
  sequence gap resubscribes only that book. Safety violations are never retried.
"""

import asyncio
import random
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from shadow_trading_suite.core.errors import SafetyViolationError
from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.core.time import Clock
from shadow_trading_suite.exchanges.errors import ExchangeError
from shadow_trading_suite.exchanges.ratelimit import RateLimiter
from shadow_trading_suite.streaming.events import (
    Ack,
    BookDeltaMsg,
    BookSnapshotMsg,
    ConnectionGap,
    Heartbeat,
    HeartbeatReply,
    ParseResult,
    StreamEvent,
    StreamKind,
    Subscription,
)
from shadow_trading_suite.streaming.orderbook import BookSyncError, LocalOrderBook
from shadow_trading_suite.streaming.state import (
    ApplyOutcome,
    Fanout,
    MarketState,
    StreamKey,
    StreamStatus,
)
from shadow_trading_suite.streaming.transport import WsConnection, WsConnector, WsEndpoint


class VenueStreamProtocol(Protocol):
    venue: Venue
    endpoint: WsEndpoint
    supported: frozenset[StreamKind]
    connect_delay_s: float
    messages_per_second: float
    max_silence: timedelta

    def reset(self) -> None: ...
    def subscribe_frames(self, subs: Sequence[Subscription]) -> list[str]: ...
    def unsubscribe_frames(self, subs: Sequence[Subscription]) -> list[str]: ...
    def subscription_for_channel(self, channel: str) -> Subscription | None: ...
    def parse(self, text: str, received_at: datetime, epoch: int) -> ParseResult: ...


@dataclass(frozen=True)
class ReconnectPolicy:
    base_s: float = 1.0
    max_s: float = 60.0
    jitter_low: float = 0.5  # delay drawn uniformly from [jitter_low, 1] x capped backoff

    def delay(self, failures: int, rng: random.Random) -> float:
        # the exponent is bounded: 2**1024 overflows a float, which ended a session for good
        # after ~1025 consecutive failures (v1.024 R1b fix); 2**62 is far beyond any cap
        capped = min(self.max_s, self.base_s * float(2 ** min(62, max(0, failures - 1))))
        return capped * rng.uniform(self.jitter_low, 1.0)


@dataclass
class SessionHealth:
    connected: bool = False
    epoch: int = 0
    connects: int = 0
    connect_failures: int = 0
    disconnects: int = 0
    gaps: int = 0
    resyncs: int = 0
    last_message_at: datetime | None = None
    last_error: str | None = None
    recent_errors: deque[str] = field(default_factory=lambda: deque(maxlen=20))

    def record(self, error: str) -> None:
        self.last_error = error
        self.recent_errors.append(error)


class _ReconnectRequiredError(Exception):
    pass


class StreamSession:
    def __init__(
        self,
        protocol: VenueStreamProtocol,
        connector: WsConnector,
        state: MarketState,
        fanout: Fanout,
        clock: Clock,
        *,
        limiter: RateLimiter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        reconnect: ReconnectPolicy | None = None,
        book_depth: int = 10,
    ) -> None:
        self._protocol = protocol
        self._connector = connector
        self._state = state
        self._fanout = fanout
        self._clock = clock
        self._limiter = limiter
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not cryptography
        self._reconnect = reconnect or ReconnectPolicy()
        self._depth = book_depth
        self._subs: dict[StreamKey, Subscription] = {}
        self._books: dict[StreamKey, LocalOrderBook] = {}
        self._book_counter = 0
        self.health = SessionHealth()

    @property
    def venue(self) -> Venue:
        return self._protocol.venue

    def add(self, subs: Sequence[Subscription]) -> None:
        for sub in subs:
            key = StreamKey(sub.instrument_id, sub.kind)
            if sub.instrument_id.venue is not self.venue:
                raise ValueError(f"{key} does not belong to venue {self.venue.value}")
            if sub.kind not in self._protocol.supported:
                self._state.set_status(key, StreamStatus.UNSUPPORTED)
                continue
            self._subs[key] = sub
            self._state.set_status(key, StreamStatus.DOWN)

    @property
    def subscriptions(self) -> list[Subscription]:
        return list(self._subs.values())

    # -- lifecycle -----------------------------------------------------------------------

    async def run(self, stop: asyncio.Event, max_attempts: int | None = None) -> None:
        attempts = failures = 0
        while not stop.is_set() and (max_attempts is None or attempts < max_attempts):
            attempts += 1
            try:
                conn = await self._connector.connect(self._protocol.endpoint)
            except SafetyViolationError:
                raise
            except ExchangeError as exc:
                self.health.connect_failures += 1
                self.health.record(f"connect: {exc}")
                failures += 1
            else:
                delivered = await self._serve(conn, stop)
                failures = 1 if delivered else failures + 1
            if stop.is_set() or (max_attempts is not None and attempts >= max_attempts):
                break
            await self._sleep(self._reconnect.delay(failures, self._rng))

    async def _serve(self, conn: WsConnection, stop: asyncio.Event) -> bool:
        """Run one connection; return True if it delivered valid data."""
        self.health.connects += 1
        self.health.epoch += 1
        self.health.connected = True
        self._protocol.reset()
        self._books = {
            k: LocalOrderBook(k.instrument_id) for k in self._subs if k.kind is StreamKind.BOOK
        }
        for key in self._subs:
            self._state.set_status(key, StreamStatus.SYNCING)
        delivered = False
        try:
            await self._sleep(self._protocol.connect_delay_s)
            if self._subs:
                for frame in self._protocol.subscribe_frames(list(self._subs.values())):
                    await self._send(conn, frame)
            while not stop.is_set():
                text = await self._next_frame(conn, stop)
                if text is None:
                    break  # stop requested
                now = self._clock.now()
                self.health.last_message_at = now
                try:
                    result = self._protocol.parse(text, now, self.health.epoch)
                except ValueError as exc:  # includes ResponseFormatError and JSON errors
                    raise _ReconnectRequiredError(f"malformed frame: {exc}") from exc
                delivered |= await self._handle(conn, result, now)
        except SafetyViolationError:
            raise
        except (_ReconnectRequiredError, ExchangeError) as exc:
            self.health.record(str(exc))
        finally:
            self.health.connected = False
            self.health.disconnects += 1
            self._state.mark_venue(self.venue, StreamStatus.DOWN)
            await conn.close()
        return delivered

    async def _next_frame(self, conn: WsConnection, stop: asyncio.Event) -> str | None:
        """Next frame, None if stop was requested first; silence beyond the bound reconnects."""
        receive = asyncio.ensure_future(conn.recv())
        stopping = asyncio.ensure_future(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {receive, stopping},
                timeout=self._protocol.max_silence.total_seconds(),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stopping.cancel()
        if receive in done:
            return receive.result()  # re-raises connection errors
        receive.cancel()
        if stopping in done:
            return None
        raise _ReconnectRequiredError(f"no frame within {self._protocol.max_silence} (silent)")

    async def _send(self, conn: WsConnection, frame: str) -> None:
        while (wait := self._limiter.try_acquire()) > 0:
            await self._sleep(wait)
        await conn.send(frame)  # GuardedConnection enforces the outbound policy

    # -- message handling ------------------------------------------------------------------

    async def _handle(self, conn: WsConnection, result: ParseResult, now: datetime) -> bool:
        delivered = False
        for control in result.controls:
            if isinstance(control, HeartbeatReply):
                await self._send(conn, control.text)
            elif isinstance(control, Heartbeat):
                pass
            elif isinstance(control, ConnectionGap):
                self.health.gaps += 1
                raise _ReconnectRequiredError(control.detail)
            elif isinstance(control, Ack) and not control.ok:
                sub = self._protocol.subscription_for_channel(control.channel)
                if sub is not None:
                    key = StreamKey(sub.instrument_id, sub.kind)
                    self._subs.pop(key, None)
                    self._state.set_status(key, StreamStatus.UNSUPPORTED)
            elif isinstance(control, BookSnapshotMsg | BookDeltaMsg):
                delivered |= await self._book(conn, control, now)
        for event in sorted(result.events, key=lambda e: e.exchange_ts):
            key = StreamKey(event.instrument_id, event.kind)
            if key not in self._subs:
                continue
            if self._state.status(key) is StreamStatus.SYNCING:
                self._state.set_status(key, StreamStatus.LIVE)  # first message restores state
            delivered |= self._publish(event)
        return delivered

    def _publish(self, event: StreamEvent) -> bool:
        if self._state.apply(event) is ApplyOutcome.ACCEPTED:
            self._fanout.publish(event)
            return True
        return False

    async def _book(
        self, conn: WsConnection, msg: BookSnapshotMsg | BookDeltaMsg, now: datetime
    ) -> bool:
        key = StreamKey(msg.subscription.instrument_id, StreamKind.BOOK)
        book = self._books.get(key)
        if book is None:
            return False
        try:
            if isinstance(msg, BookSnapshotMsg):
                book.apply_snapshot(msg.bids, msg.asks, msg.exchange_ts, msg.sequence)
                self._state.set_status(key, StreamStatus.LIVE)
            elif not book.synced:
                self._state.counters["book_delta_before_snapshot"] += 1
                return False
            else:
                book.apply_delta(msg.changes, msg.exchange_ts, msg.sequence, msg.previous)
        except BookSyncError as exc:
            await self._resync_book(conn, key, str(exc))
            return False
        self._book_counter += 1
        event = StreamEvent(
            event_id=f"{self.venue.value}:book:{key.instrument_id.symbol}:{self.health.epoch}:{self._book_counter}",
            kind=StreamKind.BOOK,
            instrument_id=key.instrument_id,
            exchange_ts=msg.exchange_ts,
            received_at=now,
            epoch=self.health.epoch,
            sequence=msg.sequence,
            payload=book.top(self._depth),
        )
        return self._publish(event)

    async def _resync_book(self, conn: WsConnection, key: StreamKey, reason: str) -> None:
        self.health.resyncs += 1
        self._state.counters["book_resync"] += 1
        self._state.set_status(key, StreamStatus.SYNCING)
        self._books[key] = LocalOrderBook(key.instrument_id)
        if key.instrument_id.venue is Venue.COINBASE:
            # Coinbase books have no per-book sequence: rebuild via a fresh connection.
            raise _ReconnectRequiredError(f"book resync required: {reason}")
        sub = self._subs[key]
        for frame in self._protocol.unsubscribe_frames([sub]) + self._protocol.subscribe_frames(
            [sub]
        ):
            await self._send(conn, frame)
