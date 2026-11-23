"""Durable day-start (00:00 UTC) and peak equity snapshots (X6): audited, restart-exact."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.paper.equity import PORTFOLIO, EquitySnapshots, cents_up
from shadow_trading_suite.storage.ledger import LedgerStore

D = Decimal
T = datetime(2026, 10, 20, 23, 59, 59, 999000, tzinfo=UTC)


def open_store(tmp: Path, clock: ManualClock) -> LedgerStore:
    return LedgerStore.open(tmp / "data", clock=clock)


def test_first_observation_sets_day_start_and_peak(tmp_path: Path) -> None:
    clock = ManualClock(T)
    store = open_store(tmp_path, clock)
    snaps = EquitySnapshots(store)
    marks = snaps.observe(T, {PORTFOLIO: D("4990.123"), "bot-01": D("748.5")})
    assert marks[PORTFOLIO].start_of_day == D("4990.13")  # rounded up: conservative
    assert marks[PORTFOLIO].peak == D("4990.13")
    assert marks["bot-01"].day == "2026-10-20"
    store.close()


def test_the_day_boundary_is_00_00_utc(tmp_path: Path) -> None:
    clock = ManualClock(T)
    store = open_store(tmp_path, clock)
    snaps = EquitySnapshots(store)
    snaps.observe(T, {PORTFOLIO: D(5000)})
    same_day = snaps.observe(T, {PORTFOLIO: D(4800)})
    assert same_day[PORTFOLIO].start_of_day == D(5000)
    midnight = T + timedelta(milliseconds=1)
    assert midnight == datetime(2026, 10, 21, tzinfo=UTC)
    nxt = snaps.observe(midnight, {PORTFOLIO: D(4800)})
    assert nxt[PORTFOLIO].start_of_day == D(4800)
    assert nxt[PORTFOLIO].day == "2026-10-21"
    assert nxt[PORTFOLIO].peak == D(5000)  # the peak never resets (M5 drawdown semantics)
    store.close()


def test_peaks_rise_only_and_are_recorded_once_per_cent(tmp_path: Path) -> None:
    clock = ManualClock(T)
    store = open_store(tmp_path, clock)
    snaps = EquitySnapshots(store)
    snaps.observe(T, {PORTFOLIO: D("5000.001")})  # recorded as 5000.01
    n = len(store.audit_records())
    snaps.observe(T, {PORTFOLIO: D("5000.004")})  # within the recorded cent
    snaps.observe(T, {PORTFOLIO: D("5000.01")})
    snaps.observe(T, {PORTFOLIO: D("4999")})
    assert len(store.audit_records()) == n
    marks = snaps.observe(T, {PORTFOLIO: D("5000.011")})
    assert marks[PORTFOLIO].peak == D("5000.02")
    assert len(store.audit_records()) == n + 1
    store.close()


def test_snapshots_survive_a_restart_exactly(tmp_path: Path) -> None:
    clock = ManualClock(T)
    store = open_store(tmp_path, clock)
    snaps = EquitySnapshots(store)
    snaps.observe(T, {PORTFOLIO: D(5000), "bot-03": D(1500)})
    snaps.observe(T, {PORTFOLIO: D(5100), "bot-03": D(1400)})
    before = snaps.current()
    store.close()
    store2 = open_store(tmp_path, clock)
    again = EquitySnapshots(store2)
    assert again.current() == before
    assert again.observe(T, {PORTFOLIO: D(4000), "bot-03": D(1000)}) == before
    store2.verify_audit_chain()
    store2.close()


def test_non_positive_equity_is_never_a_snapshot(tmp_path: Path) -> None:
    clock = ManualClock(T)
    store = open_store(tmp_path, clock)
    with pytest.raises(ValueError, match="positive"):
        EquitySnapshots(store).observe(T, {PORTFOLIO: D(0)})
    assert store.audit_records() == []
    store.close()


@settings(max_examples=50)
@given(values=st.lists(st.decimals(min_value=1, max_value=10_000, places=4), min_size=1))
def test_peak_is_the_running_maximum_rounded_up(values: list[Decimal]) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        clock = ManualClock(T)
        store = open_store(Path(tmp), clock)
        snaps = EquitySnapshots(store)
        for v in values:
            marks = snaps.observe(T, {PORTFOLIO: v})
        assert marks[PORTFOLIO].peak == cents_up(max(values))
        assert marks[PORTFOLIO].peak >= max(values)
        store.close()
