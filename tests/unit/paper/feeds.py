"""Scripted public-market events for M6-C tests (no network): a real MarketState and Fanout
driven by deterministic StreamEvents, plus a fake streaming service with the runtime's
`MarketFeed` surface."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.market import (
    BookLevel,
    FundingRate,
    MarkPrice,
    OrderBookSnapshot,
    Ticker,
    Trade,
)
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind, Subscription
from shadow_trading_suite.streaming.session import SessionHealth
from shadow_trading_suite.streaming.state import (
    Consumer,
    Fanout,
    MarketSnapshot,
    MarketState,
    OverflowPolicy,
    StreamKey,
    StreamStatus,
)

D = Decimal
T0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
BTC_EUR = InstrumentId(venue=Venue.COINBASE, symbol="BTC-EUR")
BTC_USD = InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD")
ETH_EUR = InstrumentId(venue=Venue.COINBASE, symbol="ETH-EUR")
ETH_USD = InstrumentId(venue=Venue.COINBASE, symbol="ETH-USD")


class ScriptedFeed:
    """The runtime's market-feed surface over a real MarketState and Fanout."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.state = MarketState(clock, {})
        self.fanout = Fanout()
        self.subscriptions: list[Subscription] = []
        self.health_by_venue = {v: SessionHealth(connected=True, epoch=1) for v in Venue}
        self._n = 0
        self.epoch = 1

    # -- MarketFeed ----------------------------------------------------------------------------

    def subscribe(
        self,
        consumer_id: str,
        subscriptions: Sequence[Subscription],
        *,
        maxsize: int = 10_000,
        policy: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
    ) -> Consumer:
        self.subscriptions += list(subscriptions)
        for s in subscriptions:
            key = StreamKey(s.instrument_id, s.kind)
            if self.state.status(key) is StreamStatus.DOWN:
                self.state.set_status(key, StreamStatus.DOWN)
        interests = frozenset(StreamKey(s.instrument_id, s.kind) for s in subscriptions)
        return self.fanout.register(consumer_id, maxsize, policy, interests)

    async def run(self, stop: asyncio.Event, max_attempts: int | None = None) -> None:
        await stop.wait()

    def snapshot(self, keys: Any = None) -> MarketSnapshot:
        return self.state.snapshot(keys)

    def health(self) -> dict[Venue, SessionHealth]:
        return self.health_by_venue

    # -- scripting -----------------------------------------------------------------------------

    def live(self, iid: InstrumentId, *kinds: StreamKind) -> None:
        for k in kinds:
            self.state.set_status(StreamKey(iid, k), StreamStatus.LIVE)

    def venue_status(self, venue: Venue, status: StreamStatus) -> None:
        self.state.mark_venue(venue, status)

    def emit(
        self, iid: InstrumentId, kind: StreamKind, payload: Any, *, at: datetime | None = None
    ) -> StreamEvent:
        self._n += 1
        ts = at or self.clock.now()
        event = StreamEvent(
            event_id=f"{iid.venue.value}:{kind.value}:{iid.symbol}:{self.epoch}:{self._n}",
            kind=kind,
            instrument_id=iid,
            exchange_ts=ts,
            received_at=self.clock.now(),
            epoch=self.epoch,
            payload=payload,
        )
        if self.state.apply(event).value == "accepted":
            self.fanout.publish(event)
        return event

    def ticker(
        self, iid: InstrumentId, bid: str, ask: str, *, at: datetime | None = None
    ) -> StreamEvent:
        t = Ticker(instrument_id=iid, ts=at or self.clock.now(), bid=D(bid), ask=D(ask))
        return self.emit(iid, StreamKind.TICKER, t, at=at)

    def book(
        self, iid: InstrumentId, bids: list[tuple[str, str]], asks: list[tuple[str, str]]
    ) -> StreamEvent:
        snap = OrderBookSnapshot(
            instrument_id=iid,
            ts=self.clock.now(),
            bids=tuple(BookLevel(price=D(p), quantity=D(q)) for p, q in bids),
            asks=tuple(BookLevel(price=D(p), quantity=D(q)) for p, q in asks),
        )
        return self.emit(iid, StreamKind.BOOK, snap)

    def trade(
        self, iid: InstrumentId, price: str, qty: str = "0.001", side: Side | None = Side.BUY
    ) -> StreamEvent:
        self._n += 1
        t = Trade(
            instrument_id=iid,
            trade_id=f"t{self._n}",
            ts=self.clock.now(),
            price=D(price),
            quantity=D(qty),
            aggressor=side,
        )
        return self.emit(iid, StreamKind.TRADES, t)

    def mark(self, iid: InstrumentId, price: str) -> StreamEvent:
        return self.emit(
            iid, StreamKind.MARK, MarkPrice(instrument_id=iid, ts=self.clock.now(), mark=D(price))
        )

    def funding(self, iid: InstrumentId, rate: str) -> StreamEvent:
        f = FundingRate(
            instrument_id=iid, ts=self.clock.now(), rate=D(rate), interval=timedelta(hours=1)
        )
        return self.emit(iid, StreamKind.FUNDING, f)

    def fx(
        self,
        btc_eur: str = "70520",
        btc_usd: str = "82000",
        eth_eur: str = "2580",
        eth_usd: str = "3000",
    ) -> None:
        """Coinbase FX legs (mid = the given value, spread 2 units each side)."""
        for iid, mid in (
            (BTC_EUR, btc_eur),
            (BTC_USD, btc_usd),
            (ETH_EUR, eth_eur),
            (ETH_USD, eth_usd),
        ):
            self.live(iid, StreamKind.TICKER)
            m = D(mid)
            self.ticker(iid, str(m - 1), str(m + 1))
