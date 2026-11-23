"""Shared state, consistent snapshots, dedupe/ordering and bounded fan-out."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.market import Ticker
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind
from shadow_trading_suite.streaming.state import (
    ApplyOutcome,
    ConsumerOverflowError,
    Fanout,
    GapNotice,
    MarketState,
    OverflowPolicy,
    StreamKey,
    StreamStatus,
)

T0 = datetime(2026, 10, 19, 14, tzinfo=UTC)
IID = InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD")
KEY = StreamKey(IID, StreamKind.TICKER)


def ticker(n: int, ts: datetime, price: str = "100") -> StreamEvent:
    return StreamEvent(
        event_id=f"coinbase:ticker:BTC-USD:1:{n}",
        kind=StreamKind.TICKER,
        instrument_id=IID,
        exchange_ts=ts,
        received_at=ts,
        epoch=1,
        sequence=n,
        payload=Ticker(instrument_id=IID, ts=ts, last=Decimal(price)),
    )


def live_state(clock: ManualClock) -> MarketState:
    state = MarketState(clock, stale_after={StreamKind.TICKER: timedelta(seconds=5)})
    state.set_status(KEY, StreamStatus.LIVE)
    return state


class TestMarketState:
    def test_accepts_and_versions(self) -> None:
        state = live_state(ManualClock(T0))
        before = state.version  # status changes are snapshot-visible and bump the version too
        assert state.apply(ticker(1, T0)) is ApplyOutcome.ACCEPTED
        assert state.apply(ticker(2, T0 + timedelta(seconds=1))) is ApplyOutcome.ACCEPTED
        assert state.version == before + 2

    def test_duplicate_event_dropped(self) -> None:
        state = live_state(ManualClock(T0))
        state.apply(ticker(1, T0))
        assert state.apply(ticker(1, T0)) is ApplyOutcome.DUPLICATE
        assert state.counters["duplicate"] == 1

    def test_out_of_order_event_not_treated_as_current(self) -> None:
        state = live_state(ManualClock(T0))
        state.apply(ticker(2, T0 + timedelta(seconds=2), "101"))
        assert state.apply(ticker(1, T0, "99")) is ApplyOutcome.OUT_OF_ORDER
        latest = state.snapshot([KEY]).entries[KEY].payload
        assert isinstance(latest, Ticker)
        assert latest.last == Decimal(101)

    def test_events_for_non_live_streams_rejected(self) -> None:
        state = MarketState(ManualClock(T0), stale_after={})
        state.set_status(KEY, StreamStatus.SYNCING)
        assert state.apply(ticker(1, T0)) is ApplyOutcome.NOT_LIVE
        assert KEY not in state.snapshot([KEY]).entries

    def test_reconnect_invalidates_until_restored(self) -> None:
        state = live_state(ManualClock(T0))
        state.apply(ticker(1, T0))
        state.set_status(KEY, StreamStatus.DOWN)
        snap = state.snapshot([KEY])
        assert snap.status[KEY] is StreamStatus.DOWN
        with pytest.raises(DataQualityError, match="down"):
            snap.require(KEY, max_age=timedelta(minutes=1))


class TestSnapshots:
    def test_snapshot_is_immutable_and_consistent_for_all_readers(self) -> None:
        clock = ManualClock(T0)
        state = live_state(clock)
        state.apply(ticker(1, T0))
        bot_a, bot_b = state.snapshot([KEY]), state.snapshot([KEY])
        state.apply(ticker(2, T0 + timedelta(seconds=1), "105"))
        assert bot_a.version == bot_b.version
        assert bot_a.entries == bot_b.entries  # captured view unaffected by the later update
        assert bot_a.entries[KEY].sequence == 1
        assert state.snapshot([KEY]).version == bot_a.version + 1

    def test_stale_entry_flagged_and_require_fails_closed(self) -> None:
        clock = ManualClock(T0)
        state = live_state(clock)
        state.apply(ticker(1, T0))
        clock.advance(timedelta(seconds=6))
        snap = state.snapshot([KEY])
        assert snap.status[KEY] is StreamStatus.STALE
        with pytest.raises(DataQualityError, match="stale"):
            snap.require(KEY, max_age=timedelta(seconds=5))

    def test_require_missing_fails_closed(self) -> None:
        state = live_state(ManualClock(T0))
        with pytest.raises(DataQualityError, match="no data"):
            state.snapshot([KEY]).require(KEY, max_age=timedelta(seconds=5))

    def test_require_fresh_live_value(self) -> None:
        state = live_state(ManualClock(T0))
        state.apply(ticker(1, T0))
        assert (
            state.snapshot([KEY]).require(KEY, max_age=timedelta(seconds=5)).event_id.endswith(":1")
        )


class TestFanout:
    def test_every_consumer_receives_the_same_events(self) -> None:
        fanout = Fanout()
        bots = [fanout.register(b, maxsize=10) for b in ("bot-01", "bot-02", "cdc-01", "cdc-02")]
        events = [ticker(i, T0) for i in range(3)]
        for e in events:
            fanout.publish(e)

        async def drain() -> list[list[object]]:
            return [[await c.get() for _ in range(3)] for c in bots]

        received = asyncio.run(drain())
        assert all(r == events for r in received)

    def test_slow_consumer_never_blocks_others_and_gets_a_gap_notice(self) -> None:
        fanout = Fanout()
        slow = fanout.register("bot-01", maxsize=2, policy=OverflowPolicy.DROP_OLDEST)
        fast = fanout.register("bot-02", maxsize=100)
        for i in range(5):
            fanout.publish(ticker(i, T0))
        assert slow.dropped == 3

        async def read() -> tuple[object, object, object]:
            return await slow.get(), await slow.get(), await fast.get()

        notice, newest_kept, first_fast = asyncio.run(read())
        assert isinstance(notice, GapNotice)
        assert notice.dropped == 3
        assert isinstance(newest_kept, StreamEvent)
        assert newest_kept.sequence == 3
        assert isinstance(first_fast, StreamEvent)
        assert first_fast.sequence == 0

    def test_get_nowait_has_the_same_order_and_gap_semantics(self) -> None:
        fanout = Fanout()
        c = fanout.register("paper", maxsize=2, policy=OverflowPolicy.DROP_OLDEST)
        assert c.get_nowait() is None
        for i in range(4):
            fanout.publish(ticker(i, T0))
        notice = c.get_nowait()
        assert isinstance(notice, GapNotice)
        assert notice.dropped == 2
        got = [c.get_nowait(), c.get_nowait(), c.get_nowait()]
        assert [e.sequence for e in got[:2] if isinstance(e, StreamEvent)] == [2, 3]
        assert got[2] is None
        strict = fanout.register("strict", maxsize=1, policy=OverflowPolicy.DISCONNECT)
        fanout.publish(ticker(5, T0))
        fanout.publish(ticker(6, T0))
        with pytest.raises(ConsumerOverflowError):
            strict.get_nowait()

    def test_disconnect_policy(self) -> None:
        fanout = Fanout()
        strict = fanout.register("cdc-01", maxsize=1, policy=OverflowPolicy.DISCONNECT)
        fanout.publish(ticker(0, T0))
        fanout.publish(ticker(1, T0))
        with pytest.raises(ConsumerOverflowError):
            asyncio.run(strict.get())
        fanout.publish(ticker(2, T0))  # publishing to a disconnected consumer is a no-op
        assert strict.dropped >= 1

    def test_interest_filter(self) -> None:
        fanout = Fanout()
        other = StreamKey(InstrumentId(venue=Venue.COINBASE, symbol="ETH-USD"), StreamKind.TICKER)
        c = fanout.register("bot-01", maxsize=5, interests=frozenset({other}))
        fanout.publish(ticker(0, T0))
        assert c.pending == 0

    def test_duplicate_consumer_id_rejected(self) -> None:
        fanout = Fanout()
        fanout.register("bot-01", maxsize=1)
        with pytest.raises(ValueError, match="bot-01"):
            fanout.register("bot-01", maxsize=1)


class TestNothingIncompleteIsTradable:
    @pytest.mark.parametrize(
        "status",
        [StreamStatus.SYNCING, StreamStatus.DOWN, StreamStatus.UNSUPPORTED, StreamStatus.STALE],
    )
    def test_every_non_live_status_fails_closed(self, status: StreamStatus) -> None:
        state = live_state(ManualClock(T0))
        state.apply(ticker(1, T0))
        state.set_status(KEY, status)
        with pytest.raises(DataQualityError):
            state.snapshot([KEY]).require(KEY, max_age=timedelta(minutes=5))

    def test_in_progress_candle_is_not_tradable_by_default(self) -> None:
        from shadow_trading_suite.core.market import Candle

        clock = ManualClock(T0)
        state = MarketState(clock, stale_after={})
        key = StreamKey(IID, StreamKind.CANDLES)
        state.set_status(key, StreamStatus.LIVE)
        c = Decimal(100)
        candle = Candle(
            instrument_id=IID,
            interval=timedelta(minutes=5),
            open_time=T0,
            open=c,
            high=c,
            low=c,
            close=c,
            volume=Decimal(1),
        )
        state.apply(
            StreamEvent(
                event_id="x",
                kind=StreamKind.CANDLES,
                instrument_id=IID,
                exchange_ts=T0,
                received_at=T0,
                epoch=1,
                payload=candle,
                candle_closed=False,
            )
        )
        snap = state.snapshot([key])
        with pytest.raises(DataQualityError, match="in progress"):
            snap.require(key, max_age=timedelta(minutes=5))
        assert snap.require(key, max_age=timedelta(minutes=5), allow_incomplete=True)

    def test_snapshot_cannot_be_mutated_by_a_consumer(self) -> None:
        state = live_state(ManualClock(T0))
        state.apply(ticker(1, T0))
        snap = state.snapshot([KEY])
        with pytest.raises(TypeError):
            snap.entries[KEY] = ticker(2, T0)  # type: ignore[index]
