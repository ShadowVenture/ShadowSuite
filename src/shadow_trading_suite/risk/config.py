"""Portfolio, bot-risk, margin and fee configuration (paper trading only).

Every exchange-related parameter carries a `source`: DOCUMENTED (published by the venue) or
ASSUMPTION (undocumented; never treated as a verified exchange capability). Leverage caps are
hypothetical SIMULATION caps, not claims of exchange-supported leverage. There is no setting
for live trading or moving funds off the simulated ledger; unknown keys are rejected.
"""

import tomllib
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from shadow_trading_suite.core.decimal import Dec
from shadow_trading_suite.core.instrument import InstrumentKind, Venue
from shadow_trading_suite.core.model import DomainModel, LeverageDec, NonNegativeDec, PositiveDec
from shadow_trading_suite.instruments.simulation import SimulationPermission

Fraction = Annotated[Dec, Field(ge=0, le=1)]
SimLeverage = Annotated[LeverageDec, Field(le=100)]


class ParameterSource(StrEnum):
    DOCUMENTED = "documented"
    ASSUMPTION = "assumption"


class SourcedRate(DomainModel):
    value: NonNegativeDec
    source: ParameterSource
    note: str = ""


class FeeAssumptions(DomainModel):
    spot_taker: SourcedRate
    spot_maker: SourcedRate
    derivatives_taker: SourcedRate
    derivatives_maker: SourcedRate
    conversion_cost: SourcedRate
    min_slippage: SourcedRate

    @model_validator(mode="after")
    def _conversion_cost_positive(self) -> Self:
        if self.conversion_cost.value <= 0:
            raise ValueError("conversion cost must be modelled explicitly (> 0)")
        return self


class MarginAssumptions(DomainModel):
    cryptocom_major_mmr: SourcedRate
    cryptocom_other_mmr: SourcedRate
    cryptocom_major_bases: frozenset[str]
    fcm_maintenance_fraction: SourcedRate  # maintenance = fraction x published initial margin
    volatility_multiplier: PositiveDec  # buffer = multiplier x sigma over the horizon
    volatility_horizon_minutes: int = Field(ge=1, le=1440)


class BotRiskProfile(DomainModel):
    bot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")  # plugin id
    display_name: str
    venue: Venue
    allocation_eur: PositiveDec
    cash_currency: Literal["USD"] = "USD"  # both venues' derivatives settle in USD
    max_leverage: SimLeverage
    stress_max_leverage: SimLeverage | None = None  # only in explicitly enabled stress tests
    max_position_margin: Fraction
    max_total_margin: Fraction
    max_effective_leverage: PositiveDec
    min_liquidation_distance: Fraction
    daily_loss_limit: Fraction
    max_drawdown: Fraction
    max_open_positions: int = Field(ge=1)
    max_orders_per_minute: int = Field(ge=1)

    @model_validator(mode="after")
    def _stress_above_ordinary(self) -> Self:
        if self.stress_max_leverage is not None and self.stress_max_leverage <= self.max_leverage:
            raise ValueError("stress_max_leverage must exceed the ordinary max_leverage")
        if self.max_position_margin > self.max_total_margin:
            raise ValueError("max_position_margin cannot exceed max_total_margin")
        return self


class PortfolioRiskLimits(DomainModel):
    max_gross_per_underlying: PositiveDec
    max_gross_per_venue: PositiveDec
    max_gross_total: PositiveDec
    max_margin_in_use: Fraction
    max_instrument_concentration: Fraction
    stress_shock: Fraction
    stress_shocks: tuple[Fraction, ...]
    max_stress_loss: Fraction
    emergency_daily_loss: Fraction
    emergency_drawdown: Fraction
    reject_opposing_positions: bool
    max_data_age_derivatives_s: int = Field(ge=1)
    max_data_age_spot_s: int = Field(ge=1)


class ProfitReservationConfig(DomainModel):
    """RETIRED: not supported by the suite (paper-only policy).

    There is no automatic profit reservation, savings allocation, skim, payout or transfer for
    any bot. Profits and losses stay in each bot's own ledger and equity and are reusable only
    within the unchanged M5 limits and capital allocations. This schema exists ONLY so a
    historical config file that still carries the old, disabled `[profit_reservation]` table
    keeps loading; its values are
    never read by any code path. It is not part of the default configuration, and enabling it
    is refused."""

    enabled: bool
    threshold_eur: PositiveDec
    reserve_eur: PositiveDec
    max_per_day: Literal[1]
    timezone: Literal["UTC"]
    settlement: Literal["completed_day_at_0000_utc"]

    @model_validator(mode="after")
    def _reserve_le_threshold(self) -> Self:
        if self.enabled:
            raise ValueError(
                "profit_reservation.enabled: discretionary profit reserves are not supported "
                "(paper-only policy); profits stay in each bot's trading equity"
            )
        if self.reserve_eur > self.threshold_eur:
            raise ValueError("reserve_eur cannot exceed threshold_eur")
        return self


class LiquidityReserveConfig(DomainModel):
    """Optional EUR buffer kept in the treasury (never allocated to bots). Disabled by default;
    enabling it requires an explicitly approved allocation change — allocations are never
    adjusted automatically."""

    enabled: bool = False
    fraction: Decimal = Field(default=Decimal("0.10"), gt=0, le=Decimal("0.5"))


