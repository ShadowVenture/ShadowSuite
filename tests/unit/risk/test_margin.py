"""Deterministic margin, liquidation, volatility buffer and shock modelling."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shadow_trading_suite.core.instrument import Instrument
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.exchanges.coinbase.adapter import parse_product
from shadow_trading_suite.exchanges.cryptocom.adapter import parse_instrument
from shadow_trading_suite.risk.config import ParameterSource
from shadow_trading_suite.risk.margin import (
    MarginModelUnavailableError,
    PositionTerms,
    horizon_volatility,
    liquidation_distance,
    liquidation_price,
    maintenance_margin_rate,
    required_liquidation_buffer,
    shock_outcome,
)
from tests.support.portfolio import sample_portfolio_config
from tests.unit.exchanges.fakes import fixture_json

CFG = sample_portfolio_config()
D = Decimal


def cc(symbol: str) -> Instrument:
    rows = fixture_json("cryptocom/get_instruments.json")["result"]["data"]
    return parse_instrument(next(r for r in rows if r["symbol"] == symbol))


def cb(name: str, symbol: str) -> Instrument:
    rows = fixture_json(f"coinbase/{name}.json")["products"]
    return parse_product(next(r for r in rows if r["product_id"] == symbol))


def terms(
    side: Side = Side.BUY,
    lev: str = "10",
    mmr: str = "0.005",
    fee: str = "0.0006",
    funding: str = "0",
) -> PositionTerms:
    return PositionTerms(
        side=side,
        entry=D(100),
        quantity=D(1),
        contract_size=D(1),
        leverage=D(lev),
        mmr=D(mmr),
        fee_rate=D(fee),
        funding_cost=D(funding),
    )


class TestMaintenanceMargin:
    def test_cryptocom_major_and_other_are_assumptions(self) -> None:
        btc = maintenance_margin_rate(cc("BTCUSD-PERP"), CFG.margin)
        assert (btc.value, btc.source) == (D("0.005"), ParameterSource.ASSUMPTION)
        assert maintenance_margin_rate(cc("BZUSD-PERP"), CFG.margin).value == D("0.01")

    def test_fcm_derived_from_documented_initial_margin(self) -> None:
        bip = maintenance_margin_rate(cb("products_expiring", "BIP-20DEC30-CDE"), CFG.margin)
        # worst published initial margin (overnight short 0.256625) x assumed 0.80
        assert bip.value == D("0.256625") * D("0.80")
        assert bip.source is ParameterSource.ASSUMPTION
        assert "documented" in bip.note

    def test_intx_cannot_be_modelled(self) -> None:
        with pytest.raises(MarginModelUnavailableError, match="INTX"):
            maintenance_margin_rate(cb("products_perpetual", "BTC-PERP-INTX"), CFG.margin)

    def test_spot_has_no_margin(self) -> None:
        with pytest.raises(MarginModelUnavailableError, match="spot"):
            maintenance_margin_rate(cc("BTC_USD"), CFG.margin)


class TestLiquidation:
    def test_long_and_short_closed_form(self) -> None:
        long_liq = liquidation_price(terms())
        short_liq = liquidation_price(terms(side=Side.SELL))
        assert long_liq == (D(100) * (1 - D("0.1"))) / (1 - D("0.0006") - D("0.005"))
        assert short_liq == (D(100) * (1 + D("0.1"))) / (1 + D("0.0006") + D("0.005"))

    def test_100x_leaves_under_half_a_percent(self) -> None:
        dist = liquidation_distance(terms(lev="100"), mark=D(100))
        assert D("0.004") < dist < D("0.005")

    def test_funding_cost_brings_liquidation_closer(self) -> None:
        assert liquidation_distance(terms(funding="0.001"), D(100)) < liquidation_distance(
            terms(), D(100)
        )

    def test_leverage_at_or_beyond_maintenance_is_immediate_liquidation(self) -> None:
        # FCM-like 20% maintenance at 5x (20% initial): no room at all
        with pytest.raises(MarginModelUnavailableError, match="immediately"):
            liquidation_price(terms(lev="5", mmr="0.2053"))

    @given(
        lev=st.decimals(min_value=1, max_value=100, places=2),
        mmr=st.decimals(min_value="0.001", max_value="0.009", places=4),
    )
    def test_liquidation_is_on_the_losing_side(self, lev: Decimal, mmr: Decimal) -> None:
        long_t, short_t = terms(lev=str(lev), mmr=str(mmr)), terms(Side.SELL, str(lev), str(mmr))
        assert liquidation_price(long_t) < D(100) < liquidation_price(short_t)

    @given(a=st.decimals(min_value=1, max_value=99, places=1))
    def test_more_leverage_is_never_safer(self, a: Decimal) -> None:
        lo, hi = terms(lev=str(a)), terms(lev=str(a + 1))
        assert liquidation_distance(hi, D(100)) < liquidation_distance(lo, D(100))


class TestVolatilityBuffer:
    def test_horizon_volatility_scales_with_sqrt_time(self) -> None:
        closes = [D(100), D(101), D(100), D(101), D(100)]  # alternating ±~1% per minute
        sigma_1m = horizon_volatility(closes, candle_minutes=1, horizon_minutes=1)
        sigma_1h = horizon_volatility(closes, candle_minutes=1, horizon_minutes=60)
        assert sigma_1h == pytest.approx(sigma_1m * D(60).sqrt(), rel=D("1e-12"))

    def test_too_few_candles_fails_closed(self) -> None:
        with pytest.raises(MarginModelUnavailableError, match="volatility"):
            horizon_volatility([D(100)], 1, 60)

    def test_buffer_includes_fees_spread_slippage_and_volatility(self) -> None:
        buffer = required_liquidation_buffer(
            minimum=D("0.003"),
            sigma=D("0.004"),
            multiplier=D(3),
            fee_rate=D("0.0006"),
            spread=D("0.0002"),
            slippage=D("0.0002"),
        )
        assert buffer == D("0.012") + D("0.0012") + D("0.0002") + D("0.0002")

    def test_buffer_never_below_profile_minimum(self) -> None:
        assert required_liquidation_buffer(D("0.06"), D(0), D(3), D(0), D(0), D(0)) == D("0.06")


class TestShocks:
    def test_orderly_liquidation_loses_at_most_posted_margin(self) -> None:
        outcome = shock_outcome(
            terms(lev="10"), mark=D(100), shock=D("0.20"), slippage=D("0.001"), gap=False
        )
        assert outcome.liquidated
        assert outcome.loss == outcome.margin == D(10)
        assert outcome.bad_debt == 0

    def test_gap_liquidation_can_exceed_posted_margin(self) -> None:
        outcome = shock_outcome(
            terms(lev="10"), mark=D(100), shock=D("0.20"), slippage=D("0.01"), gap=True
        )
        assert outcome.liquidated
        assert outcome.loss > outcome.margin
        assert outcome.bad_debt == outcome.loss - outcome.margin

    def test_small_shock_is_mark_to_market_plus_exit_costs(self) -> None:
        outcome = shock_outcome(
            terms(lev="10"), mark=D(100), shock=D("0.01"), slippage=D("0.001"), gap=False
        )
        assert not outcome.liquidated
        # 1.00 price loss + exit fee on 99 + slippage on 99
        assert outcome.loss == D(1) + D(99) * D("0.0006") + D(99) * D("0.001")

    def test_short_loses_on_upward_shock(self) -> None:
        outcome = shock_outcome(
            terms(Side.SELL), mark=D(100), shock=D("0.01"), slippage=D(0), gap=False
        )
        assert outcome.loss > D(1)

    def test_results_are_exact_decimals(self) -> None:
        outcome = shock_outcome(terms(), D(100), D("0.05"), D("0.001"), gap=True)
        assert isinstance(outcome.loss, Decimal)
        again = shock_outcome(terms(), D(100), D("0.05"), D("0.001"), gap=True)
        assert again == outcome  # deterministic: pure function, no clock or randomness
