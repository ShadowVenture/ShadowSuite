"""Per-bot risk engine: a PURE function of explicit inputs (deterministic, replayable).

`evaluate_bot` returns APPROVE / RESIZE / REJECT with the full trail of checks. It fails
closed: any missing, stale or unmodelable input rejects. It never executes, reserves capital
or contacts a venue. Hypothetical leverage beyond a bot's ordinary cap (e.g. a 100x stress cap) is
only available through `stress_scenario`, which produces a non-executable report.
"""

from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import Field, model_validator

from shadow_trading_suite.core.decimal import Dec
from shadow_trading_suite.core.instrument import Instrument, InstrumentId, InstrumentKind
from shadow_trading_suite.core.model import DomainModel, LeverageDec, NonNegativeDec, PositiveDec
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.core.time import UtcDatetime
from shadow_trading_suite.instruments.simulation import (
    BotSimulationProfile,
    SimulatedLeverage,
    SimulationNotPermittedError,
    simulated_leverage,
)
from shadow_trading_suite.risk.config import (
    BotRiskProfile,
    PortfolioConfig,
    SourcedRate,
    simulation_permission,
)
from shadow_trading_suite.risk.margin import (
    MarginModelUnavailableError,
    PositionTerms,
    liquidation_distance,
    liquidation_price,
    maintenance_margin_rate,
    required_liquidation_buffer,
    shock_outcome,
)

# Risk rules version recorded with every decision (replay uses the recorded version).
# v1 = M5. v2 (M7-A): a spot SELL only reduces exposure - spot is unleveraged and the ledger
# reserves the sold inventory, so it can never create exposure. It passes the halt, loss and
# drawdown gates like a derivatives reduce-only order and is capped at the bot's holding.
RULES_VERSION = 2
RULES_VERSIONS = (1, 2)


def reduces_exposure(p: "TradeProposal", inst: Instrument, rules: int) -> bool:
    if rules not in RULES_VERSIONS:
        raise ValueError(f"unknown risk rules version {rules}")
    spot_sell = rules >= 2 and inst.kind is InstrumentKind.SPOT and p.side is Side.SELL
    return p.reduce_only or spot_sell


class RiskVerdict(StrEnum):
    APPROVE = "approve"
    RESIZE = "resize"
    REJECT = "reject"


class TradeProposal(DomainModel):
    request_id: str = Field(min_length=1, max_length=128)
    bot_id: str
    instrument_id: InstrumentId
    side: Side
    quantity: PositiveDec
    leverage: LeverageDec
    reduce_only: bool = False
    created_at: UtcDatetime


class OpenPosition(DomainModel):
    position_id: str
    bot_id: str
    instrument_id: InstrumentId
    underlying: str
    side: Side
    quantity: PositiveDec
    notional_eur: PositiveDec
    margin_eur: NonNegativeDec
    leverage: LeverageDec


class BotState(DomainModel):
    bot_id: str
    equity_eur: Dec
    start_of_day_equity_eur: PositiveDec
    peak_equity_eur: PositiveDec
    open_positions: tuple[OpenPosition, ...] = ()
    orders_last_minute: int = Field(ge=0)
    halted: bool


class MarketInputs(DomainModel):
    """Market values taken from ONE validated snapshot (recorded verbatim for replay)."""

    instrument_id: InstrumentId
    snapshot_version: int = Field(ge=0)
    as_of: UtcDatetime
    bid: PositiveDec
    ask: PositiveDec
    mark: PositiveDec
    mark_source: str  # venue_mark | last_trade | mid  (Coinbase publishes no mark price)
    funding_rate: Dec | None = None
    funding_interval_s: int | None = None
    sigma: NonNegativeDec  # volatility over the configured horizon
    slippage: NonNegativeDec  # estimated relative slippage for the order size
    eur_per_quote: PositiveDec

    @model_validator(mode="after")
    def _uncrossed(self) -> Self:
        if self.bid >= self.ask:
            raise ValueError("bid must be below ask")
        return self

    @property
    def spread(self) -> Decimal:
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2)