class PortfolioConfig(DomainModel):
    portfolio_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    base_currency: Literal["EUR"]
    starting_capital_eur: PositiveDec
    bots: tuple[BotRiskProfile, ...]
    limits: PortfolioRiskLimits
    margin: MarginAssumptions
    fees: FeeAssumptions
    # RETIRED: accepted only from historical files, always disabled, never read
    profit_reservation: ProfitReservationConfig | None = None
    liquidity_reserve: LiquidityReserveConfig = LiquidityReserveConfig()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [b.bot_id for b in self.bots]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate bot_id")
        total = sum((b.allocation_eur for b in self.bots), Decimal(0))
        if total > self.starting_capital_eur:
            raise ValueError(
                f"allocations {total} exceed starting capital {self.starting_capital_eur}"
            )
        reserve = self.liquidity_reserve
        if reserve.enabled and total > self.starting_capital_eur - self.treasury_floor_eur():
            limit = self.starting_capital_eur - self.treasury_floor_eur()
            raise ValueError(
                f"a {reserve.fraction:.0%} liquidity reserve requires allocations <= {limit:f}; "
                f"current allocations total {total:f}. Allocations are not changed "
                "automatically: approve an explicit allocation change first"
            )
        return self

    def treasury_floor_eur(self) -> Decimal:
        if not self.liquidity_reserve.enabled:
            return Decimal(0)
        return self.starting_capital_eur * self.liquidity_reserve.fraction

    def bot(self, bot_id: str) -> BotRiskProfile:
        for b in self.bots:
            if b.bot_id == bot_id:
                return b
        raise KeyError(bot_id)


def _assumption(value: str, note: str) -> SourcedRate:
    return SourcedRate(value=Decimal(value), source=ParameterSource.ASSUMPTION, note=note)


def default_portfolio_config(
    bots: tuple[BotRiskProfile, ...] = (),
    *,
    starting_capital_eur: Decimal = Decimal(10000),
    portfolio_id: str = "paper-main",
) -> PortfolioConfig:
    """Example paper defaults: portfolio limits, margin and fee ASSUMPTIONS, and NO bots.

    Bots are added by the operator (one `BotRiskProfile` per installed plugin); the suite
    ships none. Every fee and margin value is an ASSUMPTION, never account evidence."""

    return PortfolioConfig(
        portfolio_id=portfolio_id,
        base_currency="EUR",
        starting_capital_eur=starting_capital_eur,
        bots=bots,
        limits=PortfolioRiskLimits(
            max_gross_per_underlying=Decimal(4),
            max_gross_per_venue=Decimal(5),
            max_gross_total=Decimal(6),
            max_margin_in_use=Decimal("0.60"),
            max_instrument_concentration=Decimal("0.40"),
            stress_shock=Decimal("0.05"),
            stress_shocks=(Decimal("0.01"), Decimal("0.03"), Decimal("0.05"), Decimal("0.10")),
            max_stress_loss=Decimal("0.15"),
            emergency_daily_loss=Decimal("0.08"),
            emergency_drawdown=Decimal("0.20"),
            reject_opposing_positions=True,
            max_data_age_derivatives_s=5,
            max_data_age_spot_s=10,
        ),
        margin=MarginAssumptions(
            cryptocom_major_mmr=_assumption("0.005", "no public maintenance tiers; BTC/ETH"),
            cryptocom_other_mmr=_assumption("0.01", "no public maintenance tiers; other perps"),
            cryptocom_major_bases=frozenset({"BTC", "ETH"}),
            fcm_maintenance_fraction=_assumption(
                "0.80", "maintenance as share of published FCM initial margin"
            ),
            volatility_multiplier=Decimal(3),
            volatility_horizon_minutes=60,
        ),
        fees=FeeAssumptions(
            spot_taker=_assumption("0.006", "ASSUMPTION: verify against the official fee schedule"),
            spot_maker=_assumption("0.004", "ASSUMPTION: verify against the official fee schedule"),
            derivatives_taker=_assumption(
                "0.0006", "ASSUMPTION: verify against the official fee schedule"
            ),
            derivatives_maker=_assumption(
                "0.0002", "ASSUMPTION: verify against the official fee schedule"
            ),
            conversion_cost=_assumption("0.002", "simulated FX/stablecoin conversion cost"),
            min_slippage=_assumption("0.0002", "floor when the book walk finds less"),
        ),
    )


def load_portfolio_config(path: Path) -> PortfolioConfig:
    """TOML with money as strings; floats, unknown keys and unsafe settings are rejected."""
    with path.open("rb") as f:
        return PortfolioConfig.model_validate(tomllib.load(f))


def simulation_permission(bot: BotRiskProfile) -> SimulationPermission:
    """Default per-bot paper simulation allowlist (instrument kinds on the bot's venue)."""
    kinds = (
        frozenset({InstrumentKind.SPOT, InstrumentKind.DATED_FUTURE})  # FCM; INTX not modelable
        if bot.venue is Venue.COINBASE
        else frozenset({InstrumentKind.SPOT, InstrumentKind.PERPETUAL, InstrumentKind.DATED_FUTURE})
    )
    return SimulationPermission(bot_id=bot.bot_id, venue=bot.venue, instrument_kinds=kinds)
