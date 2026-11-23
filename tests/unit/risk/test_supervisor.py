"""Portfolio supervisor: conflicts, aggregates, stress, emergency stop, races, audit, replay."""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from shadow_trading_suite.core.instrument import Instrument, Venue
from shadow_trading_suite.core.market import BookLevel, FundingRate, MarkPrice, OrderBookSnapshot
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.exchanges.cryptocom.adapter import parse_instrument
from shadow_trading_suite.risk.capital import CapitalManager
from shadow_trading_suite.risk.engine import BotState, RiskVerdict, TradeProposal
from shadow_trading_suite.risk.pipeline import RiskPipeline, replay_audit
from shadow_trading_suite.risk.supervisor import PortfolioEquity, PortfolioSupervisor
from shadow_trading_suite.storage.ledger import LedgerStore
from shadow_trading_suite.streaming.events import Payload, StreamEvent, StreamKind
from shadow_trading_suite.streaming.state import MarketState, StreamKey, StreamStatus
from tests.support.portfolio import sample_portfolio_config
from tests.unit.exchanges.fakes import fixture_json

D = Decimal
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
CFG = sample_portfolio_config()
RATE = D("0.86")
ROWS = fixture_json("cryptocom/get_instruments.json")["result"]["data"]
BTC = parse_instrument(next(r for r in ROWS if r["symbol"] == "BTCUSD-PERP"))
ETH = parse_instrument(next(r for r in ROWS if r["symbol"] == "ETHUSD-PERP"))


def put(state: MarketState, inst: Instrument, kind: StreamKind, payload: Payload) -> None:
    state.set_status(StreamKey(inst.id, kind), StreamStatus.LIVE)
    state.apply(
        StreamEvent(
            event_id=f"{inst.id}:{kind}",
            kind=kind,
            instrument_id=inst.id,
            exchange_ts=NOW,
            received_at=NOW,
            epoch=1,
            payload=payload,
        )
    )


def lvl(price: Decimal) -> BookLevel:
    return BookLevel(price=price, quantity=D(100))


def market_state(clock: ManualClock) -> MarketState:
    state = MarketState(clock, stale_after={})
    for inst, px in ((BTC, D(82000)), (ETH, D(3000))):
        put(
            state,
            inst,
            StreamKind.BOOK,
            OrderBookSnapshot(
                instrument_id=inst.id, ts=NOW, bids=(lvl(px - 1),), asks=(lvl(px + 1),)
            ),
        )
        put(state, inst, StreamKind.MARK, MarkPrice(instrument_id=inst.id, ts=NOW, mark=px))
        put(
            state,
            inst,
            StreamKind.FUNDING,
            FundingRate(
                instrument_id=inst.id, ts=NOW, rate=D("0.00001"), interval=timedelta(hours=1)
            ),
        )
    return state


class Env:
    def __init__(self, tmp: Path, equity: PortfolioEquity | None = None) -> None:
        self.clock = ManualClock(NOW)
        self.store = LedgerStore.open(tmp / "data", clock=self.clock)
        self.capital = CapitalManager(self.store, self.clock, CFG.fees.conversion_cost.value)
        self.capital.deposit_paper_capital("seed", CFG.starting_capital_eur)
        for b in CFG.bots:
            self.capital.allocate(
                f"alloc:{b.bot_id}", b.bot_id, b.venue, "USD", b.allocation_eur, RATE
            )
        self.equity = equity or PortfolioEquity(
            equity_eur=D(5000), start_of_day_eur=D(5000), peak_eur=D(5000)
        )
        self.supervisor = PortfolioSupervisor(CFG, self.capital, self.store, lambda: self.equity)
        self.pipeline = RiskPipeline(CFG, self.supervisor, self.store)
        self.state = market_state(self.clock)

    def submit(
        self,
        bot: str,
        side: Side,
        inst: Instrument = BTC,
        qty: str = "0.002",
        lev: str = "5",
        rid: str | None = None,
        reduce_only: bool = False,
    ) -> Any:
        eq = CFG.bot(bot).allocation_eur
        proposal = TradeProposal(
            request_id=rid or f"{bot}-{side.value}-{inst.id.symbol}",
            bot_id=bot,
            instrument_id=inst.id,
            side=side,
            quantity=D(qty),
            leverage=D(lev),
            reduce_only=reduce_only,
            created_at=NOW,
        )
        bot_state = BotState(
            bot_id=bot,
            equity_eur=eq,
            start_of_day_equity_eur=eq,
            peak_equity_eur=eq,
            orders_last_minute=0,
            halted=False,
        )
        return self.pipeline.submit_from_snapshot(
            proposal, inst, bot_state, self.state.snapshot(), sigma=D("0.002"), eur_per_quote=RATE
        )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.store.close()


def approved(result: Any) -> bool:
    return result.supervisor is not None and result.supervisor.approved


class TestHappyPathAndCapital:
    def test_approved_exposure_commits_margin_atomically(self, env: Env) -> None:
        free_before = env.capital.account("bot-03", Venue.CRYPTOCOM, "USD").free
        result = env.submit("bot-03", Side.BUY)
        assert result.bot.verdict is RiskVerdict.APPROVE
        assert approved(result)
        assert env.capital.account("bot-03", Venue.CRYPTOCOM, "USD").free < free_before

    def test_same_request_is_idempotent(self, env: Env) -> None:
        first = env.submit("bot-03", Side.BUY, rid="dup")
        committed = env.capital.account("bot-03", Venue.CRYPTOCOM, "USD").committed
        second = env.submit("bot-03", Side.BUY, rid="dup")
        assert second.supervisor == first.supervisor
        assert env.capital.account("bot-03", Venue.CRYPTOCOM, "USD").committed == committed