class CheckResult(DomainModel):
    name: str
    passed: bool
    detail: str


class BotRiskDecision(DomainModel):
    request_id: str
    bot_id: str
    verdict: RiskVerdict
    approved_quantity: Dec | None
    checks: tuple[CheckResult, ...]
    reasons: tuple[str, ...]
    leverage: SimulatedLeverage | None = None
    entry_price: Dec | None = None
    margin_required_quote: Dec | None = None
    margin_required_eur: Dec | None = None
    notional_eur: Dec | None = None
    liquidation_price: Dec | None = None
    liquidation_distance: Dec | None = None
    required_buffer: Dec | None = None
    maintenance_margin: SourcedRate | None = None
    snapshot_version: int


def _fee(inst: Instrument, cfg: PortfolioConfig) -> Decimal:
    taker = cfg.fees.spot_taker if inst.kind is InstrumentKind.SPOT else cfg.fees.derivatives_taker
    return taker.value


def _entry(p: TradeProposal, m: MarketInputs) -> Decimal:
    return m.ask * (1 + m.slippage) if p.side is Side.BUY else m.bid * (1 - m.slippage)


def _funding_cost(p: TradeProposal, m: MarketInputs) -> Decimal:
    if m.funding_rate is None:
        return Decimal(0)
    paying = m.funding_rate if p.side is Side.BUY else -m.funding_rate
    return max(Decimal(0), paying)  # one interval of adverse funding


def _sim_profile(bot: BotRiskProfile, cap: Decimal) -> BotSimulationProfile:
    return BotSimulationProfile(
        bot_id=bot.bot_id, permission=simulation_permission(bot), max_simulated_leverage=cap
    )


