"""Real-time EUR rate (C11/X5): triangulated, cross-checked, fresh, recorded, fail closed."""

from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.paper.fx import FX_SUBSCRIPTIONS, FxPolicy, eur_rates, usd_eur_rate
from shadow_trading_suite.streaming.events import StreamKind
from shadow_trading_suite.streaming.state import StreamKey, StreamStatus
from tests.unit.paper.feeds import BTC_EUR, BTC_USD, ETH_EUR, ETH_USD, T0, ScriptedFeed

D = Decimal
POLICY = FxPolicy()


def feed() -> ScriptedFeed:
    f = ScriptedFeed(ManualClock(T0))
    f.fx()
    return f


def test_usd_is_triangulated_via_btc_and_cross_checked_via_eth() -> None:
    f = feed()
    q = usd_eur_rate(f.snapshot(), POLICY)
    assert q.currency == "USD"
    assert q.eur_per_unit == (D(70520) / D(82000)).quantize(D("1e-18"))
    assert q.cross_check == (D(2580) / D(3000)).quantize(D("1e-18"))
    assert q.method == "triangulated:coinbase:BTC-EUR/BTC-USD;check:ETH-EUR/ETH-USD"
    assert len(q.sources) == 4
    assert all(s.startswith("coinbase:ticker:") for s in q.sources)
    assert q.as_of == T0
    assert eur_rates(q) == {"USD": q.eur_per_unit, "EUR": D(1)}


def test_subscriptions_are_public_coinbase_tickers_only() -> None:
    assert {(s.instrument_id, s.kind) for s in FX_SUBSCRIPTIONS} == {
        (i, StreamKind.TICKER) for i in (BTC_EUR, BTC_USD, ETH_EUR, ETH_USD)
    }


def test_cross_check_disagreement_fails_closed() -> None:
    f = feed()
    f.ticker(ETH_EUR, "2699", "2701")  # ETH route 0.9 vs BTC route 0.86: 4.6% apart
    with pytest.raises(DataQualityError, match="cross-check"):
        usd_eur_rate(f.snapshot(), POLICY)


def test_a_stale_leg_fails_closed() -> None:
    f = feed()
    f.clock.set(T0 + timedelta(seconds=11))
    with pytest.raises(DataQualityError, match="stale"):
        usd_eur_rate(f.snapshot(), POLICY)


def test_legs_too_far_apart_in_exchange_time_fail_closed() -> None:
    f = feed()
    f.clock.set(T0 + timedelta(seconds=6))
    f.ticker(BTC_USD, "81999", "82001")  # BTC-EUR leg is 6 s older than BTC-USD
    with pytest.raises(DataQualityError, match="skew"):
        usd_eur_rate(f.snapshot(), POLICY)


def test_a_non_live_leg_fails_closed() -> None:
    f = feed()
    f.state.set_status(StreamKey(BTC_EUR, StreamKind.TICKER), StreamStatus.SYNCING)
    with pytest.raises(DataQualityError, match="syncing"):
        usd_eur_rate(f.snapshot(), POLICY)


def test_a_one_sided_ticker_fails_closed() -> None:
    f = feed()
    f.emit(
        BTC_EUR,
        StreamKind.TICKER,
        f.snapshot()
        .entries[StreamKey(BTC_EUR, StreamKind.TICKER)]
        .payload.model_copy(update={"bid": None}),
    )
    with pytest.raises(DataQualityError, match="two-sided"):
        usd_eur_rate(f.snapshot(), POLICY)


@given(scale=st.decimals(min_value=D("0.5"), max_value=D("2"), places=3))
def test_rate_is_invariant_to_the_crypto_price_level(scale: Decimal) -> None:
    """Scaling BTC (or ETH) prices in both currencies leaves EUR/USD unchanged."""
    f = ScriptedFeed(ManualClock(T0))
    f.fx(
        btc_eur=str(D(70520) * scale * 100),
        btc_usd=str(D(82000) * scale * 100),
        eth_eur=str(D(2580) * scale),
        eth_usd=str(D(3000) * scale),
    )
    q = usd_eur_rate(f.snapshot(), POLICY)
    assert abs(q.eur_per_unit - D("0.86")) < D("0.0001")  # only the 2-unit spreads differ
