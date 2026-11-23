"""StreamSession reliability: reconnect/backoff, heartbeats, gaps, resync, rate limits."""

import asyncio
import json
import random
from typing import Any

import pytest

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.exchanges.errors import TransportPolicyError
from shadow_trading_suite.exchanges.ratelimit import TokenBucket
from shadow_trading_suite.streaming.coinbase import CoinbaseStreamProtocol
from shadow_trading_suite.streaming.cryptocom import CryptoComStreamProtocol
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind, Subscription
from shadow_trading_suite.streaming.session import ReconnectPolicy, StreamSession
from shadow_trading_suite.streaming.state import Fanout, MarketState, StreamKey, StreamStatus
from shadow_trading_suite.streaming.transport import WsClosedError
from tests.unit.streaming.fakes import SILENCE, ScriptedConnector, Timeline, cc_result, ws

CC_BTC = InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTC_USD")
CB_BTC = InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD")
BOOK = StreamKey(CC_BTC, StreamKind.BOOK)
CC_TICKER = StreamKey(CC_BTC, StreamKind.TICKER)


def build(
    protocol: Any, scripts: list[Any], subs: list[Subscription], **kw: Any
) -> tuple[StreamSession, ScriptedConnector, MarketState, Fanout, Timeline]:
    tl = Timeline()
    connector = ScriptedConnector(scripts, tl)
    state = MarketState(tl.clock, stale_after={})
    fanout = Fanout()
    session = StreamSession(
        protocol,
        connector,
        state,
        fanout,
        tl.clock,
        limiter=TokenBucket(
            protocol.messages_per_second, 2, monotonic=tl.monotonic, sleep=tl.sync_sleep
        ),
        sleep=tl.sleep,
        rng=random.Random(7),  # noqa: S311 - deterministic test jitter
        **kw,
    )
    session.add(subs)
    return session, connector, state, fanout, tl


def run(session: StreamSession, attempts: int) -> None:
    asyncio.run(session.run(asyncio.Event(), max_attempts=attempts))


def sent(connector: ScriptedConnector, index: int) -> list[dict[str, Any]]:
    return [json.loads(s) for s in connector.connections[index].sent]


CC_SUBS = [Subscription(CC_BTC, StreamKind.BOOK), Subscription(CC_BTC, StreamKind.TICKER)]


class TestCryptoComSession:
    def test_waits_after_connect_then_subscribes_once(self) -> None:
        session, connector, *_, tl = build(CryptoComStreamProtocol(), [[]], CC_SUBS)
        run(session, 1)
        assert tl.sleeps[0] == 1.0  # venue-recommended delay before the first request
        [frame] = sent(connector, 0)
        assert frame["method"] == "subscribe"
        assert frame["params"]["channels"] == ["book.BTC_USD.10", "ticker.BTC_USD"]

    def test_book_becomes_live_only_after_snapshot(self) -> None:
        events: list[StreamEvent] = []
        script = [
            cc_result("book_update_1.json"),
            cc_result("book_snapshot.json"),
            cc_result("book_update_1.json"),
            cc_result("book_update_2.json"),
        ]
        session, _, state, fanout, _ = build(CryptoComStreamProtocol(), [script], CC_SUBS)
        consumer = fanout.register("cdc-01", maxsize=100)
        run(session, 1)
        while consumer.pending:
            events.append(asyncio.run(consumer.get()))  # type: ignore[arg-type]
        books = [e for e in events if e.kind is StreamKind.BOOK]
        assert len(books) == 3  # delta before the snapshot was not applied
        assert (
            books[-1].sequence
            == json.loads(cc_result("book_update_2.json"))["result"]["data"][0]["u"]
        )
        assert state.counters["book_delta_before_snapshot"] == 1

    def test_heartbeat_answered(self) -> None:
        session, connector, *_ = build(
            CryptoComStreamProtocol(), [[cc_result("heartbeat.json")]], CC_SUBS
        )
        run(session, 1)
        replies = [f for f in sent(connector, 0) if f["method"] == "public/respond-heartbeat"]
        assert replies == [
            {
                "id": json.loads(cc_result("heartbeat.json"))["id"],
                "method": "public/respond-heartbeat",
            }
        ]

    def test_book_sequence_gap_resubscribes_only_that_book(self) -> None:
        def break_chain(d: dict[str, Any]) -> None:
            d["result"]["data"][0]["pu"] = 1  # does not match the snapshot's u

        script = [
            cc_result("book_snapshot.json"),
            cc_result("book_update_1.json", break_chain),
            cc_result("book_snapshot.json"),
        ]
        session, connector, state, *_ = build(CryptoComStreamProtocol(), [script], CC_SUBS)
        run(session, 1)
        frames = sent(connector, 0)
        assert [f["method"] for f in frames] == ["subscribe", "unsubscribe", "subscribe"]
        assert frames[1]["params"]["channels"] == ["book.BTC_USD.10"]
        assert session.health.resyncs == 1
        assert state.counters["book_resync"] == 1

    def test_rejected_subscription_marked_unsupported(self) -> None:
        settlement = cc_result(
            "ack_error_settlement.json", lambda d: d.update(channel="ticker.BTC_USD")
        )
        session, _, state, *_ = build(CryptoComStreamProtocol(), [[settlement]], CC_SUBS)
        run(session, 1)
        assert state.status(CC_TICKER) is StreamStatus.UNSUPPORTED