class TestConflictsAndAggregates:
    def test_opposing_positions_on_same_underlying_rejected_across_bots(self, env: Env) -> None:
        assert approved(env.submit("bot-03", Side.BUY))
        result = env.submit("bot-04", Side.SELL)
        assert not approved(result)
        assert any("opposing" in r for r in result.supervisor.reasons)

    def test_same_direction_is_allowed_but_counts_toward_caps(self, env: Env) -> None:
        assert approved(env.submit("bot-03", Side.BUY))
        assert approved(env.submit("bot-04", Side.BUY))

    def test_caps_use_combined_exposure_across_bots(self, tmp_path: Path) -> None:
        small = Env(
            tmp_path, PortfolioEquity(equity_eur=D(300), start_of_day_eur=D(300), peak_eur=D(300))
        )
        try:
            first = small.submit("bot-03", Side.BUY, qty="0.003")  # ~212 EUR gross < equity
            assert approved(first)
            second = small.submit("bot-04", Side.BUY, qty="0.003", rid="second")  # same alone
            assert not approved(second)  # combined ~423 EUR breaches a portfolio-wide cap
            assert any(
                r.split(":")[0] in {"concentration", "underlying", "total", "stress", "margin"}
                for r in second.supervisor.reasons
            )
        finally:
            small.store.close()

    def test_stress_loss_uses_gross_exposure_with_gap_losses(self, env: Env) -> None:
        result = env.submit("bot-03", Side.BUY, qty="0.002")
        assert approved(result)
        stress = {c.name: c for c in result.supervisor_checks}
        assert "stress" in stress
        assert "gap" in stress["stress"].detail


class TestFailClosed:
    def test_stale_snapshot_rejected_and_audited(self, env: Env) -> None:
        env.clock.advance(timedelta(seconds=30))
        result = env.submit("bot-03", Side.BUY)
        assert result.bot.verdict is RiskVerdict.REJECT
        assert result.supervisor is None
        assert any("stale" in r for r in result.bot.reasons)
        assert env.store.audit_records()[-1].kind == "bot_decision"

    def test_capital_shortfall_turns_approval_into_rejection(self, env: Env) -> None:
        acct = env.capital.account("bot-03", Venue.CRYPTOCOM, "USD")
        env.capital.commit("hold", "bot-03", Venue.CRYPTOCOM, "USD", acct.free - D("0.01"))
        result = env.submit("bot-03", Side.BUY)
        assert not approved(result)
        assert any("capital" in r for r in result.supervisor.reasons)


class TestEmergencyShutdown:
    def test_portfolio_daily_loss_triggers_shutdown_for_every_bot(self, tmp_path: Path) -> None:
        losing = Env(
            tmp_path,
            PortfolioEquity(equity_eur=D(4590), start_of_day_eur=D(5000), peak_eur=D(5000)),
        )
        try:
            result = losing.submit("bot-03", Side.BUY)
            assert not approved(result)
            assert losing.supervisor.emergency_shutdown
            assert not approved(losing.submit("bot-04", Side.BUY, inst=ETH))
            kinds = [r.kind for r in losing.store.audit_records()]
            assert "emergency_shutdown" in kinds
        finally:
            losing.store.close()

    def test_shutdown_requires_manual_reset(self, env: Env) -> None:
        env.supervisor.trigger_emergency("operator drill")
        assert not approved(env.submit("bot-03", Side.BUY))
        env.supervisor.reset_emergency("drill complete")
        assert approved(env.submit("bot-03", Side.BUY, rid="after-reset"))


class TestConcurrency:
    def test_racing_opposite_requests_never_both_approved(self, env: Env) -> None:
        results: dict[str, Any] = {}
        barrier = threading.Barrier(2)

        def go(bot: str, side: Side) -> None:
            barrier.wait()
            results[bot] = env.submit(bot, side, rid=f"race-{bot}")

        threads = [
            threading.Thread(target=go, args=a)
            for a in (("bot-03", Side.BUY), ("bot-04", Side.SELL))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(approved(r) for r in results.values()) == 1

    def test_racing_requests_never_overcommit_capital(self, env: Env) -> None:
        def go(i: int) -> None:
            for j in range(5):
                env.submit("bot-04", Side.BUY, qty="0.002", rid=f"cap-{i}-{j}")

        threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        acct = env.capital.account("bot-04", Venue.CRYPTOCOM, "USD")
        assert acct.committed <= acct.balance


class TestAuditAndReplay:
    def test_every_decision_audited_in_a_verified_hash_chain(self, env: Env) -> None:
        env.submit("bot-03", Side.BUY)
        env.submit("bot-04", Side.SELL)
        records = env.store.audit_records()
        assert {r.kind for r in records} >= {"bot_decision", "supervisor_decision"}
        assert env.store.verify_audit_chain() == len(records)

    def test_deterministic_replay_reproduces_every_decision(self, env: Env) -> None:
        env.submit("bot-03", Side.BUY)
        env.submit("bot-04", Side.SELL)
        env.submit("bot-03", Side.BUY, qty="5", rid="too-big")
        env.clock.advance(timedelta(seconds=30))
        env.submit("bot-04", Side.BUY, rid="stale")
        mismatches = replay_audit(env.store.audit_records(), CFG)
        assert mismatches == []

    def test_tampering_is_detected(self, env: Env) -> None:
        from shadow_trading_suite.storage.hardened import StorageError

        env.submit("bot-03", Side.BUY)
        env.store._con.execute("UPDATE risk_audit SET payload = '{}' WHERE seq = 1")
        with pytest.raises(StorageError, match="chain"):
            env.store.verify_audit_chain()
