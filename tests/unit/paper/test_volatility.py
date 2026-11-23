"""Minute-close volatility from public trades: deterministic, warm-up and gaps fail closed."""

import math
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.paper.volatility import VolatilityPolicy, VolatilityTracker
from shadow_trading_suite.streaming.events import StreamKind
from tests.unit.paper.feeds import T0, ScriptedFeed
from tests.unit.risk.test_engine import PERP

D = Decimal
POLICY = VolatilityPolicy(horizon_minutes=60, min_returns=5, window=120, max_gap_minutes=3)


def run(
    prices: list[str], *, step_s: int = 60, policy: VolatilityPolicy = POLICY
) -> tuple[VolatilityTracker, ScriptedFeed]:
    f = ScriptedFeed(ManualClock(T0))
    f.live(PERP.id, StreamKind.TRADES)
    tracker = VolatilityTracker(policy)
    for i, p in enumerate(prices):
        f.clock.set(T0 + timedelta(seconds=step_s * i))
        tracker.observe(f.trade(PERP.id, p))
    return tracker, f


def test_sigma_is_sample_stdev_of_minute_returns_scaled_to_the_horizon() -> None:
    prices = ["100", "101", "100", "102", "101", "103", "102"]
    tracker, f = run(prices)
    # the last minute (102) is still open: closes are the first 6 prices
    closes = [D(p) for p in prices[:6]]
    rets = [b / a - 1 for a, b in pairwise(closes)]
    mean = sum(rets, D(0)) / len(rets)
    var = sum(((r - mean) ** 2 for r in rets), D(0)) / (len(rets) - 1)
    expected = var.sqrt() * D(60).sqrt()
    assert tracker.sigma(PERP.id, f.clock.now()) == expected


def test_warm_up_is_required() -> None:
    tracker, f = run(["100", "101", "100"])
    with pytest.raises(DataQualityError, match="warm-up"):
        tracker.sigma(PERP.id, f.clock.now())


def test_warm_up_needs_exactly_min_returns() -> None:
    prices = ["100", "101", "100", "102", "101", "103"]  # 5 closed minutes = 4 returns
    short, f = run([*prices[:-1], "100.5"])
    with pytest.raises(DataQualityError, match="4/5"):
        short.sigma(PERP.id, f.clock.now())
    enough, f2 = run([*prices, "100.5"])  # 6 closed minutes = 5 returns
    assert enough.sigma(PERP.id, f2.clock.now()) > 0


def test_zero_variation_is_not_a_volatility_estimate() -> None:
    tracker, f = run(["100"] * 8)
    with pytest.raises(DataQualityError, match="no price variation"):
        tracker.sigma(PERP.id, f.clock.now())


def test_an_unknown_instrument_fails_closed() -> None:
    tracker = VolatilityTracker(POLICY)
    with pytest.raises(DataQualityError):
        tracker.sigma(PERP.id, T0)


def test_a_long_trade_gap_restarts_the_window_never_interpolates() -> None:
    tracker, f = run(["100", "101", "100", "102", "101", "103", "102"])
    f.clock.set(f.clock.now() + timedelta(minutes=10))
    tracker.observe(f.trade(PERP.id, "150"))
    with pytest.raises(DataQualityError, match="warm-up"):
        tracker.sigma(PERP.id, f.clock.now())


def test_a_silent_market_makes_sigma_stale() -> None:
    tracker, f = run(["100", "101", "100", "102", "101", "103", "102"])
    with pytest.raises(DataQualityError, match="stale"):
        tracker.sigma(PERP.id, f.clock.now() + timedelta(minutes=5))


def test_multi_minute_gaps_are_normalised_per_minute() -> None:
    every_minute, f1 = run(["100", "101", "100", "102", "101", "103", "102"])
    two_minutes, f2 = run(["100", "101", "100", "102", "101", "103", "102"], step_s=120)
    s1 = every_minute.sigma(PERP.id, f1.clock.now())
    s2 = two_minutes.sigma(PERP.id, f2.clock.now())
    assert abs(s2 * D(2).sqrt() - s1) < D("1e-20")  # same moves over twice the time


def test_out_of_order_trades_are_ignored() -> None:
    tracker, f = run(["100", "101", "100", "102", "101", "103", "102"])
    before = tracker.sigma(PERP.id, f.clock.now())
    old = f.trade(PERP.id, "500").model_copy(update={"exchange_ts": T0, "event_id": "late"})
    tracker.observe(old)
    assert tracker.sigma(PERP.id, f.clock.now()) == before


@settings(max_examples=40)
@given(
    moves=st.lists(st.integers(min_value=-50, max_value=50), min_size=8, max_size=30),
    scale=st.integers(min_value=1, max_value=1000),
)
def test_sigma_is_scale_invariant_and_deterministic(moves: list[int], scale: int) -> None:
    assume(len(set(moves[1:-1])) > 1)  # zero variation is refused (tested above)
    base = [D(10_000) + sum(moves[: i + 1]) for i in range(len(moves))]
    a, fa = run([str(p) for p in base])
    b, fb = run([str(p * scale) for p in base])
    c, _ = run([str(p) for p in base])
    sa, sb = a.sigma(PERP.id, fa.clock.now()), b.sigma(PERP.id, fb.clock.now())
    assert math.isclose(float(sa), float(sb), rel_tol=1e-12, abs_tol=1e-18)
    assert c.sigma(PERP.id, fa.clock.now()) == sa


def test_seeding_from_candle_closes_matches_the_trade_path() -> None:
    prices = ["100", "101", "100", "102", "101", "103", "102"]
    live, f = run(prices)
    seeded = VolatilityTracker(POLICY)
    closes = [(T0 + timedelta(minutes=i), D(p)) for i, p in enumerate(prices[:6])]
    assert seeded.seed(PERP.id, closes) == 5
    now = T0 + timedelta(minutes=6)
    assert seeded.sigma(PERP.id, now) == live.sigma(PERP.id, f.clock.now())
    seeded.observe(f.trade(PERP.id, "104"))  # live trades continue the seeded window
    assert seeded.sigma(PERP.id, now) == live.sigma(PERP.id, f.clock.now())


def test_a_warm_window_is_never_replaced_by_a_seed() -> None:
    live, f = run(["100", "101", "100", "102", "101", "103", "102"])
    before = live.sigma(PERP.id, f.clock.now())
    assert live.seed(PERP.id, [(T0, D(1)), (T0 + timedelta(minutes=1), D(2))]) == 0
    assert live.sigma(PERP.id, f.clock.now()) == before
