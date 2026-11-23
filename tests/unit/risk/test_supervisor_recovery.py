"""Supervisor recovery (M6-A): state rebuilt from the audit trail, reconciled with capital."""

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.instruments.simulation import SimulatedLeverage
from shadow_trading_suite.risk.capital import CapitalManager
from shadow_trading_suite.risk.interfaces import ExposureRequest, ExposureTerms, StressTerms
from shadow_trading_suite.risk.supervisor import PortfolioEquity, PortfolioSupervisor
from shadow_trading_suite.storage.ledger import LedgerStore
from shadow_trading_suite.streaming.state import MarketState
from tests.support.portfolio import sample_portfolio_config

D = Decimal
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
CFG = sample_portfolio_config()
RATE = D("0.86")
BTC = InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTCUSD-PERP")
EQUITY = PortfolioEquity(equity_eur=D(5000), start_of_day_eur=D(5000), peak_eur=D(5000))


def request(rid: str, bot: str, side: Side, margin: str = "33.33333333333") -> ExposureRequest:
    stress = StressTerms(
        side=side,
        entry=D(82000),
        mark=D(82000),
        quantity=D("0.002"),
        contract_size=D(1),
        leverage=D(5),
        mmr=D("0.005"),
        fee_rate=D("0.0006"),
        funding_cost=D(0),
        eur_per_quote=RATE,
    )
    terms = ExposureTerms(
        underlying="BTC",
        venue=Venue.CRYPTOCOM,
        notional_eur=D(141),
        margin_eur=D("28.2"),
        margin_quote=D(margin),
        margin_currency="USD",
        stress=stress,
    )
    return ExposureRequest(
        request_id=rid,
        bot_id=bot,
        instrument_id=BTC,
        side=side,
        quantity=D("0.002"),
        reference_price=D(82000),
        simulated_leverage=SimulatedLeverage(
            value=D(5), venue_published_max=D(100), beyond_venue_published=False
        ),
        snapshot_version=0,
        terms=terms,
    )


class Env:
    def __init__(self, tmp: Path, fresh: bool = True) -> None:
        self.tmp = tmp
        self.store = LedgerStore.open(tmp / "data", clock=ManualClock(NOW))
        self.capital = CapitalManager(self.store, ManualClock(NOW), CFG.fees.conversion_cost.value)
        if fresh:
            self.capital.deposit_paper_capital("seed", CFG.starting_capital_eur)
            for b in CFG.bots:
                self.capital.allocate(
                    f"alloc:{b.bot_id}", b.bot_id, b.venue, "USD", b.allocation_eur, RATE
                )
        self.supervisor = PortfolioSupervisor(CFG, self.capital, self.store, lambda: EQUITY)
        self.snapshot = MarketState(ManualClock(NOW), stale_after={}).snapshot()

    def review(self, req: ExposureRequest) -> bool:
        return self.supervisor.review(req, self.snapshot).approved

    def restart(self) -> "Env":
        self.store.close()
        return Env(self.tmp, fresh=False)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.store.close()


def test_exposures_and_decisions_survive_restart(env: Env) -> None:
    assert env.review(request("r1", "bot-03", Side.BUY))
    assert env.review(request("r2", "bot-04", Side.BUY))
    env.supervisor.release("r2")
    before = env.supervisor.state().exposures
    committed = env.capital.account("bot-03", Venue.CRYPTOCOM, "USD").committed
    e2 = env.restart()
    try:
        assert e2.supervisor.state().exposures == before
        assert e2.supervisor.recovery_issues == ()
        assert not e2.supervisor.emergency_shutdown
        assert e2.review(request("r1", "bot-03", Side.BUY))  # idempotent: stored decision
        assert e2.capital.account("bot-03", Venue.CRYPTOCOM, "USD").committed == committed
        assert not e2.review(request("r3", "bot-04", Side.SELL))  # opposing restored exposure
    finally:
        e2.store.close()


@pytest.mark.parametrize("reset", [False, True])
def test_emergency_flag_survives_restart(env: Env, reset: bool) -> None:
    env.supervisor.trigger_emergency("drill")
    if reset:
        env.supervisor.reset_emergency("done")
    e2 = env.restart()
    try:
        assert e2.supervisor.emergency_shutdown is (not reset)
    finally:
        e2.store.close()


def test_reconciliation_failure_triggers_emergency(env: Env) -> None:
    assert env.review(request("r1", "bot-03", Side.BUY))
    env.capital.release("r1")  # capital changed behind the supervisor's back
    e2 = env.restart()
    try:
        assert e2.supervisor.emergency_shutdown
        assert any("r1" in issue for issue in e2.supervisor.recovery_issues)
        kinds = [r.kind for r in e2.store.audit_records()]
        assert "recovery_reconciliation_failed" in kinds
        assert "emergency_shutdown" in kinds
        assert e2.store.verify_audit_chain() == len(kinds)
    finally:
        e2.store.close()


@pytest.mark.parametrize(
    "fault", ["_insert_operation", "_insert_event", "_insert_audit", "_commit"]
)
def test_review_is_atomic_with_its_audit_record(
    env: Env, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    def boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError(f"crash in {fault}")

    monkeypatch.setattr(env.store, fault, boom)
    with pytest.raises(RuntimeError):
        env.review(request("r1", "bot-03", Side.BUY))
    monkeypatch.undo()
    assert env.supervisor.state().exposures == ()
    assert env.capital.hold("r1") is None
    e2 = env.restart()
    try:
        assert e2.capital.hold("r1") is None
        assert e2.supervisor.state().exposures == ()
        assert e2.review(request("r1", "bot-03", Side.BUY))  # can be retried cleanly
    finally:
        e2.store.close()


def test_release_is_atomic(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    assert env.review(request("r1", "bot-03", Side.BUY))

    def boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("crash")

    monkeypatch.setattr(env.store, "_insert_audit", boom)
    with pytest.raises(RuntimeError):
        env.supervisor.release("r1")
    monkeypatch.undo()
    assert len(env.supervisor.state().exposures) == 1
    hold = env.capital.hold("r1")
    assert hold is not None
    assert not hold.released


def test_capital_and_audit_must_share_one_store(env: Env, tmp_path: Path) -> None:
    other = LedgerStore.open(tmp_path / "other", clock=ManualClock(NOW))
    try:
        with pytest.raises(ValueError, match="same ledger store"):
            PortfolioSupervisor(CFG, env.capital, other, lambda: EQUITY)
    finally:
        other.close()
