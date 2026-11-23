"""SYNTHETIC test portfolio: four fictitious bots (`bot-01` .. `bot-04`) with fictitious paper
allocations. These values exist only to exercise the risk and execution engines in tests; they
are not a recommendation, not a strategy and not anyone's budget. The suite ships no bots."""

from decimal import Decimal

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.risk.config import (
    BotRiskProfile,
    PortfolioConfig,
    default_portfolio_config,
)


def _bot(
    n: int,
    venue: Venue,
    eur: int,
    lev: str,
    pos: str,
    tot: str,
    eff: str,
    liq: str,
    daily: str,
    dd: str,
    positions: int,
    rate: int,
    stress: str | None = None,
) -> BotRiskProfile:
    return BotRiskProfile(
        bot_id=f"bot-0{n}",
        display_name=f"BOT-0{n}",
        venue=venue,
        allocation_eur=Decimal(eur),
        max_leverage=Decimal(lev),
        stress_max_leverage=Decimal(stress) if stress else None,
        max_position_margin=Decimal(pos),
        max_total_margin=Decimal(tot),
        max_effective_leverage=Decimal(eff),
        min_liquidation_distance=Decimal(liq),
        daily_loss_limit=Decimal(daily),
        max_drawdown=Decimal(dd),
        max_open_positions=positions,
        max_orders_per_minute=rate,
    )


def sample_portfolio_config() -> PortfolioConfig:
    bots = (
        _bot(1, Venue.COINBASE, 750, "20", "0.10", "0.50", "20", "0.003", "0.10", "0.25", 5, 30,
             "100"),
        _bot(2, Venue.COINBASE, 1250, "20", "0.15", "0.60", "8", "0.03", "0.07", "0.20", 5, 20),
        _bot(3, Venue.CRYPTOCOM, 1500, "10", "0.20", "0.70", "5", "0.06", "0.05", "0.15", 4, 10),
        _bot(4, Venue.CRYPTOCOM, 1500, "5", "0.25", "0.80", "3", "0.12", "0.04", "0.12", 4, 6),
    )  # fmt: skip
    return default_portfolio_config(bots, starting_capital_eur=Decimal(5000))