def evaluate_bot(
    p: TradeProposal,
    inst: Instrument,
    state: BotState,
    m: MarketInputs,
    cfg: PortfolioConfig,
    rules: int = RULES_VERSION,
) -> BotRiskDecision:
    bot = cfg.bot(p.bot_id)
    checks: list[CheckResult] = []

    def check(name: str, ok: bool, detail: str) -> bool:
        checks.append(CheckResult(name=name, passed=ok, detail=detail))
        return ok

    def reject(**extra: Any) -> BotRiskDecision:
        reasons = tuple(f"{c.name}: {c.detail}" for c in checks if not c.passed)
        return BotRiskDecision(
            request_id=p.request_id,
            bot_id=p.bot_id,
            verdict=RiskVerdict.REJECT,
            approved_quantity=None,
            checks=tuple(checks),
            reasons=reasons,
            snapshot_version=m.snapshot_version,
            **extra,
        )

    increasing = not reduces_exposure(p, inst, rules)
    spot_sell = inst.kind is InstrumentKind.SPOT and p.side is Side.SELL and not increasing
    held = sum((x.quantity for x in state.open_positions if x.instrument_id == inst.id), Decimal(0))
    check(
        "halt",
        not (state.halted and increasing),
        "bot halted (kill switch)" if state.halted else "ok",
    )
    loss = (state.start_of_day_equity_eur - state.equity_eur) / state.start_of_day_equity_eur
    check(
        "daily_loss",
        not (increasing and loss >= bot.daily_loss_limit),
        f"daily loss {loss:.4f} vs limit {bot.daily_loss_limit}",
    )
    dd = (state.peak_equity_eur - state.equity_eur) / state.peak_equity_eur
    check(
        "drawdown",
        not (increasing and dd >= bot.max_drawdown),
        f"drawdown {dd:.4f} vs limit {bot.max_drawdown}",
    )
    permitted = simulation_permission(bot).allows(inst) and inst.id == p.instrument_id
    check(
        "permission",
        permitted,
        f"{inst.id} {'permitted' if permitted else 'not permitted'} for {bot.bot_id}",
    )
    max_age = timedelta(
        seconds=cfg.limits.max_data_age_spot_s
        if inst.kind is InstrumentKind.SPOT
        else cfg.limits.max_data_age_derivatives_s
    )
    age = p.created_at - m.as_of
    fresh = m.instrument_id == inst.id and timedelta(0) <= age <= max_age
    check("market_data", fresh, f"age {age}, instrument {m.instrument_id}")
    if inst.kind is InstrumentKind.SPOT:
        lev_ok, lev_detail = p.leverage == 1, "spot is unleveraged"
    elif p.leverage > bot.max_leverage:
        lev_ok, lev_detail = (
            False,
            (
                f"{p.leverage}x exceeds ordinary cap {bot.max_leverage}x; hypothetical leverage "
                "above the ordinary cap is only available in explicitly enabled stress scenarios"
            ),
        )
    else:
        lev_ok, lev_detail = True, f"{p.leverage}x within {bot.max_leverage}x"
    check("leverage", lev_ok, lev_detail)
    check(
        "order_rate",
        state.orders_last_minute < bot.max_orders_per_minute,
        f"{state.orders_last_minute} orders in the last minute",
    )
    check(
        "positions",
        not (increasing and len(state.open_positions) >= bot.max_open_positions),
        f"{len(state.open_positions)} open positions",
    )
    if spot_sell:
        check("holding", held > 0, f"held {held} of {inst.id}")
    if not all(c.passed for c in checks):
        return reject()

    try:
        leverage = simulated_leverage(_sim_profile(bot, bot.max_leverage), inst, p.leverage)
    except SimulationNotPermittedError as exc:
        check("leverage", False, str(exc))
        return reject()

    entry = _entry(p, m)
    fee = _fee(inst, cfg)
    unit_notional_quote = entry * inst.contract_size
    unit_margin_eur = unit_notional_quote / p.leverage * m.eur_per_quote
    unit_notional_eur = unit_notional_quote * m.eur_per_quote
    existing_margin = sum((x.margin_eur for x in state.open_positions), Decimal(0))
    existing_notional = sum((x.notional_eur for x in state.open_positions), Decimal(0))
    equity = max(state.equity_eur, Decimal(0))
    limits = (
        [p.quantity, held]  # selling a holding frees capital: capped by what is held
        if spot_sell
        else [
            p.quantity,
            bot.max_position_margin * equity / unit_margin_eur,
            (bot.max_total_margin * equity - existing_margin) / unit_margin_eur,
            (bot.max_effective_leverage * equity - existing_notional) / unit_notional_eur,
        ]
    )
    allowed = max(Decimal(0), min(limits))
    allowed = (allowed / inst.lot_size).to_integral_value(rounding=ROUND_FLOOR) * inst.lot_size
    sized = allowed >= max(inst.min_qty, inst.lot_size) and allowed > 0
    check(
        "margin",
        sized,
        f"allowed {allowed} of {p.quantity} (position {bot.max_position_margin}, total "
        f"{bot.max_total_margin}, effective {bot.max_effective_leverage}x of equity {equity})",
    )
    if not sized:
        return reject(leverage=leverage, entry_price=entry)

    # a spot SELL is backed by the held inventory the ledger reserves, never by cash (v2)
    margin_quote = Decimal(0) if spot_sell else allowed * unit_notional_quote / p.leverage
    extra: dict[str, Any] = {
        "leverage": leverage,
        "entry_price": entry,
        "margin_required_quote": margin_quote,
        "margin_required_eur": Decimal(0) if spot_sell else allowed * unit_margin_eur,
        "notional_eur": allowed * unit_notional_eur,
    }
    if inst.kind is not InstrumentKind.SPOT:
        try:
            mmr = maintenance_margin_rate(inst, cfg.margin)
            terms = PositionTerms(
                p.side,
                entry,
                allowed,
                inst.contract_size,
                p.leverage,
                mmr.value,
                fee,
                _funding_cost(p, m),
            )
            liq = liquidation_price(terms)
            dist = liquidation_distance(terms, m.mark)
        except MarginModelUnavailableError as exc:
            check("liquidation", False, str(exc))
            return reject(**extra)
        buffer = required_liquidation_buffer(
            bot.min_liquidation_distance,
            m.sigma,
            cfg.margin.volatility_multiplier,
            fee,
            m.spread,
            max(m.slippage, cfg.fees.min_slippage.value),
        )
        extra.update(
            liquidation_price=liq,
            liquidation_distance=dist,
            required_buffer=buffer,
            maintenance_margin=mmr,
        )
        if not check("liquidation", dist > buffer, f"distance {dist:.5f} vs required {buffer:.5f}"):
            return reject(**extra)
    verdict = RiskVerdict.APPROVE if allowed == p.quantity else RiskVerdict.RESIZE
    return BotRiskDecision(
        request_id=p.request_id,
        bot_id=p.bot_id,
        verdict=verdict,
        approved_quantity=allowed,
        checks=tuple(checks),
        reasons=(),
        snapshot_version=m.snapshot_version,
        **extra,
    )


