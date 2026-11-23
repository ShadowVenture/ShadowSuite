"""Capital manager: atomic reservations, no double allocation, durable, replayable, race-free."""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.risk.capital import (
    AMOUNT_QUANTUM,
    CapitalError,
    CapitalManager,
    InsufficientCapitalError,
)
from shadow_trading_suite.storage.ledger import LedgerStore
from tests.support.portfolio import sample_portfolio_config
from tests.unit.accounting.legacy_ops import book_legacy_reserve

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
D = Decimal
EUR_PER_USD = D("0.86")  # recorded valuation rate (EUR per 1 USD)
CFG = sample_portfolio_config()
COST = CFG.fees.conversion_cost.value


@pytest.fixture
def ledger_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def open_manager(ledger_dir: Path) -> tuple[CapitalManager, LedgerStore]:
    store = LedgerStore.open(ledger_dir, clock=ManualClock(NOW))
    return CapitalManager(store, ManualClock(NOW), conversion_cost=COST), store


@pytest.fixture
def manager(ledger_dir: Path) -> Iterator[CapitalManager]:
    m, store = open_manager(ledger_dir)
    m.deposit_paper_capital("seed", D(5000))
    yield m
    store.close()


def allocate_all(m: CapitalManager) -> None:
    for bot in CFG.bots:
        m.allocate(
            f"alloc:{bot.bot_id}", bot.bot_id, bot.venue, "USD", bot.allocation_eur, EUR_PER_USD
        )


class TestAllocation:
    def test_allocation_converts_at_recorded_rate_with_explicit_cost(
        self, manager: CapitalManager
    ) -> None:
        manager.allocate("a1", "bot-03", Venue.CRYPTOCOM, "USD", D(1500), EUR_PER_USD)
        acct = manager.account("bot-03", Venue.CRYPTOCOM, "USD")
        expected = ((D(1500) * (1 - COST)) / EUR_PER_USD).quantize(AMOUNT_QUANTUM, ROUND_DOWN)
        assert acct.balance == expected  # credits rounded DOWN: never creates money
        assert manager.treasury_eur == D(3500)
        assert manager.conversion_costs_eur == D(1500) * COST

    def test_no_double_allocation(self, manager: CapitalManager) -> None:
        allocate_all(manager)
        assert manager.treasury_eur == 0
        with pytest.raises(InsufficientCapitalError):
            manager.allocate("extra", "bot-01", Venue.COINBASE, "USD", D(1), EUR_PER_USD)

    def test_same_request_is_idempotent(self, manager: CapitalManager) -> None:
        manager.allocate("a1", "bot-01", Venue.COINBASE, "USD", D(750), EUR_PER_USD)
        manager.allocate("a1", "bot-01", Venue.COINBASE, "USD", D(750), EUR_PER_USD)
        assert manager.treasury_eur == D(4250)

    def test_reused_id_with_different_content_refused(self, manager: CapitalManager) -> None:
        manager.allocate("a1", "bot-01", Venue.COINBASE, "USD", D(750), EUR_PER_USD)
        with pytest.raises(CapitalError, match="conflict"):
            manager.allocate("a1", "bot-01", Venue.COINBASE, "USD", D(700), EUR_PER_USD)


class TestCommitments:
    def test_commit_and_release(self, manager: CapitalManager) -> None:
        allocate_all(manager)
        acct = manager.account("bot-03", Venue.CRYPTOCOM, "USD")
        manager.commit("req-1", "bot-03", Venue.CRYPTOCOM, "USD", D(100))
        assert manager.account("bot-03", Venue.CRYPTOCOM, "USD").free == acct.free - 100
        manager.release("req-1")
        assert manager.account("bot-03", Venue.CRYPTOCOM, "USD").free == acct.free

    def test_cannot_commit_more_than_free(self, manager: CapitalManager) -> None:
        allocate_all(manager)
        free = manager.account("bot-01", Venue.COINBASE, "USD").free
        with pytest.raises(InsufficientCapitalError):
            manager.commit("big", "bot-01", Venue.COINBASE, "USD", free + D("0.01"))

    def test_bots_cannot_spend_each_others_capital(self, manager: CapitalManager) -> None:
        allocate_all(manager)
        with pytest.raises(InsufficientCapitalError):
            manager.commit("x", "bot-02", Venue.CRYPTOCOM, "USD", D(1))  # bot-02 has no CDC funds

    def test_release_unknown_or_twice_is_refused_or_idempotent(
        self, manager: CapitalManager
    ) -> None:
        allocate_all(manager)
        with pytest.raises(CapitalError, match="unknown"):
            manager.release("never-committed")
        manager.commit("c", "bot-04", Venue.CRYPTOCOM, "USD", D(10))
        manager.release("c")
        manager.release("c")  # idempotent
        assert manager.account("bot-04", Venue.CRYPTOCOM, "USD").committed == 0

    def test_concurrent_commits_never_overcommit(self, manager: CapitalManager) -> None:
        allocate_all(manager)
        free = manager.account("bot-01", Venue.COINBASE, "USD").free
        chunk = (free / 10).quantize(D("0.01"))
        errors: list[Exception] = []

        def worker(i: int) -> None:
            for j in range(3):
                try:
                    manager.commit(f"t{i}-{j}", "bot-01", Venue.COINBASE, "USD", chunk)
                except InsufficientCapitalError as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        acct = manager.account("bot-01", Venue.COINBASE, "USD")
        assert acct.committed <= acct.balance
        assert acct.committed == chunk * (24 - len(errors))
        assert len(errors) >= 14  # only 10 chunks fit


