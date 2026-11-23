"""Optional portfolio liquidity reserve (R2): disabled by default, never changes allocations."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.risk.capital import CapitalManager, InsufficientCapitalError
from shadow_trading_suite.risk.config import PortfolioConfig
from shadow_trading_suite.storage.ledger import LedgerStore
from tests.support.portfolio import sample_portfolio_config

D = Decimal
BASE = sample_portfolio_config()
PROPOSED = {
    "bot-01": D(675),
    "bot-02": D(1125),
    "bot-03": D(1350),
    "bot-04": D(1350),
}  # pro rata 90%


def with_reserve(allocations: dict[str, Decimal] | None = None) -> PortfolioConfig:
    data = BASE.model_dump()
    data["liquidity_reserve"] = {"enabled": True, "fraction": D("0.10")}
    if allocations:
        data["bots"] = [b | {"allocation_eur": allocations[b["bot_id"]]} for b in data["bots"]]
    return PortfolioConfig.model_validate(data)


def test_disabled_by_default_and_m5_allocations_unchanged() -> None:
    assert not BASE.liquidity_reserve.enabled
    assert BASE.treasury_floor_eur() == 0
    assert [b.allocation_eur for b in BASE.bots] == [D(750), D(1250), D(1500), D(1500)]


def test_enabling_with_current_allocations_is_refused_not_silently_adjusted() -> None:
    with pytest.raises(ValidationError, match="4500") as exc:
        with_reserve()
    assert "not changed automatically" in str(exc.value)


def test_documented_allocation_change_validates() -> None:
    cfg = with_reserve(PROPOSED)
    assert cfg.treasury_floor_eur() == D(500)
    assert sum(b.allocation_eur for b in cfg.bots) == D(4500)
    assert cfg.limits == BASE.limits  # M5 limits untouched
    assert [b.max_leverage for b in cfg.bots] == [b.max_leverage for b in BASE.bots]


def test_capital_manager_keeps_the_floor_in_treasury(tmp_path: Path) -> None:
    cfg = with_reserve(PROPOSED)
    clock = ManualClock(datetime(2026, 10, 19, tzinfo=UTC))
    store = LedgerStore.open(tmp_path / "d", clock=clock)
    try:
        capital = CapitalManager(
            store,
            clock,
            cfg.fees.conversion_cost.value,
            treasury_floor_eur=cfg.treasury_floor_eur(),
        )
        capital.deposit_paper_capital("seed", cfg.starting_capital_eur)
        for b in cfg.bots:
            capital.allocate(f"a:{b.bot_id}", b.bot_id, b.venue, "USD", b.allocation_eur, D("0.86"))
        assert capital.treasury_eur == D(500)
        with pytest.raises(InsufficientCapitalError, match="floor"):
            capital.allocate("extra", "bot-01", BASE.bots[0].venue, "USD", D(1), D("0.86"))
    finally:
        store.close()
