"""Central real-time service: one connection per venue for all bots; consistent views."""

import ast
import asyncio
import inspect
import random
from pathlib import Path
from typing import Any

import pytest

import shadow_trading_suite.streaming as streaming_pkg
from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.exchanges.ratelimit import TokenBucket
from shadow_trading_suite.risk.interfaces import MarketSnapshotProvider
from shadow_trading_suite.streaming.coinbase import CoinbaseStreamProtocol
from shadow_trading_suite.streaming.cryptocom import CryptoComStreamProtocol
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind, Subscription
from shadow_trading_suite.streaming.service import RealtimeMarketDataService
from shadow_trading_suite.streaming.session import StreamSession
from shadow_trading_suite.streaming.state import Fanout, MarketState, StreamKey, StreamStatus
from tests.unit.streaming.fakes import ScriptedConnector, Timeline, cc_result, ws

CB_BTC = InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD")
CC_BTC = InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTC_USD")


def service(
    cb_script: list[Any], cc_script: list[Any]
) -> tuple[RealtimeMarketDataService, dict[Venue, ScriptedConnector], Timeline]:
    tl = Timeline()
    state, fanout = MarketState(tl.clock, stale_after={}), Fanout()
    connectors = {
        Venue.COINBASE: ScriptedConnector([cb_script], tl),
        Venue.CRYPTOCOM: ScriptedConnector([cc_script], tl),
    }
    sessions = [
        StreamSession(
            proto,
            connectors[proto.venue],
            state,
            fanout,
            tl.clock,
            limiter=TokenBucket(1000, 100, monotonic=tl.monotonic, sleep=tl.sync_sleep),
            sleep=tl.sleep,
            rng=random.Random(1),  # noqa: S311 - deterministic test jitter
        )
        for proto in (CoinbaseStreamProtocol(), CryptoComStreamProtocol())
    ]
    return RealtimeMarketDataService(sessions, state, fanout), connectors, tl


CB_SCRIPT = [
    ws("coinbase/ticker_update.json", sequence_num=0),
    ws("coinbase/trades_update.json", sequence_num=1),
]
CC_SCRIPT = [
    cc_result("book_snapshot.json"),
    cc_result("book_update_1.json"),
    cc_result("ticker.json"),
]


def four_bots(svc: RealtimeMarketDataService) -> dict[str, Any]:
    cb = [Subscription(CB_BTC, StreamKind.TICKER), Subscription(CB_BTC, StreamKind.TRADES)]
    cc = [Subscription(CC_BTC, StreamKind.BOOK), Subscription(CC_BTC, StreamKind.TICKER)]
    return {
        "bot-01": svc.subscribe("bot-01", cb),
        "bot-02": svc.subscribe("bot-02", cb[:1]),
        "cdc-01": svc.subscribe("cdc-01", cc),
        "cdc-02": svc.subscribe("cdc-02", cc),
    }


def drain(consumer: Any) -> list[StreamEvent]:
    out = []
    while consumer.pending:
        out.append(asyncio.run(consumer.get()))
    return out


def test_four_bots_share_one_connection_per_venue() -> None:
    svc, connectors, _ = service(CB_SCRIPT, CC_SCRIPT)
    four_bots(svc)
    asyncio.run(svc.run(asyncio.Event(), max_attempts=1))
    assert [len(c.connections) for c in connectors.values()] == [1, 1]
    cb_channels = [f for f in connectors[Venue.COINBASE].connections[0].sent]
    assert sum('"ticker"' in f for f in cb_channels) == 1  # deduplicated across bot-01/bot-02


def test_bots_on_the_same_streams_receive_identical_event_sequences() -> None:
    svc, _, _ = service(CB_SCRIPT, CC_SCRIPT)
    bots = four_bots(svc)
    asyncio.run(svc.run(asyncio.Event(), max_attempts=1))
    a, b = drain(bots["cdc-01"]), drain(bots["cdc-02"])
    assert a == b
    assert len(a) == 3
    assert [e.event_id for e in a] == [e.event_id for e in b]
    cb2 = drain(bots["bot-02"])
    assert {e.kind for e in cb2} == {StreamKind.TICKER}  # only its declared interests


def test_snapshot_is_shared_and_requires_live_streams() -> None:
    svc, _, _ = service(CB_SCRIPT, CC_SCRIPT)
    four_bots(svc)
    asyncio.run(svc.run(asyncio.Event(), max_attempts=1))
    key = StreamKey(CC_BTC, StreamKind.TICKER)
    snap = svc.snapshot([key])
    # The scripted connection has ended: the stream is DOWN, so it is no longer current.
    assert snap.status[key] is StreamStatus.DOWN
    provider: MarketSnapshotProvider = svc  # static conformance (mypy)
    assert provider.snapshot([key]).version == snap.version


def test_one_session_per_venue_enforced() -> None:
    tl = Timeline()
    state, fanout = MarketState(tl.clock, stale_after={}), Fanout()

    def make() -> StreamSession:
        return StreamSession(
            CoinbaseStreamProtocol(),
            ScriptedConnector([], tl),
            state,
            fanout,
            tl.clock,
            limiter=TokenBucket(1, 1),
        )

    with pytest.raises(ValueError, match="coinbase"):
        RealtimeMarketDataService([make(), make()], state, fanout)


def test_subscriptions_must_be_declared_before_start_and_for_known_venues() -> None:
    svc, _, _ = service(CB_SCRIPT, CC_SCRIPT)
    svc.subscribe("bot-01", [Subscription(CB_BTC, StreamKind.TICKER)])
    asyncio.run(svc.run(asyncio.Event(), max_attempts=1))
    with pytest.raises(RuntimeError, match="before"):
        svc.subscribe("bot-02", [Subscription(CB_BTC, StreamKind.TICKER)])


def test_streaming_layer_never_decides_leverage_or_executes() -> None:
    forbidden = (
        "shadow_trading_suite.instruments.simulation",
        "shadow_trading_suite.execution",
        "shadow_trading_suite.portfolio",
    )
    for module in Path(inspect.getfile(streaming_pkg)).parent.glob("*.py"):
        tree = ast.parse(module.read_text())
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not [m for m in imported if m.startswith(forbidden)], module.name