class TestRealisedResultsAndReserve:
    def test_realised_pnl_changes_balance(self, manager: CapitalManager) -> None:
        allocate_all(manager)
        before = manager.account("bot-03", Venue.CRYPTOCOM, "USD").balance
        manager.realize("pnl-1", "bot-03", Venue.CRYPTOCOM, "USD", D("-12.5"))
        assert manager.account("bot-03", Venue.CRYPTOCOM, "USD").balance == before - D("12.5")

    def test_new_profit_reserve_is_refused_as_unsupported(self, manager: CapitalManager) -> None:
        allocate_all(manager)
        digest = manager.state_digest()
        with pytest.raises(CapitalError, match="not supported"):
            manager.transfer_to_reserve(
                "pdm:2026-10-18:reserve", {("bot-03", Venue.CRYPTOCOM, "USD"): D(100)}, EUR_PER_USD
            )
        assert manager.reserve_eur == 0
        assert manager.state_digest() == digest

    def test_historical_reserve_is_excluded_from_trading_capital(
        self, manager: CapitalManager
    ) -> None:
        allocate_all(manager)
        book_legacy_reserve(
            manager,
            "pdm:2026-10-18:reserve",
            {("bot-03", Venue.CRYPTOCOM, "USD"): D(100)},
            EUR_PER_USD,
        )
        assert manager.reserve_eur == (D(100) * EUR_PER_USD * (1 - COST)).quantize(
            AMOUNT_QUANTUM, ROUND_DOWN
        )
        assert manager.treasury_eur == 0
        with pytest.raises(InsufficientCapitalError):
            manager.allocate("from-reserve", "bot-01", Venue.COINBASE, "USD", D(1), EUR_PER_USD)
        assert manager.verify_replay() == []

    def test_historical_reserve_could_not_use_committed_capital(
        self, manager: CapitalManager
    ) -> None:
        allocate_all(manager)
        acct = manager.account("bot-04", Venue.CRYPTOCOM, "USD")
        manager.commit("m", "bot-04", Venue.CRYPTOCOM, "USD", acct.free - D(5))
        with pytest.raises(InsufficientCapitalError):
            book_legacy_reserve(
                manager, "pdm:x", {("bot-04", Venue.CRYPTOCOM, "USD"): D(10)}, EUR_PER_USD
            )
        assert manager.reserve_eur == 0  # all-or-nothing


class TestDurability:
    def test_state_survives_restart_identically(self, ledger_dir: Path) -> None:
        m1, s1 = open_manager(ledger_dir)
        m1.deposit_paper_capital("seed", D(5000))
        allocate_all(m1)
        m1.commit("c1", "bot-02", Venue.COINBASE, "USD", D(50))
        digest = m1.state_digest()
        s1.close()
        m2, s2 = open_manager(ledger_dir)
        try:
            assert m2.state_digest() == digest
            assert m2.account("bot-02", Venue.COINBASE, "USD").committed == D(50)
        finally:
            s2.close()

    def test_crash_mid_operation_applies_nothing(
        self, ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m1, s1 = open_manager(ledger_dir)
        m1.deposit_paper_capital("seed", D(5000))
        before = m1.state_digest()

        def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("process killed mid-write")

        monkeypatch.setattr(s1, "_insert_event", boom)
        with pytest.raises(RuntimeError):
            m1.allocate("a", "bot-01", Venue.COINBASE, "USD", D(750), EUR_PER_USD)
        monkeypatch.undo()
        assert m1.state_digest() == before
        s1.close()
        m2, s2 = open_manager(ledger_dir)
        try:
            assert m2.state_digest() == before
            assert m2.treasury_eur == D(5000)
        finally:
            s2.close()

    def test_events_are_append_only(self, manager: CapitalManager) -> None:
        allocate_all(manager)
        seqs = [e.seq for e in manager.events()]
        assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))


def test_numerically_equal_retry_is_idempotent_not_a_conflict(manager: CapitalManager) -> None:
    manager.allocate("a1", "bot-01", Venue.COINBASE, "USD", D("750"), EUR_PER_USD)
    manager.allocate("a1", "bot-01", Venue.COINBASE, "USD", D("750.00"), D("0.8600"))
    assert manager.treasury_eur == D(4250)
