"""Valuation: ledger vs economic equity (R1), unrealised PnL, EUR conversion, fail closed."""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.accounting.reporting import portfolio_risk_equity, value_bot
from shadow_trading_suite.accounting.trading import LiquidationExecution, LiquidationTrigger
from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.orders import Liquidity, Side
from tests.unit.accounting.test_trading import BOT, NOW, PERP, RATE, SPOT, START_CASH, VENUE, Env

D = Decimal
RATES = {"USD": RATE}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.store.close()


def liquidate_with_bad_debt(env: Env) -> None:
    env.order("l1", PERP, Side.BUY, "0.01", "82000", fee="0.0006", lev="20")
    env.fill("f1", "l1", "82000", "0.01", "0.0006")
    env.ledger.settle_liquidation(
        "liq:1",
        BOT,
        PERP.instrument_id,
        [LiquidationExecution(price=D(77500), quantity=D("0.01"))],
        D("0.005"),
        LiquidationTrigger(
            mark=D(78300), maintenance_requirement=D("3.915"), position_equity=D(-2), ts=NOW
        ),
    )


def test_economic_loss_includes_simulated_insurance_absorption(env: Env) -> None:
    liquidate_with_bad_debt(env)
    v = value_bot(env.ledger, BOT, VENUE, {}, RATES)
    usd = v.currency("USD")
    assert usd.ledger_equity == START_CASH - D("41.492")
    assert usd.insurance_absorbed == D("7.875")
    assert usd.economic_equity == START_CASH - D("49.367")
    assert v.economic_equity_eur == usd.economic_equity * RATE
    assert v.ledger_equity_eur - v.economic_equity_eur == D("7.875") * RATE
    assert (usd.realized_price_pnl, usd.liquidation_fees, usd.trading_fees) == (
        D(-45),
        D("3.875"),
        D("0.492"),
    )


def test_derivative_unrealised_at_mark(env: Env) -> None:
    env.order("l1", PERP, Side.BUY, "0.01", "82000", fee="0.0006", lev="5")
    env.fill("f1", "l1", "82000", "0.01", "0.0006")
    usd = value_bot(env.ledger, BOT, VENUE, {PERP.instrument_id: D(83000)}, RATES).currency("USD")
    assert usd.unrealized_pnl == D(10)
    assert usd.margin == D(164)
    assert usd.ledger_equity == START_CASH - D("0.492") + D(10)


def test_spot_inventory_and_unrealised_at_mark(env: Env) -> None:
    env.order("b1", SPOT, Side.BUY, "0.005", "82000")
    env.fill("f1", "b1", "82000", "0.005", "0.004", Liquidity.MAKER)
    usd = value_bot(env.ledger, BOT, VENUE, {SPOT.instrument_id: D(84000)}, RATES).currency("USD")
    assert usd.inventory_value == D(420)
    assert usd.unrealized_pnl == D(10)
    assert usd.ledger_equity == START_CASH - D("411.64") + D(420)


def test_missing_mark_or_rate_fails_closed(env: Env) -> None:
    env.order("l1", PERP, Side.BUY, "0.01", "82000", fee="0.0006", lev="5")
    env.fill("f1", "l1", "82000", "0.01", "0.0006")
    with pytest.raises(DataQualityError, match="mark"):
        value_bot(env.ledger, BOT, VENUE, {}, RATES)
    with pytest.raises(DataQualityError, match="rate"):
        value_bot(env.ledger, BOT, VENUE, {PERP.instrument_id: D(83000)}, {})


def test_portfolio_risk_equity_uses_economic_values(env: Env) -> None:
    liquidate_with_bad_debt(env)
    eq = portfolio_risk_equity(
        env.ledger, [(BOT, VENUE)], {}, RATES, start_of_day_eur=D(5000), peak_eur=D(5000)
    )
    bot = value_bot(env.ledger, BOT, VENUE, {}, RATES)
    assert eq.equity_eur == env.ledger.treasury_eur + bot.economic_equity_eur
    assert eq.equity_eur < env.ledger.treasury_eur + bot.ledger_equity_eur
