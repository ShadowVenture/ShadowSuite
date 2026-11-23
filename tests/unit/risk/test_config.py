"""M5 configuration: approved defaults, validation, and explicit assumption labelling."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.risk.config import (
    BotRiskProfile,
    ParameterSource,
    PortfolioConfig,
    default_portfolio_config,
    load_portfolio_config,
)
from tests.support.portfolio import sample_portfolio_config

CONFIG = sample_portfolio_config()
# the historical, disabled table (still accepted from old files; never read)
LEGACY_RESERVATION = {
    "enabled": False,
    "threshold_eur": Decimal(3000),
    "reserve_eur": Decimal(700),
    "max_per_day": 1,
    "timezone": "UTC",
    "settlement": "completed_day_at_0000_utc",
}


class TestApprovedDefaults:
    def test_four_bots_two_per_venue_with_approved_allocations(self) -> None:
        bots = {b.bot_id: b for b in CONFIG.bots}
        assert {k: (b.venue, b.allocation_eur) for k, b in bots.items()} == {
            "bot-01": (Venue.COINBASE, Decimal(750)),
            "bot-02": (Venue.COINBASE, Decimal(1250)),
            "bot-03": (Venue.CRYPTOCOM, Decimal(1500)),
            "bot-04": (Venue.CRYPTOCOM, Decimal(1500)),
        }
        assert CONFIG.starting_capital_eur == Decimal(5000)

    def test_display_names(self) -> None:
        assert [b.display_name for b in CONFIG.bots] == ["BOT-01", "BOT-02", "BOT-03", "BOT-04"]

    def test_cb01_100x_is_stress_only(self) -> None:
        bot01 = CONFIG.bot("bot-01")
        assert bot01.stress_max_leverage == Decimal(100)
        assert bot01.max_leverage == Decimal(20)  # ordinary paper cap (assumption, reported)
        assert all(b.stress_max_leverage is None for b in CONFIG.bots if b.bot_id != "bot-01")

    @pytest.mark.parametrize(
        ("bot", "lev", "liq", "daily", "dd"),
        [
            ("bot-01", "20", "0.003", "0.10", "0.25"),
            ("bot-02", "20", "0.03", "0.07", "0.20"),
            ("bot-03", "10", "0.06", "0.05", "0.15"),
            ("bot-04", "5", "0.12", "0.04", "0.12"),
        ],
    )
    def test_per_bot_limits(self, bot: str, lev: str, liq: str, daily: str, dd: str) -> None:
        p = CONFIG.bot(bot)
        assert (p.max_leverage, p.min_liquidation_distance, p.daily_loss_limit, p.max_drawdown) == (
            Decimal(lev),
            Decimal(liq),
            Decimal(daily),
            Decimal(dd),
        )

    def test_portfolio_limits(self) -> None:
        lim = CONFIG.limits
        assert lim.max_gross_per_underlying == Decimal(4)
        assert lim.max_gross_per_venue == Decimal(5)
        assert lim.max_gross_total == Decimal(6)
        assert lim.max_margin_in_use == Decimal("0.60")
        assert lim.max_instrument_concentration == Decimal("0.40")
        assert lim.stress_shock == Decimal("0.05")
        assert lim.max_stress_loss == Decimal("0.15")
        assert (lim.emergency_daily_loss, lim.emergency_drawdown) == (
            Decimal("0.08"),
            Decimal("0.20"),
        )
        assert lim.reject_opposing_positions is True

    def test_profit_reservation_is_retired_and_absent_by_default(self) -> None:
        # no reservation table, threshold or amount in the default config
        assert CONFIG.profit_reservation is None
        dumped = str(CONFIG.model_dump())
        assert "threshold_eur" not in dumped
        assert "reserve_eur" not in dumped
        data = CONFIG.model_dump()
        data["profit_reservation"] = LEGACY_RESERVATION | {"enabled": True}
        with pytest.raises(ValidationError, match="not supported"):
            PortfolioConfig.model_validate(data)


class TestAssumptionLabelling:
    def test_undocumented_parameters_are_assumptions(self) -> None:
        m = CONFIG.margin
        assert m.cryptocom_major_mmr.source is ParameterSource.ASSUMPTION
        assert m.cryptocom_other_mmr.source is ParameterSource.ASSUMPTION
        assert m.fcm_maintenance_fraction.source is ParameterSource.ASSUMPTION
        for fee in (
            CONFIG.fees.spot_taker,
            CONFIG.fees.spot_maker,
            CONFIG.fees.derivatives_taker,
            CONFIG.fees.derivatives_maker,
            CONFIG.fees.conversion_cost,
        ):
            assert fee.source is ParameterSource.ASSUMPTION

    def test_conversion_cost_is_never_zero(self) -> None:
        assert CONFIG.fees.conversion_cost.value > 0


def _bots(**bot01: Any) -> list[dict[str, Any]]:
    base = [b.model_dump() for b in CONFIG.bots]
    base[0].update(bot01)
    return base


class TestValidation:
    def test_allocations_cannot_exceed_capital(self) -> None:
        with pytest.raises(ValidationError, match="allocat"):
            PortfolioConfig.model_validate(
                CONFIG.model_dump() | {"bots": _bots(allocation_eur=Decimal(751))}
            )

    def test_duplicate_bot_ids_rejected(self) -> None:
        bots = [b.model_dump() for b in CONFIG.bots]
        bots[1]["bot_id"] = "bot-01"
        with pytest.raises(ValidationError, match="duplicate"):
            PortfolioConfig.model_validate(CONFIG.model_dump() | {"bots": bots})

    @pytest.mark.parametrize(
        "bad", [{"max_leverage": Decimal(101)}, {"stress_max_leverage": Decimal(150)}]
    )
    def test_leverage_ceiling_100(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            BotRiskProfile.model_validate(CONFIG.bot("bot-01").model_dump() | bad)

    def test_stress_cap_must_exceed_ordinary_cap(self) -> None:
        with pytest.raises(ValidationError, match="stress"):
            BotRiskProfile.model_validate(
                CONFIG.bot("bot-01").model_dump() | {"stress_max_leverage": Decimal(10)}
            )

    def test_a_legacy_reservation_table_is_still_checked(self) -> None:
        data = CONFIG.model_dump()
        data["profit_reservation"] = LEGACY_RESERVATION | {"reserve_eur": Decimal(3001)}
        with pytest.raises(ValidationError):
            PortfolioConfig.model_validate(data)


class TestToml:
    def test_example_config_matches_defaults(self) -> None:
        example = Path(__file__).resolve().parents[3] / "config" / "portfolio.example.toml"
        assert load_portfolio_config(example) == default_portfolio_config()

    def test_float_money_in_toml_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "p.toml"
        path.write_text("starting_capital_eur = 5000.0\n")
        with pytest.raises(ValidationError, match="float"):
            load_portfolio_config(path)

    def test_no_withdrawal_or_live_settings_accepted(self, tmp_path: Path) -> None:
        example = Path(__file__).resolve().parents[3] / "config" / "portfolio.example.toml"
        text = example.read_text() + "\nreal_withdrawals_enabled = false\n"
        path = tmp_path / "p.toml"
        path.write_text(text)
        with pytest.raises(ValidationError, match="real_withdrawals_enabled"):
            load_portfolio_config(path)
