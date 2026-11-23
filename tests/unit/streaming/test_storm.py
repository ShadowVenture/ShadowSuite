"""Streaming under stress (M7-D): reconnection storms, gaps and saturated consumers.

A long, seeded sequence of connect failures, short-lived connections, malformed frames,
silences, sequence gaps and deltas-before-snapshot. The session must never exceed the backoff
cap, never amplify outbound traffic, never mark a book LIVE in an epoch without that epoch's
snapshot, recover fully afterwards and behave identically on a second run (replayable).
"""

import asyncio
import random
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from shadow_trading_suite.streaming.cryptocom import CryptoComStreamProtocol
from shadow_trading_suite.streaming.events import StreamEvent
from shadow_trading_suite.streaming.session import ReconnectPolicy
from shadow_trading_suite.streaming.state import GapNotice, OverflowPolicy, StreamKey, StreamStatus
from shadow_trading_suite.streaming.transport import WsClosedError
from tests.unit.streaming.fakes import SILENCE, cc_result
from tests.unit.streaming.test_session import BOOK, CC_SUBS, build

CAP_S = ReconnectPolicy().max_s

SNAPSHOT = "snapshot"
GAPPED = "gapped"  # snapshot, then a delta whose pu does not chain
DELTA_FIRST = "delta_first"
MALFORMED = "malformed"
SILENT = "silent"
REFUSED = "refused"
KINDS = (SNAPSHOT, GAPPED, DELTA_FIRST, MALFORMED, SILENT, REFUSED)


def _gap(data: dict[str, Any]) -> None:
    data["result"]["data"][0]["pu"] = 1  # never the snapshot's u


def script_for(kind: str) -> Any:
    if kind == REFUSED:
        return WsClosedError("503 storm")
    return {
        SNAPSHOT: [cc_result("book_snapshot.json"), cc_result("book_update_1.json")],
        GAPPED: [cc_result("book_snapshot.json"), cc_result("book_update_1.json", _gap)],
        DELTA_FIRST: [cc_result("book_update_2.json")],
        MALFORMED: ["{not json"],
        SILENT: [SILENCE],
    }[kind]


def storm(kinds: list[str]) -> dict[str, Any]:
    scripts = [script_for(k) for k in kinds] + [script_for(SNAPSHOT)]  # then a healthy one
    session, connector, state, fanout, tl = build(CryptoComStreamProtocol(), scripts, CC_SUBS)
    consumer = fanout.register("bot", maxsize=10_000)
    live_epochs: list[int] = []
    snapshot_epochs: set[int] = set()
    original_status, original_snapshot = state.set_status, session._book

    def spy_status(key: StreamKey, status: StreamStatus) -> None:
        if key == BOOK and status is StreamStatus.LIVE:
            live_epochs.append(session.health.epoch)
        original_status(key, status)

    async def spy_book(conn: Any, msg: Any, now: Any) -> bool:
        if type(msg).__name__ == "BookSnapshotMsg":
            snapshot_epochs.add(session.health.epoch)
        return await original_snapshot(conn, msg, now)

    state.set_status = spy_status  # type: ignore[method-assign]
    session._book = spy_book  # type: ignore[method-assign]
    asyncio.run(session.run(asyncio.Event(), max_attempts=len(scripts)))
    events = []
    while (item := consumer.get_nowait()) is not None:
        events.append(item)
    return {
        "session": session,
        "connector": connector,
        "state": state,
        "sleeps": list(tl.sleeps),
        "live_epochs": live_epochs,
        "snapshot_epochs": snapshot_epochs,
        "events": [(e.epoch, e.event_id) for e in events if isinstance(e, StreamEvent)],
        "gaps": [e for e in events if isinstance(e, GapNotice)],
    }


def storm_kinds(seed: int, n: int) -> list[str]:
    rng = random.Random(seed)  # noqa: S311 - deterministic scenario
    return [rng.choice(KINDS) for _ in range(n)]


class TestReconnectionStorm:
    KINDS = storm_kinds(2026, 120)

    def test_backoff_is_always_capped_and_never_zero(self) -> None:
        r = storm(self.KINDS)
        connect_delay = CryptoComStreamProtocol().connect_delay_s
        backoffs = [s for s in r["sleeps"] if s != connect_delay and s > 0.01]
        assert backoffs
        assert max(backoffs) <= CAP_S
        assert all(s >= 0 for s in r["sleeps"])
        refused = self.KINDS.count(REFUSED)
        assert r["session"].health.connect_failures == refused
        # consecutive refusals grow the backoff, and the cap is reached in a long outage
        assert max(backoffs) >= CAP_S * ReconnectPolicy().jitter_low or refused < 7

    def test_a_book_is_live_only_in_an_epoch_that_received_its_snapshot(self) -> None:
        r = storm(self.KINDS)
        assert r["live_epochs"]
        assert set(r["live_epochs"]) <= r["snapshot_epochs"]

    def test_full_recovery_after_the_storm(self) -> None:
        r = storm(self.KINDS)
        session = r["session"]
        last = session.health.epoch
        assert last in r["snapshot_epochs"]
        assert r["events"][-1][0] == last  # the final, healthy connection delivers again
        epochs = [e for e, _ in r["events"]]
        assert epochs == sorted(epochs)  # never an event from an older connection afterwards
        assert r["state"].status(BOOK) is StreamStatus.DOWN  # script ended: fail closed

    def test_no_outbound_amplification(self) -> None:
        r = storm(self.KINDS)
        per_connection = {len(c.sent) for c in r["connector"].connections}
        # each connection sends its subscribe frame(s) plus at most one resync pair
        assert max(per_connection) <= 3

    def test_the_storm_is_replayable(self) -> None:
        a, b = storm(self.KINDS), storm(self.KINDS)
        assert a["sleeps"] == b["sleeps"]
        assert a["events"] == b["events"]
        assert a["live_epochs"] == b["live_epochs"]


@settings(max_examples=40, deadline=None)
@given(st.lists(st.sampled_from(KINDS), min_size=1, max_size=25))
def test_any_storm_never_goes_live_without_a_snapshot(kinds: list[str]) -> None:
    r = storm(kinds)
    assert set(r["live_epochs"]) <= r["snapshot_epochs"]
    assert all(s <= CAP_S for s in r["sleeps"])
    assert r["session"].health.epoch in r["snapshot_epochs"]  # the healthy tail recovers


class TestSaturatedConsumers:
    def test_overflow_drops_oldest_with_a_gap_notice_or_disconnects(self) -> None:
        scripts = [script_for(SNAPSHOT), script_for(SNAPSHOT), script_for(SNAPSHOT)]
        session, _, _, fanout, _ = build(CryptoComStreamProtocol(), scripts, CC_SUBS)
        drop = fanout.register("drop", maxsize=2)
        cut = fanout.register("cut", maxsize=2, policy=OverflowPolicy.DISCONNECT)
        full = fanout.register("full", maxsize=100)
        asyncio.run(session.run(asyncio.Event(), max_attempts=3))
        assert full.pending > 2
        first = drop.get_nowait()
        assert isinstance(first, GapNotice)
        assert first.dropped == full.pending - 2
        assert cut.disconnected