class TestReconnect:
    def test_disconnect_marks_down_then_resubscribes_and_requires_fresh_state(self) -> None:
        first = [cc_result("book_snapshot.json"), cc_result("ticker.json")]
        second = [cc_result("book_update_2.json")]  # delta on the NEW connection before snapshot
        session, connector, state, *_ = build(CryptoComStreamProtocol(), [first, second], CC_SUBS)
        run(session, 2)
        assert len(connector.connections) == 2
        assert sent(connector, 1)[0]["method"] == "subscribe"  # subscriptions recovered
        assert state.status(BOOK) is StreamStatus.DOWN  # never LIVE again without a snapshot
        assert session.health.epoch == 2

    def test_exponential_backoff_with_jitter_and_cap(self) -> None:
        outage = [WsClosedError("503")] * 6
        session, connector, *_, tl = build(
            CryptoComStreamProtocol(),
            [*outage, []],
            CC_SUBS,
            reconnect=ReconnectPolicy(base_s=1, max_s=8, jitter_low=0.5),
        )
        run(session, 7)
        backoffs = tl.sleeps[:6]
        assert all(0.5 * min(8, 2**i) <= b <= min(8, 2**i) for i, b in enumerate(backoffs))
        assert len(set(backoffs)) > 1  # jittered
        assert max(backoffs) <= 8
        assert len(connector.connections) == 1
        assert session.health.connect_failures == 6

    def test_silence_forces_reconnect(self) -> None:
        session, connector, *_ = build(CryptoComStreamProtocol(), [[SILENCE], []], CC_SUBS)
        run(session, 2)
        assert len(connector.connections) == 2
        assert any("silen" in e for e in session.health.recent_errors)

    def test_malformed_frame_forces_reconnect(self) -> None:
        session, connector, *_ = build(CryptoComStreamProtocol(), [["{not json"], []], CC_SUBS)
        run(session, 2)
        assert len(connector.connections) == 2


CB_SUBS = [Subscription(CB_BTC, k) for k in (StreamKind.TICKER, StreamKind.BOOK, StreamKind.TRADES)]


