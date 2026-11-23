"""Contracts for the central portfolio risk supervisor (implemented in M5).

The market-data layer only PROVIDES consistent snapshots; it never decides leverage, never
approves exposure and never executes. The supervisor reviews every proposed (paper) exposure
against one snapshot version, so decisions are reproducible, and will reject conflicting
positions across bots, duplicated exposure and excessive aggregate (hypothetical) leverage.
"""

from collections.abc import Iterable
from decimal import Decimal
from typing import Protocol, Self

from pydantic import Field, model_validator

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.model import DomainModel, LeverageDec, NonNegativeDec, PositiveDec
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.instruments.simulation import SimulatedLeverage
from shadow_trading_suite.streaming.state import MarketSnapshot, StreamKey


class MarketSnapshotProvider(Protocol):
    def snapshot(self, keys: Iterable[StreamKey] | None = None) -> MarketSnapshot: ...


class StressTerms(DomainModel):
    """What the supervisor needs to re-price an exposure under stress (quote currency)."""

    side: Side
    entry: PositiveDec
    mark: PositiveDec
    quantity: PositiveDec
    contract_size: PositiveDec
    leverage: LeverageDec
    mmr: NonNegativeDec | None  # None for spot
    fee_rate: NonNegativeDec
    funding_cost: NonNegativeDec
    eur_per_quote: PositiveDec


class ExposureTerms(DomainModel):
    underlying: str
    venue: Venue
    notional_eur: PositiveDec
    margin_eur: NonNegativeDec  # zero only for capital-free, exposure-reducing requests
    margin_quote: NonNegativeDec
    margin_currency: str
    stress: StressTerms


class ExposureRequest(DomainModel):
    """A bot's proposed PAPER exposure, bound to the snapshot it was derived from."""

    request_id: str = Field(min_length=1, max_length=128)  # idempotency key
    bot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    instrument_id: InstrumentId
    side: Side
    quantity: PositiveDec
    reference_price: PositiveDec
    simulated_leverage: SimulatedLeverage
    snapshot_version: int = Field(ge=0)
    reduce_only: bool = False
    # v2 rules: set by the pipeline for requests that can only reduce exposure (spot SELL);
    # absent in v1 records (False), so their supervisor decisions replay unchanged
    reduces_exposure: bool = False
    terms: ExposureTerms

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.reference_price

    @property
    def capital_free(self) -> bool:
        return self.terms.margin_quote == 0

    @model_validator(mode="after")
    def _only_reducing_requests_are_capital_free(self) -> Self:
        if (self.terms.margin_quote == 0 or self.terms.margin_eur == 0) and not (
            self.reduces_exposure and self.terms.margin_quote == 0 and self.terms.margin_eur == 0
        ):
            raise ValueError("only an exposure-reducing request may need no capital")
        return self


class SupervisorDecision(DomainModel):
    approved: bool
    reasons: tuple[str, ...]
    snapshot_version: int = Field(ge=0)

    @model_validator(mode="after")
    def _explained(self) -> Self:
        if not self.approved and not self.reasons:
            raise ValueError("a rejection must state at least one reason")
        return self


class PortfolioRiskSupervisor(Protocol):
    def review(self, request: ExposureRequest, snapshot: MarketSnapshot) -> SupervisorDecision: ...
