"""Real-time EUR conversion for paper risk and reporting (M6-C, C11/X5). Fail closed.

Neither venue lists a USD/EUR pair, so USD is triangulated from Coinbase public tickers:
EUR per USD = mid(BTC-EUR) / mid(BTC-USD). A second, independent route via ETH must agree
within `FxPolicy.cross_check_tolerance`. Every leg must be a LIVE, two-sided ticker received
within `max_age`, and the legs' exchange timestamps may be at most `max_skew` apart. A quote
records the event ids it was computed from, so every decision that used it is verifiable
(the M5 audit records the rate inside each decision's market inputs).

Crypto.com `USD` (perpetual settlement, spot quote) is valued with the same rate: paper
accounting does not distinguish the venues' USD units (ASSUMPTION, labelled in the report).
USD is never assumed equal to USDC/USDT.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.market import Ticker
from shadow_trading_suite.core.model import DomainModel
from shadow_trading_suite.core.time import UtcDatetime
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind, Subscription
from shadow_trading_suite.streaming.state import MarketSnapshot, StreamKey

QUANTUM = Decimal("1e-18")  # storage precision of valuation rates (M3)
USD_UNIT_ASSUMPTION = "Crypto.com USD and Coinbase USD are valued as the same unit (ASSUMPTION)"


def _cb(symbol: str) -> InstrumentId:
    return InstrumentId(venue=Venue.COINBASE, symbol=symbol)


PRIMARY = (_cb("BTC-EUR"), _cb("BTC-USD"))
CHECK = (_cb("ETH-EUR"), _cb("ETH-USD"))
FX_SUBSCRIPTIONS = tuple(Subscription(i, StreamKind.TICKER) for i in (*PRIMARY, *CHECK))


@dataclass(frozen=True)
class FxPolicy:
    max_age: timedelta = timedelta(seconds=10)  # proposal §6: stale > 10 s => no new orders
    max_skew: timedelta = timedelta(seconds=5)  # legs of one quote must be near-simultaneous
    cross_check_tolerance: Decimal = Decimal("0.005")  # relative ETH vs BTC route deviation


class FxQuote(DomainModel):
    currency: str
    eur_per_unit: Decimal
    cross_check: Decimal
    deviation: Decimal
    as_of: UtcDatetime  # oldest leg's exchange timestamp
    method: str
    sources: tuple[str, ...]  # event ids of the four tickers used


def _leg(
    snapshot: MarketSnapshot, iid: InstrumentId, policy: FxPolicy
) -> tuple[Decimal, StreamEvent]:
    event = snapshot.require(StreamKey(iid, StreamKind.TICKER), policy.max_age)
    t = event.payload
    if not isinstance(t, Ticker) or t.bid is None or t.ask is None:
        raise DataQualityError(f"{iid} ticker is not two-sided")
    return (t.bid + t.ask) / 2, event


def _route(
    snapshot: MarketSnapshot, legs: tuple[InstrumentId, InstrumentId], policy: FxPolicy
) -> tuple[Decimal, list[StreamEvent]]:
    eur, e1 = _leg(snapshot, legs[0], policy)
    usd, e2 = _leg(snapshot, legs[1], policy)
    return eur / usd, [e1, e2]


def usd_eur_rate(snapshot: MarketSnapshot, policy: FxPolicy) -> FxQuote:
    """EUR per USD from ONE snapshot, or DataQualityError (no rate => no new orders)."""
    rate, events = _route(snapshot, PRIMARY, policy)
    check, more = _route(snapshot, CHECK, policy)
    events += more
    times = [e.exchange_ts for e in events]
    if max(times) - min(times) > policy.max_skew:
        raise DataQualityError(f"EUR rate legs skew {max(times) - min(times)} > {policy.max_skew}")
    deviation = abs(check - rate) / rate
    if deviation > policy.cross_check_tolerance:
        raise DataQualityError(
            f"EUR rate cross-check failed: BTC route {rate:.6f} vs ETH route {check:.6f}"
        )
    return FxQuote(
        currency="USD",
        eur_per_unit=rate.quantize(QUANTUM, ROUND_HALF_EVEN),
        cross_check=check.quantize(QUANTUM, ROUND_HALF_EVEN),
        deviation=deviation.quantize(QUANTUM, ROUND_HALF_EVEN),
        as_of=min(times),
        method="triangulated:coinbase:BTC-EUR/BTC-USD;check:ETH-EUR/ETH-USD",
        sources=tuple(e.event_id for e in events),
    )


def eur_rates(quote: FxQuote) -> dict[str, Decimal]:
    """The `eur_rates` mapping the accounting reports take."""
    return {quote.currency: quote.eur_per_unit, "EUR": Decimal(1)}
