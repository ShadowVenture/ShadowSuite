"""Deterministic isolated-margin, liquidation, volatility-buffer and shock models (Decimal).

Exchange parameters are DOCUMENTED only where a venue publishes them (Coinbase FCM initial
margin rates, published max leverage); maintenance margins and fees are ASSUMPTIONS.

Liquidation (isolated margin, posted margin = entry x qty / leverage; opening fee is expensed
from cash, D2): the position is liquidated when equity = maintenance margin, i.e.

    long :  P = entry * (1 - 1/L + F) / (1 - f - mmr)
    short:  P = entry * (1 + 1/L - F) / (1 + f + mmr)

with exit fee rate f, maintenance margin rate mmr and accrued funding cost F (fraction of entry
notional). An orderly liquidation loses the posted margin; a GAP (price jumps past the
liquidation price, or liquidation fails to execute in time) realises the full mark-to-market
loss plus costs, which can exceed the posted margin (bad debt).
"""

import itertools
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from shadow_trading_suite.core.instrument import Instrument, InstrumentKind, Venue
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.exchanges.coinbase.details import CoinbaseDetails
from shadow_trading_suite.risk.config import MarginAssumptions, ParameterSource, SourcedRate


class MarginModelUnavailableError(Exception):
    """The position cannot be modelled safely. Callers must reject (fail closed)."""


def maintenance_margin_rate(inst: Instrument, assumptions: MarginAssumptions) -> SourcedRate:
    if inst.kind is InstrumentKind.SPOT:
        raise MarginModelUnavailableError("spot positions are unleveraged; no margin model")
    if inst.id.venue is Venue.CRYPTOCOM:
        major = inst.base in assumptions.cryptocom_major_bases
        return assumptions.cryptocom_major_mmr if major else assumptions.cryptocom_other_mmr
    details = inst.venue_details
    if not isinstance(details, CoinbaseDetails) or details.future is None:
        raise MarginModelUnavailableError(f"{inst.id}: no futures details")
    if inst.sub_venue == "INTX":
        raise MarginModelUnavailableError(
            f"{inst.id}: INTX book is empty and margin tiers unpublished"
        )
    rates = [
        r
        for m in (details.future.intraday_margin_rate, details.future.overnight_margin_rate)
        if m is not None
        for r in (m.long_margin_rate, m.short_margin_rate)
        if r is not None
    ]
    if not rates:
        raise MarginModelUnavailableError(f"{inst.id}: no published margin rates")
    fraction = assumptions.fcm_maintenance_fraction
    return SourcedRate(
        value=max(rates) * fraction.value,
        source=ParameterSource.ASSUMPTION,
        note=f"{fraction.value} x worst documented FCM initial margin {max(rates)}",
    )


@dataclass(frozen=True)
class PositionTerms:
    side: Side
    entry: Decimal
    quantity: Decimal
    contract_size: Decimal
    leverage: Decimal
    mmr: Decimal
    fee_rate: Decimal
    funding_cost: Decimal = Decimal(0)  # accrued funding paid, fraction of entry notional

    @property
    def notional(self) -> Decimal:
        return self.entry * self.quantity * self.contract_size

    @property
    def margin(self) -> Decimal:
        return self.notional / self.leverage


def liquidation_price(t: PositionTerms) -> Decimal:
    if t.mmr + t.fee_rate + t.funding_cost >= 1 / t.leverage:
        raise MarginModelUnavailableError(
            f"leverage {t.leverage} with maintenance {t.mmr} would liquidate immediately"
        )
    if t.side is Side.BUY:
        return t.entry * (1 - 1 / t.leverage + t.funding_cost) / (1 - t.fee_rate - t.mmr)
    return t.entry * (1 + 1 / t.leverage - t.funding_cost) / (1 + t.fee_rate + t.mmr)


def liquidation_distance(t: PositionTerms, mark: Decimal) -> Decimal:
    """Relative adverse move from `mark` that triggers liquidation."""
    liq = liquidation_price(t)
    return (mark - liq) / mark if t.side is Side.BUY else (liq - mark) / mark


def horizon_volatility(
    closes: Sequence[Decimal], candle_minutes: int, horizon_minutes: int
) -> Decimal:
    """Std-dev of simple returns per candle, scaled by sqrt(horizon / candle)."""
    if len(closes) < 3:
        raise MarginModelUnavailableError("not enough candles to estimate volatility")
    returns = [b / a - 1 for a, b in itertools.pairwise(closes)]
    return statistics.pstdev(returns) * (Decimal(horizon_minutes) / Decimal(candle_minutes)).sqrt()


def required_liquidation_buffer(
    minimum: Decimal,
    sigma: Decimal,
    multiplier: Decimal,
    fee_rate: Decimal,
    spread: Decimal,
    slippage: Decimal,
) -> Decimal:
    """Volatility-aware minimum distance: k x sigma + round-trip fees + spread + slippage."""
    return max(minimum, multiplier * sigma + 2 * fee_rate + spread + slippage)


@dataclass(frozen=True)
class ShockOutcome:
    price: Decimal
    loss: Decimal  # in quote currency, includes exit fee, slippage and funding
    margin: Decimal
    liquidated: bool
    bad_debt: Decimal  # loss beyond the posted margin (only possible in gap scenarios)


def shock_outcome(
    t: PositionTerms, mark: Decimal, shock: Decimal, slippage: Decimal, *, gap: bool
) -> ShockOutcome:
    adverse = mark * (1 - shock) if t.side is Side.BUY else mark * (1 + shock)
    size = t.quantity * t.contract_size
    move = (t.entry - adverse) if t.side is Side.BUY else (adverse - t.entry)
    full_loss = move * size + adverse * size * (t.fee_rate + slippage) + t.funding_cost * t.notional
    liq = liquidation_price(t)
    liquidated = adverse <= liq if t.side is Side.BUY else adverse >= liq
    if liquidated and not gap:
        return ShockOutcome(adverse, t.margin, t.margin, True, Decimal(0))
    return ShockOutcome(
        adverse, full_loss, t.margin, liquidated, max(Decimal(0), full_loss - t.margin)
    )
