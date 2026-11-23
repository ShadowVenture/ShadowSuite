"""R1 risk-limit enforcement: M5 engine and supervisor limits act on ECONOMIC equity derived
from the ledger (simulated insurance absorption counts as a loss)."""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.accounting.reporting import (
    bot_risk_state,
    portfolio_risk_equity,
    value_bot,
)
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.risk.engine import RiskVerdict, evaluate_bot
from shadow_trading_suite.risk.supervisor import PortfolioEquity, PortfolioState, review_portfolio
from tests.unit.accounting.test_reporting import RATES, liquidate_with_bad_debt
from tests.unit.accounting.test_trading import BOT, PERP, VENUE, Env
from tests.unit.risk.test_engine import CFG, market, proposal
from tests.unit.risk.test_engine import PERP as ENGINE_PERP
from tests.unit.risk.test_supervisor_recovery import request

D = Decimal


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.store.close()


def test_bot_state_is_derived_from_the_ledger_with_economic_equity(env: Env) -> None:
    env.order("l1", PERP, Side.BUY, "0.01", "82000", fee="0.0006", lev="5")
    env.fill("f1", "l1", "82000", "0.01", "0.0006")
    marks = {PERP.instrument_id: D(83000)}
    state = bot_risk_state(
        env.ledger,
        BOT,
        VENUE,
        marks,
        RATES,
        start_of_day_eur=D(1500),
        peak_eur=D(1500),
        orders_last_minute=2,
        halted=False,
    )
    valuation = value_bot(env.ledger, BOT, VENUE, marks, RATES)
    assert state.equity_eur == valuation.economic_equity_eur
    (pos,) = state.open_positions
    assert (pos.side, pos.quantity, pos.underlying) == (Side.BUY, D("0.01"), "BTC")
    assert pos.notional_eur == D("830") * RATES["USD"]
    assert pos.margin_eur == D(164) * RATES["USD"]
    assert pos.leverage == D(830) / D(164)


def test_engine_daily_loss_limit_counts_simulated_insurance_absorption(env: Env) -> None:
    liquidate_with_bad_debt(env)
    v = value_bot(env.ledger, BOT, VENUE, {}, RATES)
    limit = CFG.bot(BOT).daily_loss_limit
    start = v.economic_equity_eur / (1 - limit) + D("0.01")  # economic loss > limit > ledger loss
    kwargs = {
        "start_of_day_eur": start,
        "peak_eur": start,
        "orders_last_minute": 0,
        "halted": False,
    }
    economic = bot_risk_state(env.ledger, BOT, VENUE, {}, RATES, **kwargs)  # type: ignore[arg-type]
    ledger_view = economic.model_copy(update={"equity_eur": v.ledger_equity_eur})
    p, m = proposal(), market(ENGINE_PERP)
    assert evaluate_bot(p, ENGINE_PERP, ledger_view, m, CFG).verdict is not RiskVerdict.REJECT
    decision = evaluate_bot(p, ENGINE_PERP, economic, m, CFG)
    assert decision.verdict is RiskVerdict.REJECT
    assert any(r.startswith("daily_loss") for r in decision.reasons)


def test_supervisor_emergency_uses_economic_portfolio_loss(env: Env) -> None:
    liquidate_with_bad_debt(env)
    economic = portfolio_risk_equity(
        env.ledger, [(BOT, VENUE)], {}, RATES, start_of_day_eur=D(1), peak_eur=D(1)
    )
    ledger_total = (
        env.ledger.treasury_eur + value_bot(env.ledger, BOT, VENUE, {}, RATES).ledger_equity_eur
    )
    start = economic.equity_eur / (1 - CFG.limits.emergency_daily_loss) + D("0.01")
    req = request("r1", "bot-03", Side.BUY)

    def trigger(equity: Decimal) -> str | None:
        eq = PortfolioEquity(equity_eur=equity, start_of_day_eur=start, peak_eur=start)
        return review_portfolio(req, PortfolioState(equity=eq, emergency=False), 0, CFG)[2]

    assert trigger(ledger_total) is None
    assert trigger(economic.equity_eur) is not None
