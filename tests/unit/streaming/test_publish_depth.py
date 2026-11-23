"""C13/R3: configurable published book depth; 50 levels (VERIFIED by the 2026-10-19 probes)
in the production wiring for both venues. No new URL, channel or header."""

import json

import pytest

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.streaming.cryptocom import CryptoComStreamProtocol, validate_outbound
from shadow_trading_suite.streaming.events import StreamKind, Subscription
from shadow_trading_suite.streaming.service import PUBLISH_DEPTH, RealtimeMarketDataService
from tests.unit.execution.harness import T0

BOOK = Subscription(InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTC_USD"), StreamKind.BOOK)


def test_default_depth_is_unchanged() -> None:
    (frame,) = CryptoComStreamProtocol().subscribe_frames([BOOK])
    assert json.loads(frame)["params"]["channels"] == ["book.BTC_USD.10"]


def test_depth_50_uses_the_verified_channel_and_passes_the_outbound_validator() -> None:
    (frame,) = CryptoComStreamProtocol(book_depth=50).subscribe_frames([BOOK])
    assert json.loads(frame)["params"]["channels"] == ["book.BTC_USD.50"]
    validate_outbound(frame)  # unchanged validator accepts it


@pytest.mark.parametrize("depth", [0, 5, 20, 100, 150])
def test_undocumented_depths_are_refused(depth: int) -> None:
    with pytest.raises(ValueError, match="book depth"):
        CryptoComStreamProtocol(book_depth=depth)


def test_production_wiring_publishes_50_levels_on_both_venues() -> None:
    service = RealtimeMarketDataService.create(ManualClock(T0))
    assert PUBLISH_DEPTH == 50
    for venue, session in service._sessions.items():
        assert session._depth == 50, venue
        if venue is Venue.CRYPTOCOM:
            assert session._protocol.book_depth == 50  # type: ignore[attr-defined]
