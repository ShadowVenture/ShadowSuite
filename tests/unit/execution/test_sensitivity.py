"""Latency sensitivity (§3.1) and the uncertainty report: the same scripted scenario under all
five profiles; deterministic, monotonic on a synthetic trend, `ideal` = pure book walk."""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.execution.assumptions import PROFILES
from shadow_trading_suite.execution.matching import walk
from shadow_trading_suite.execution.report import DISCLAIMER, ExecutionReport, execution_report
from shadow_trading_suite.storage.ledger import canonical_json
from tests.unit.execution.harness import PERP, Harness, market_terms

D = Decimal
ORDER = ["ideal", "colocated", "retail_eu", "degraded", "stressed"]


def trend_book(step: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Synthetic monotonic uptrend: +1 USD per 100 ms."""
    return [(str(81999 + step), "1")], [(str(82001 + step), "0.004"), (str(82002 + step), "1")]


def scenario(tmp: Path, profile: str) -> ExecutionReport:
    h = Harness(tmp, profiles={"bot-03": profile})
    try:
        bids, asks = trend_book(0)
        h.perp_ready(bids=bids, asks=asks)
        assert h.accept(PERP, Side.BUY, "0.01", market_terms()).accepted
        h.book(PERP, bids, asks)  # same instant: only `ideal` may fill here
        for step in range(1, 50):
            h.advance(100)
            h.book(PERP, *trend_book(step))
            if not h.engine.state.orders:
                break
        h.verify_all()
        return execution_report(h.store.audit_records(), h.engine.assumptions, {"bot-03": profile})
    finally:
        h.close()


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ExecutionReport]:
    return {p: scenario(tmp_path_factory.mktemp(p), p) for p in ORDER}


def shortfall(report: ExecutionReport) -> Decimal:
    fills = report.fills
    return sum((f.shortfall_bp for f in fills), D(0)) / len(fills)


def test_all_five_profiles_are_reported(reports: dict[str, ExecutionReport]) -> None:
    assert set(reports) == set(PROFILES)
    for r in reports.values():
        assert r.fills
        assert r.disclaimer == DISCLAIMER
        assert "queue.model" in r.assumption_parameters
        assert "book.depth.cryptocom" in r.verified_parameters
        assert not set(r.verified_parameters) & set(r.assumption_parameters)


def test_shortfall_is_non_decreasing_in_latency_on_a_trend(
    reports: dict[str, ExecutionReport],
) -> None:
    values = [shortfall(reports[p]) for p in ORDER]
    assert values == sorted(values), dict(zip(ORDER, values, strict=True))
    assert values[0] < values[-1]


def test_reports_are_deterministic(tmp_path: Path, reports: dict[str, ExecutionReport]) -> None:
    again = scenario(tmp_path / "again", "retail_eu")
    a = canonical_json(again.model_dump(mode="json"))
    b = canonical_json(reports["retail_eu"].model_dump(mode="json"))
    assert a == b


def test_ideal_equals_the_pure_book_walk_at_the_decision_snapshot(
    reports: dict[str, ExecutionReport],
) -> None:
    from tests.unit.execution.test_assumptions_support_matching import book

    bids, asks = trend_book(0)
    expected = walk(
        book(bids, asks),
        {},
        buy=True,
        quantity=D("0.01"),
        contract_size=D(1),
        lot_size=PERP.lot_size,
        limit=None,
        band=None,
        now=book(bids, asks).ts,
        replenish=timedelta(seconds=1),
    )
    ideal = reports["ideal"]
    assert [f.fill_id.split(":")[-1] for f in ideal.fills] == ["0", "1"]
    walked = [(t.price, t.quantity) for t in expected.takes]
    mids = D(82000)
    assert [round(f.shortfall_bp, 6) for f in ideal.fills] == [
        round((p - mids) / mids * 10_000, 6) for p, _ in walked
    ]


def test_report_lists_assumptions_separately_from_verified_behaviour(
    reports: dict[str, ExecutionReport],
) -> None:
    r = json.loads(canonical_json(reports["retail_eu"].model_dump(mode="json")))
    assert r["assumption_sha256"] == reports["ideal"].assumption_sha256
    assert all(f["basis"] in ("book_walk", "queue_model") for f in r["fills"])
    assert "NOT evidence of real-world" in r["disclaimer"]
