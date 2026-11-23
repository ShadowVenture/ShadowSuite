"""Per-bot risk engine: pure, fail-closed, deterministic decisions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from shadow_trading_suite.core.instrument import Instrument
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.exchanges.coinbase.adapter import parse_product
from shadow_trading_suite.exchanges.cryptocom.adapter import parse_instrument
from shadow_trading_suite.risk.engine import (
    BotState,
    MarketInputs,
    OpenPosition,
    RiskVerdict,
    TradeProposal,
    evaluate_bot,
    stress_scenario,
)
from tests.support.portfolio import sample_portfolio_config
from tests.unit.exchanges.fakes import fixture_json

CFG = sample_portfolio_config()
D = Decimal
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def cc(symbol: str) -> Instrument:
    rows = fixture_json("cryptocom/get_instruments.json")["result"]["data"]
    return parse_instrument(next(r for r in rows if r["symbol"] == symbol))


def cb(name: str, symbol: str) -> Instrument:
    return parse_product(
        next(
            r
            for r in fixture_json(f"coinbase/{name}.json")["products"]
            if r["product_id"] == symbol
        )
    )


PERP = cc("BTCUSD-PERP")
CC_SPOT = cc("BTC_USD")
FCM = cb("products_expiring", "BIP-20DEC30-CDE")
CB_SPOT = cb("products_spot", "BTC-USD")


def market(inst: Instrument, **kw: Any) -> MarketInputs:
    fields: dict[str, Any] = {
        "instrument_id": inst.id,
        "snapshot_version": 7,
        "as_of": NOW,
        "bid": D("81999"),
        "ask": D("82001"),
        "mark": D(82000),
        "mark_source": "venue_mark",
        "funding_rate": D("0.00001"),
        "funding_interval_s": 3600,
        "sigma": D("0.002"),
        "slippage": D("0.0002"),
        "eur_per_quote": D("0.86"),
    }
    fields.update(kw)
    return MarketInputs.model_validate(fields)


def state(bot: str = "bot-03", **kw: Any) -> BotState:
    eq = CFG.bot(bot).allocation_eur
    fields: dict[str, Any] = {
        "bot_id": bot,
        "equity_eur": eq,
        "start_of_day_equity_eur": eq,
        "peak_equity_eur": eq,
        "open_positions": (),
        "orders_last_minute": 0,
        "halted": False,
    }
    fields.update(kw)
    return BotState.model_validate(fields)


def proposal(inst: Instrument = PERP, bot: str = "bot-03", **kw: Any) -> TradeProposal:
    fields: dict[str, Any] = {
        "request_id": "r1",
        "bot_id": bot,
        "instrument_id": inst.id,
        "side": Side.BUY,
        "quantity": D("0.01"),
        "leverage": D(5),
        "created_at": NOW,
    }
    fields.update(kw)
    return TradeProposal.model_validate(fields)


def decide(
    p: TradeProposal,
    inst: Instrument = PERP,
    st: BotState | None = None,
    m: MarketInputs | None = None,
) -> Any:
    return evaluate_bot(p, inst, st or state(p.bot_id), m or market(inst), CFG)


def failed(decision: Any) -> set[str]:
    return {c.name for c in decision.checks if not c.passed}


class TestApprovals:
    def test_reasonable_perp_trade_approved_with_full_check_trail(self) -> None:
        d = decide(proposal())
        assert d.verdict is RiskVerdict.APPROVE, d.reasons
        assert d.margin_required_quote == D("0.01") * D(82001) * (1 + D("0.0002")) / 5
        assert d.liquidation_distance is not None
        assert d.liquidation_distance > d.required_buffer
        assert d.snapshot_version == 7
        assert {c.name for c in d.checks} >= {
            "halt",
            "permission",
            "leverage",
            "margin",
            "liquidation",
        }

    def test_spot_trade_unleveraged(self) -> None:
        d = decide(proposal(CC_SPOT, leverage=D(1), quantity=D("0.001")), CC_SPOT)
        assert d.verdict is RiskVerdict.APPROVE
        assert d.liquidation_distance is None

    def test_decisions_are_deterministic(self) -> None:
        assert decide(proposal()) == decide(proposal())


class TestRejections:
    def test_spot_with_leverage_rejected(self) -> None:
        assert "leverage" in failed(decide(proposal(CC_SPOT, leverage=D(2)), CC_SPOT))

    def test_ordinary_leverage_cap(self) -> None:
        assert "leverage" in failed(decide(proposal(leverage=D(11))))  # bot-03 cap 10x

    def test_cb01_100x_never_in_ordinary_paper_evaluation(self) -> None:
        d = decide(proposal(FCM, "bot-01", leverage=D(100)), FCM, state("bot-01"), market(FCM))
        assert d.verdict is RiskVerdict.REJECT
        assert any("stress" in r for r in d.reasons)

    def test_wrong_venue_or_unpermitted_instrument(self) -> None:
        assert "permission" in failed(decide(proposal(CB_SPOT, "bot-03", leverage=D(1)), CB_SPOT))

    def test_fcm_leverage_infeasible_under_maintenance_assumption(self) -> None:
        # 1 contract = 0.01 BTC; sized fine, but ~20.5% maintenance vs 10% initial margin
        d = decide(
            proposal(FCM, "bot-02", leverage=D(10), quantity=D(1)),
            FCM,
            state("bot-02"),
            market(FCM),
        )
        assert d.verdict is RiskVerdict.REJECT
        assert "liquidation" in failed(d)

    def test_volatility_aware_buffer_rejects_tight_liquidation(self) -> None:
        # 10x leaves ~9.5%; 3 x 4% hourly sigma + costs requires ~12.1%
        d = decide(proposal(leverage=D(10)), m=market(PERP, sigma=D("0.04")))
        assert "liquidation" in failed(d)

    @pytest.mark.parametrize(
        ("kw", "check"),
        [
            ({"halted": True}, "halt"),
            ({"equity_eur": D(1420)}, "daily_loss"),  # 5.3% down vs 5% limit
            ({"peak_equity_eur": D(1800)}, "drawdown"),  # 16.7% below peak vs 15%
            ({"orders_last_minute": 10}, "order_rate"),
        ],
    )
    def test_bot_state_limits(self, kw: dict[str, Any], check: str) -> None:
        assert check in failed(decide(proposal(), st=state(**kw)))

    def test_max_open_positions_blocks_only_exposure_increases(self) -> None:
        """This limit in isolation (a mutation test once survived without it)."""
        limit = CFG.bot("bot-03").max_open_positions

        def pos(n: int) -> OpenPosition:
            return OpenPosition(
                position_id=f"p{n}",
                bot_id="bot-03",
                instrument_id=PERP.id,
                underlying="BTC",
                side=Side.BUY,
                quantity=D("0.001"),
                notional_eur=D(70),
                margin_eur=D(14),
                leverage=D(5),
            )

        below = state(open_positions=tuple(pos(n) for n in range(limit - 1)))
        assert "positions" not in failed(decide(proposal(), st=below))
        full = state(open_positions=tuple(pos(n) for n in range(limit)))
        assert "positions" in failed(decide(proposal(), st=full))

    def test_spot_leverage_is_refused_by_the_engine_check_itself(self) -> None:
        """The engine check fires before the `simulated_leverage` backstop (defence in depth)."""
        d = decide(proposal(CC_SPOT, leverage=D(2)), CC_SPOT)
        lev = next(c for c in d.checks if c.name == "leverage")
        assert not lev.passed
        assert lev.detail == "spot is unleveraged"

    def test_stale_or_mismatched_market_inputs_rejected(self) -> None:
        old = market(PERP, as_of=NOW - timedelta(seconds=30))
        assert "market_data" in failed(decide(proposal(), m=old))
        other = market(CC_SPOT)
        assert "market_data" in failed(decide(proposal(), m=other))

    def test_crossed_or_missing_quotes_rejected(self) -> None:
        with pytest.raises(ValueError, match="bid"):
            market(PERP, bid=D(82002))


class TestResize:
    def test_position_margin_cap_resizes_down_to_lot(self) -> None:
        d = decide(proposal(quantity=D(1)))  # far above 20% of equity as margin
        assert d.verdict is RiskVerdict.RESIZE
        assert d.approved_quantity is not None
        assert d.approved_quantity < D(1)
        assert d.approved_quantity % PERP.lot_size == 0
        margin_eur = d.approved_quantity * D(82001) * (1 + D("0.0002")) / 5 * D("0.86")
        assert margin_eur <= CFG.bot("bot-03").max_position_margin * D(1500)

    def test_resize_below_minimum_rejects(self) -> None:
        tiny_equity = state(
            equity_eur=D("0.5"), start_of_day_equity_eur=D("0.5"), peak_equity_eur=D("0.5")
        )
        assert decide(proposal(quantity=D(1)), st=tiny_equity).verdict is RiskVerdict.REJECT


class TestStressScenarios:
    def test_cb01_100x_only_through_explicit_stress_scenario(self) -> None:
        report = stress_scenario(
            proposal(FCM, "bot-01", leverage=D(100)), FCM, market(FCM), CFG, enabled=True
        )
        assert report.hypothetical is True
        assert report.executable is False
        assert report.leverage.value == D(100)
        assert report.leverage.beyond_venue_published is None  # FCM publishes no max leverage
        # ~20.5% assumed maintenance vs 1% initial margin: liquidated on the spot
        assert report.liquidates_immediately is True
        assert report.liquidation_distance is None
        assert [o.shock for o in report.outcomes] == list(CFG.limits.stress_shocks)
        assert all(o.orderly_loss == report.margin_quote for o in report.outcomes)
        assert all(o.gap_bad_debt > 0 for o in report.outcomes)  # losses beyond margin modelled

    def test_stress_requires_explicit_enablement_cap_and_venue(self) -> None:
        p = proposal(FCM, "bot-01", leverage=D(100))
        with pytest.raises(PermissionError, match="enabled"):
            stress_scenario(p, FCM, market(FCM), CFG, enabled=False)
        with pytest.raises(PermissionError, match="stress cap"):
            stress_scenario(
                proposal(PERP, "bot-03", leverage=D(50)), PERP, market(PERP), CFG, enabled=True
            )
        with pytest.raises(PermissionError, match="venue"):
            stress_scenario(
                proposal(PERP, "bot-01", leverage=D(100)), PERP, market(PERP), CFG, enabled=True
            )