class TestCoinbaseSession:
    def test_subscribe_frames_respect_message_rate_limit(self) -> None:
        session, connector, *_, tl = build(CoinbaseStreamProtocol(), [[]], CB_SUBS)
        run(session, 1)
        assert len(sent(connector, 0)) == 4  # heartbeats + 3 channels
        assert sum(tl.sleeps) >= (4 - 2) / 8 - 1e-9  # burst 2, then 8 msg/s

    def test_connection_sequence_gap_triggers_reconnect(self) -> None:
        script = [
            ws("coinbase/heartbeat.json", sequence_num=0),
            ws("coinbase/heartbeat.json", sequence_num=5),
        ]
        session, connector, *_ = build(CoinbaseStreamProtocol(), [script, []], CB_SUBS)
        run(session, 2)
        assert len(connector.connections) == 2
        assert session.health.gaps == 1

    def test_venue_rate_limit_error_reconnects_with_backoff(self) -> None:
        error = json.dumps({"type": "error", "message": "rate limit exceeded"})
        session, connector, *_, tl = build(CoinbaseStreamProtocol(), [[error], []], CB_SUBS)
        run(session, 2)
        assert len(connector.connections) == 2
        assert any(s >= 0.5 for s in tl.sleeps)  # backoff before reconnecting
        assert any("rate limit" in e for e in session.health.recent_errors)

    def test_unsupported_kinds_marked_and_never_subscribed(self) -> None:
        mark = Subscription(CB_BTC, StreamKind.MARK)
        session, connector, state, *_ = build(CoinbaseStreamProtocol(), [[]], [*CB_SUBS, mark])
        run(session, 1)
        assert state.status(StreamKey(CB_BTC, StreamKind.MARK)) is StreamStatus.UNSUPPORTED
        assert all(f["channel"] != "mark" for f in sent(connector, 0))

    def test_duplicate_and_out_of_order_events_not_published(self) -> None:
        newer = ws("coinbase/ticker_update.json", sequence_num=1)
        older = json.loads(ws("coinbase/ticker_update.json", sequence_num=2))
        older["timestamp"] = "2026-10-19T14:00:00Z"
        script = [ws("coinbase/trades_update.json", sequence_num=0), newer, json.dumps(older)]
        session, _, state, fanout, _ = build(CoinbaseStreamProtocol(), [script], CB_SUBS)
        consumer = fanout.register("bot-01", maxsize=100)
        run(session, 1)
        kinds = []
        while consumer.pending:
            e = asyncio.run(consumer.get())
            kinds.append(e.kind)  # type: ignore[union-attr]
        assert kinds.count(StreamKind.TICKER) == 1
        assert state.counters["out_of_order"] == 1


def test_outbound_policy_violation_is_never_retried() -> None:
    class Rogue(CryptoComStreamProtocol):
        def subscribe_frames(self, subs: Any) -> list[str]:
            return [json.dumps({"id": 1, "method": "public/auth", "api_key": "x"})]

    session, connector, *_ = build(Rogue(), [[], []], CC_SUBS)
    with pytest.raises(TransportPolicyError):
        run(session, 2)
    assert len(connector.connections) == 1
    assert connector.connections[0].sent == []


def test_exchange_outage_then_full_recovery_with_fresh_state() -> None:
    first = [cc_result("book_snapshot.json")]
    outage = [WsClosedError("502"), WsClosedError("502")]
    recovered = [cc_result("book_snapshot.json"), cc_result("book_update_1.json")]
    session, _, state, fanout, _ = build(
        CryptoComStreamProtocol(), [first, *outage, recovered], CC_SUBS
    )
    consumer = fanout.register("cdc-02", maxsize=100)
    epochs_seen: list[int] = []
    original = state.set_status

    def spy(key: StreamKey, status: StreamStatus) -> None:
        if key == BOOK and status is StreamStatus.LIVE:
            epochs_seen.append(session.health.epoch)
        original(key, status)

    state.set_status = spy  # type: ignore[method-assign]
    run(session, 4)
    assert session.health.connect_failures == 2
    assert epochs_seen == [1, 2]  # LIVE again only after the new connection's snapshot
    events = []
    while consumer.pending:
        events.append(asyncio.run(consumer.get()))
    assert [e.epoch for e in events if isinstance(e, StreamEvent)] == [1, 2, 2]


def test_stop_is_honoured_promptly_even_on_a_silent_connection() -> None:
    import time

    from tests.unit.streaming.fakes import HANG

    session, *_ = build(CryptoComStreamProtocol(), [[HANG]], CC_SUBS)

    async def main() -> float:
        stop = asyncio.Event()
        task = asyncio.create_task(session.run(stop))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        return time.monotonic() - started

    assert asyncio.run(main()) < 1.0


def test_backoff_never_overflows_after_a_very_long_outage() -> None:
    """v1.024 R1b: 2**1024 overflowed a float after ~1025 consecutive failures and ended the
    session (a long-running recorder or runtime died instead of retrying at the cap)."""
    policy = ReconnectPolicy()
    rng = random.Random(3)  # noqa: S311 - deterministic test jitter
    for failures in (1, 2, 7, 1025, 10**6):
        delay = policy.delay(failures, rng)
        assert 0 < delay <= policy.max_s
