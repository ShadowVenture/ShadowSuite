"""Execution-level instrument support matrix (E9, proposal §3.4).

An order is accepted only for an explicitly enabled instrument, and only while every market
data feed that instrument class requires is LIVE and fresh. Nothing is proxied: index prices
are never used as marks, mids are never used as Crypto.com marks, funding is not extrapolated.
M5 `SimulationPermission` is not consulted or modified here.
"""

from dataclasses import dataclass
from datetime import timedelta

from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import Instrument, InstrumentId, InstrumentKind, Venue
from shadow_trading_suite.streaming.events import StreamKind
from shadow_trading_suite.streaming.state import MarketSnapshot, StreamKey

SPOT_FEEDS = (StreamKind.BOOK, StreamKind.TRADES)
PERP_FEEDS = (StreamKind.BOOK, StreamKind.TRADES, StreamKind.MARK, StreamKind.FUNDING)

# The approved M6 instruments (E9). Everything else is refused with a stated reason.
ENABLED: dict[InstrumentId, InstrumentKind] = {
    InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTC_USD"): InstrumentKind.SPOT,
    InstrumentId(venue=Venue.CRYPTOCOM, symbol="ETH_USD"): InstrumentKind.SPOT,
    InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTCUSD-PERP"): InstrumentKind.PERPETUAL,
    InstrumentId(venue=Venue.CRYPTOCOM, symbol="ETHUSD-PERP"): InstrumentKind.PERPETUAL,
    InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD"): InstrumentKind.SPOT,
    InstrumentId(venue=Venue.COINBASE, symbol="ETH-USD"): InstrumentKind.SPOT,
}


class ExecutionNotSupportedError(Exception):
    """The instrument (or its market data) is not approved for paper execution."""


@dataclass(frozen=True)
class Support:
    instrument_id: InstrumentId
    required_feeds: tuple[StreamKind, ...]


def support_for(inst: Instrument) -> Support:
    """Static check of the approved matrix; raises with the reason."""
    venue, kind = inst.id.venue, inst.kind
    if venue is Venue.COINBASE and kind is not InstrumentKind.SPOT:
        raise ExecutionNotSupportedError(
            f"{inst.id}: Coinbase derivatives are disabled (E9: no mark, funding stream, "
            "maintenance margin or settlement feed; FCM margining is account-level)"
        )
    if kind is InstrumentKind.DATED_FUTURE:
        raise ExecutionNotSupportedError(f"{inst.id}: dated futures disabled (no settlement feed)")
    if inst.quote != "USD" or (inst.settle or inst.quote) != "USD":
        raise ExecutionNotSupportedError(
            f"{inst.id}: only USD-quoted/settled instruments are enabled (C9)"
        )
    if ENABLED.get(inst.id) is not kind:
        raise ExecutionNotSupportedError(f"{inst.id}: not in the approved M6 execution allowlist")
    return Support(inst.id, SPOT_FEEDS if kind is InstrumentKind.SPOT else PERP_FEEDS)


def require_live_feeds(
    support: Support, snapshot: MarketSnapshot, max_age: timedelta, funding_age: timedelta
) -> None:
    """Every required feed must be LIVE and fresh, else DataQualityError (fail closed)."""
    missing = []
    for feed in support.required_feeds:
        try:
            age = funding_age if feed is StreamKind.FUNDING else max_age
            snapshot.require(StreamKey(support.instrument_id, feed), age)
        except DataQualityError as exc:
            missing.append(str(exc))
    if missing:
        raise DataQualityError("required market data unverified: " + "; ".join(missing))