# -- explicitly enabled, non-executable stress scenarios ---------------------------------------


class StressOutcome(DomainModel):
    shock: Dec
    orderly_loss: Dec  # isolated margin: loss capped at posted margin
    gap_loss: Dec  # gap / failed liquidation: full loss incl. fees and slippage
    gap_bad_debt: Dec  # gap loss beyond the posted margin


class StressReport(DomainModel):
    bot_id: str
    instrument_id: InstrumentId
    hypothetical: bool = True
    executable: bool = False
    leverage: SimulatedLeverage
    margin_quote: Dec
    maintenance_margin: SourcedRate
    liquidates_immediately: bool
    liquidation_price: Dec | None
    liquidation_distance: Dec | None
    outcomes: tuple[StressOutcome, ...]


def stress_scenario(
    p: TradeProposal, inst: Instrument, m: MarketInputs, cfg: PortfolioConfig, *, enabled: bool
) -> StressReport:
    if not enabled:
        raise PermissionError("stress scenarios must be explicitly enabled")
    bot = cfg.bot(p.bot_id)
    if bot.stress_max_leverage is None:
        raise PermissionError(f"{bot.bot_id} has no stress cap")
    if inst.id.venue is not bot.venue:
        raise PermissionError(f"{inst.id} is not on {bot.bot_id}'s venue {bot.venue.value}")
    try:
        lev = simulated_leverage(_sim_profile(bot, bot.stress_max_leverage), inst, p.leverage)
    except SimulationNotPermittedError as exc:
        raise PermissionError(str(exc)) from exc
    mmr = maintenance_margin_rate(inst, cfg.margin)
    fee = _fee(inst, cfg)
    terms = PositionTerms(
        p.side,
        _entry(p, m),
        p.quantity,
        inst.contract_size,
        p.leverage,
        mmr.value,
        fee,
        _funding_cost(p, m),
    )
    slip = max(m.slippage, cfg.fees.min_slippage.value)
    try:
        liq, dist, immediate = liquidation_price(terms), liquidation_distance(terms, m.mark), False
    except MarginModelUnavailableError:
        liq, dist, immediate = None, None, True
    outcomes = []
    for shock in cfg.limits.stress_shocks:
        if immediate:
            adverse = m.mark * (1 - shock) if p.side is Side.BUY else m.mark * (1 + shock)
            size = terms.quantity * terms.contract_size
            move = terms.entry - adverse if p.side is Side.BUY else adverse - terms.entry
            gap_loss = (
                move * size + adverse * size * (fee + slip) + terms.funding_cost * terms.notional
            )
            orderly = terms.margin
        else:
            orderly = shock_outcome(terms, m.mark, shock, slip, gap=False).loss
            gap_loss = shock_outcome(terms, m.mark, shock, slip, gap=True).loss
        outcomes.append(
            StressOutcome(
                shock=shock,
                orderly_loss=min(orderly, terms.margin),
                gap_loss=gap_loss,
                gap_bad_debt=max(Decimal(0), gap_loss - terms.margin),
            )
        )
    return StressReport(
        bot_id=bot.bot_id,
        instrument_id=inst.id,
        leverage=lev,
        margin_quote=terms.margin,
        maintenance_margin=mmr,
        liquidates_immediately=immediate,
        liquidation_price=liq,
        liquidation_distance=dist,
        outcomes=tuple(outcomes),
    )
