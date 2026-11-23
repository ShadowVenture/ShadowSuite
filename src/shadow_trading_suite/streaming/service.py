"""The central real-time market-data service shared by all bots.

Exactly one StreamSession — one WebSocket connection — per venue, regardless of how many bots
subscribe. Subscriptions are de-duplicated across bots; every bot gets its own bounded
consumer filtered to its declared streams, and all bots read the same MarketState snapshots.
This service provides data only: it never decides leverage or executes anything.
"""

import asyncio
import random
from collections.abc import Iterable, Sequence
from datetime import timedelta
from pathlib import Path

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.core.time import Clock
from shadow_trading_suite.exchanges.ratelimit import RateLimiter, SharedFileTokenBucket, TokenBucket
from shadow_trading_suite.streaming.coinbase import CoinbaseStreamProtocol
from shadow_trading_suite.streaming.cryptocom import CryptoComStreamProtocol
from shadow_trading_suite.streaming.events import StreamKind, Subscription
from shadow_trading_suite.streaming.session import SessionHealth, StreamSession
from shadow_trading_suite.streaming.state import (
    Consumer,
    Fanout,
    MarketSnapshot,
    MarketState,
    OverflowPolicy,
    StreamKey,
)
from shadow_trading_suite.streaming.transport import WebsocketsConnector

# Book levels published to consumers per venue (C13/R3): 50 levels VERIFIED on both venues by
# the 2026-10-19 probes (Coinbase level2 full depth -> top 50; Crypto.com book.{sym}.50).
PUBLISH_DEPTH = 50

DEFAULT_STALE_AFTER = {
    StreamKind.TICKER: 30,
    StreamKind.BOOK: 30,
    StreamKind.TRADES: 300,  # trades are event-driven; quiet markets are legitimate
    StreamKind.CANDLES: 600,
    StreamKind.MARK: 30,
    StreamKind.INDEX: 30,
    StreamKind.FUNDING: 7200,
    StreamKind.ESTIMATED_FUNDING: 300,
}


class RealtimeMarketDataService:
    def __init__(
        self, sessions: Sequence[StreamSession], state: MarketState, fanout: Fanout
    ) -> None:
        self._sessions: dict[Venue, StreamSession] = {}
        for session in sessions:
            if session.venue in self._sessions:
                raise ValueError(f"duplicate stream session for venue {session.venue.value}")
            self._sessions[session.venue] = session
        self._state = state
        self._fanout = fanout
        self._started = False

    @classmethod
    def create(
        cls, clock: Clock, rate_limit_dir: Path | None = None
    ) -> "RealtimeMarketDataService":
        """Production wiring. With `rate_limit_dir` (a shared volume) the per-IP message
        budget is coordinated across containers; otherwise it is process-local only."""
        state = MarketState(
            clock, {k: timedelta(seconds=v) for k, v in DEFAULT_STALE_AFTER.items()}
        )
        fanout = Fanout()
        sessions = []
        protocols = (CoinbaseStreamProtocol(), CryptoComStreamProtocol(book_depth=PUBLISH_DEPTH))
        for protocol in protocols:
            rate = protocol.messages_per_second * 0.5  # stay well inside the venue limit
            limiter: RateLimiter = (
                SharedFileTokenBucket(rate_limit_dir, f"{protocol.venue.value}-ws", rate, 2)
                if rate_limit_dir is not None
                else TokenBucket(rate, 2)
            )
            sessions.append(
                StreamSession(
                    protocol,
                    WebsocketsConnector(),
                    state,
                    fanout,
                    clock,
                    limiter=limiter,
                    rng=random.Random(),  # noqa: S311 - reconnect jitter, not cryptography
                    book_depth=PUBLISH_DEPTH,
                )
            )
        return cls(sessions, state, fanout)

    def subscribe(
        self,
        bot_id: str,
        subscriptions: Sequence[Subscription],
        *,
        maxsize: int = 10_000,
        policy: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
    ) -> Consumer:
        if self._started:
            raise RuntimeError("subscriptions must be declared before the service starts")
        for sub in subscriptions:
            if sub.instrument_id.venue not in self._sessions:
                raise ValueError(f"no stream session for venue {sub.instrument_id.venue.value}")
        for venue, session in self._sessions.items():
            session.add([s for s in subscriptions if s.instrument_id.venue is venue])
        interests = frozenset(StreamKey(s.instrument_id, s.kind) for s in subscriptions)
        return self._fanout.register(bot_id, maxsize, policy, interests)

    async def run(self, stop: asyncio.Event, max_attempts: int | None = None) -> None:
        self._started = True
        await asyncio.gather(*(s.run(stop, max_attempts) for s in self._sessions.values()))

    def snapshot(self, keys: Iterable[StreamKey] | None = None) -> MarketSnapshot:
        return self._state.snapshot(keys)

    def health(self) -> dict[Venue, SessionHealth]:
        return {venue: s.health for venue, s in self._sessions.items()}
