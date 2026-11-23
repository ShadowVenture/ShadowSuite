"""Assumption registry (§7), support matrix (E9) and the pure matching rules (E2, E8)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.market import BookLevel, OrderBookSnapshot, Trade
from shadow_trading_suite.core.orders import OrderType, Side
from shadow_trading_suite.execution.assumptions import (
    DEFAULT_PROFILE,
    PROFILES,
    LatencyAction,
    Parameter,
    Source,
    default_assumption_set,
    jitter_ms,
    latency,
)
from shadow_trading_suite.execution.matching import queue_step, walk
from shadow_trading_suite.execution.model import Consumption, ExecOrder, OrderTerms, TimeInForce
from shadow_trading_suite.execution.support import (
    ENABLED,
    ExecutionNotSupportedError,
    support_for,
)
from tests.support.portfolio import sample_portfolio_config
from tests.unit.execution.harness import ETH_PERP
from tests.unit.risk.test_engine import CB_SPOT, CC_SPOT, FCM, PERP, cb, cc

D = Decimal
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
CFG = sample_portfolio_config()
PINNED_SHA256 = "d118dcd5e9173842b4f30d0f3f8e6d572bbf90d15c1e6f852075ade35952818c"


class TestAssumptions:
    def test_hash_is_pinned_to_the_version(self) -> None:
        a = default_assumption_set(CFG)
        assert (a.version, a.sha256) == ("1.0.0", PINNED_SHA256), (
            "an assumption value changed: bump AssumptionSet.version and re-pin the hash"
        )

    def test_fees_and_margins_are_the_unchanged_m5_values(self) -> None:
        a = default_assumption_set(CFG)
        assert a.decimal("fee.spot_taker") == CFG.fees.spot_taker.value == D("0.006")
        assert a.decimal("fee.derivatives_maker") == CFG.fees.derivatives_maker.value
        assert a.decimal("mmr.cryptocom_major") == CFG.margin.cryptocom_major_mmr.value
        assert a.decimal("liquidation.fee_rate") == D("0.005")

    def test_every_parameter_is_labelled_and_traceable(self) -> None:
        a = default_assumption_set(CFG)
        for p in a.parameters.values():
            if p.source is Source.VERIFIED:
                assert p.evidence
            if p.source is Source.DERIVED:
                assert p.inputs
        assert "book.depth.cryptocom" in a.by_source(Source.VERIFIED)
        assert "queue.model" in a.by_source(Source.ASSUMPTION)
        with pytest.raises(ValidationError, match="evidence"):
            Parameter(value="1", source=Source.VERIFIED)
        with pytest.raises(ValidationError, match="inputs"):
            Parameter(value="1", source=Source.DERIVED)

    def test_the_five_profiles_and_the_default(self) -> None:
        assert DEFAULT_PROFILE == "retail_eu"
        assert {
            k: (p.submit_ms, p.cancel_ms, p.liquidation_ms, p.jitter_ms)
            for k, p in PROFILES.items()
        } == {
            "ideal": (0, 0, 0, 0),
            "colocated": (20, 20, 20, 5),
            "retail_eu": (250, 250, 250, 50),
            "degraded": (1000, 1500, 1000, 250),
            "stressed": (3000, 5000, 3000, 1000),
        }

    @settings(max_examples=200)
    @given(
        st.sampled_from(sorted(PROFILES)),
        st.text(min_size=1, max_size=20),
        st.sampled_from(list(LatencyAction)),
    )
    def test_jitter_is_deterministic_and_bounded(
        self, pid: str, subject: str, action: LatencyAction
    ) -> None:
        p = PROFILES[pid]
        j = jitter_ms(p, "seed", subject, action)
        assert j == jitter_ms(p, "seed", subject, action)
        assert -p.jitter_ms <= j <= p.jitter_ms
        assert latency(p, "seed", subject, action) >= timedelta(0)
        if pid == "ideal":
            assert latency(p, "seed", subject, action) == timedelta(0)


class TestSupport:
    @pytest.mark.parametrize("inst", [PERP, CC_SPOT, CB_SPOT, ETH_PERP])
    def test_enabled_instruments(self, inst: object) -> None:
        support = support_for(inst)  # type: ignore[arg-type]
        assert support.instrument_id in ENABLED

    @pytest.mark.parametrize(
        ("inst", "reason"),
        [
            (FCM, "Coinbase derivatives are disabled"),
            (cc("BTCUSD-261030"), "dated futures disabled"),
            (cc("BTC_EUR"), "USD"),
            (cc("EUR_USDT"), "USD"),
        ],
    )
    def test_refused_with_a_stated_reason(self, inst: object, reason: str) -> None:
        with pytest.raises(ExecutionNotSupportedError, match=reason):
            support_for(inst)  # type: ignore[arg-type]

    def test_intx_perpetuals_are_refused(self) -> None:
        intx = cb("products_perpetual", "BTC-PERP-INTX")
        with pytest.raises(ExecutionNotSupportedError, match="Coinbase derivatives"):
            support_for(intx)


IID = InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTCUSD-PERP")


def book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        instrument_id=IID,
        ts=NOW,
        bids=tuple(BookLevel(price=D(p), quantity=D(q)) for p, q in bids),
        asks=tuple(BookLevel(price=D(p), quantity=D(q)) for p, q in asks),
    )


def w(b: OrderBookSnapshot, qty: str, limit: Decimal | None = None) -> list[tuple[str, str]]:
    result = walk(
        b,
        {},
        buy=True,
        quantity=D(qty),
        contract_size=D(1),
        lot_size=D("0.0001"),
        limit=limit,
        band=None,
        now=NOW,
        replenish=timedelta(seconds=1),
    )
    return [(str(t.price), str(t.quantity)) for t in result.takes]


class TestWalk:
    B = book([("99", "1")], [("100", "0.3"), ("101", "0.3"), ("110", "5")])

    def test_walks_levels_and_floors_to_the_lot(self) -> None:
        assert w(self.B, "0.45") == [("100", "0.3"), ("101", "0.15")]
        assert w(book([("99", "1")], [("100", "0.00015")]), "1") == [("100", "0.0001")]

    def test_limit_band_and_depth_stops(self) -> None:
        assert w(self.B, "1", limit=D(100)) == [("100", "0.3")]
        r = walk(
            self.B,
            {},
            buy=True,
            quantity=D(10),
            contract_size=D(1),
            lot_size=D("0.0001"),
            limit=None,
            band=(D(90), D(105)),
            now=NOW,
            replenish=timedelta(seconds=1),
        )
        assert r.stop == "price_band"
        r = walk(
            self.B,
            {},
            buy=True,
            quantity=D(10),
            contract_size=D(1),
            lot_size=D("0.0001"),
            limit=None,
            band=None,
            now=NOW,
            replenish=timedelta(seconds=1),
        )
        assert (r.stop, r.quantity) == ("depth", D("5.6"))

    def test_consumed_size_is_unavailable_until_replenished_or_updated(self) -> None:
        used = {
            "cryptocom:BTCUSD-PERP:ask:100": Consumption(
                instrument_id=IID,
                book_side="ask",
                price=D(100),
                level_quantity=D("0.3"),
                consumed=D("0.2"),
                ts=NOW,
                book_event_id="e1",
            )
        }

        def available(b: OrderBookSnapshot, at: datetime) -> Decimal:
            return walk(
                b,
                used,
                buy=True,
                quantity=D(1),
                contract_size=D(1),
                lot_size=D("0.0001"),
                limit=D(100),
                band=None,
                now=at,
                replenish=timedelta(seconds=1),
            ).quantity

        assert available(self.B, NOW + timedelta(milliseconds=999)) == D("0.1")
        assert available(self.B, NOW + timedelta(seconds=1)) == D("0.3")
        assert available(book([("99", "1")], [("100", "0.4")]), NOW) == D("0.4")

    @settings(max_examples=100)
    @given(
        st.lists(
            st.tuples(st.integers(1, 50), st.integers(1, 1000)),
            min_size=1,
            max_size=8,
            unique_by=lambda x: x[0],
        ),
        st.integers(1, 5000),
    )
    def test_never_fills_beyond_the_displayed_depth(
        self, levels: list[tuple[int, int]], qty: int
    ) -> None:
        asks = sorted((D(100 + p), D(q) / 1000) for p, q in levels)
        b = book([("99", "1")], [(str(p), str(q)) for p, q in asks])
        r = walk(
            b,
            {},
            buy=True,
            quantity=D(qty) / 1000,
            contract_size=D(1),
            lot_size=D("0.001"),
            limit=None,
            band=None,
            now=NOW,
            replenish=timedelta(seconds=1),
        )
        assert r.quantity <= min(D(qty) / 1000, sum(q for _, q in asks))
        for take, (price, q) in zip(r.takes, asks, strict=False):
            assert take.price == price
            assert take.quantity <= q


def resting(queue: str, venue: Venue = Venue.CRYPTOCOM, side: Side = Side.BUY) -> ExecOrder:
    return ExecOrder(
        order_id="o",
        request_id="r",
        bot_id="bot-03",
        instrument_id=InstrumentId(venue=venue, symbol="X"),
        side=side,
        quantity=D("0.01"),
        terms=OrderTerms(
            order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC, limit_price=D(100)
        ),
        reduce_only=False,
        leverage=D(1),
        contract_size=D(1),
        lot_size=D("0.0001"),
        is_spot=False,
        decision_mid=D(100),
        band=D("0.01"),
        profile_id="ideal",
        accepted_at=NOW,
        effective_at=NOW,
        queue_ahead=D(queue),
    )


def trade(price: str, qty: str, aggressor: Side | None) -> Trade:
    return Trade(
        instrument_id=IID,
        trade_id="t",
        ts=NOW,
        price=D(price),
        quantity=D(qty),
        aggressor=aggressor,
    )


class TestQueueModel:
    def test_cryptocom_at_price_works_off_the_queue_then_fills(self) -> None:
        r = queue_step(
            resting("0.5"), trade("100", "0.505", Side.SELL), D("0.505"), Venue.CRYPTOCOM
        )
        assert (r.fill, r.queue_ahead, r.trade_left) == (D("0.005"), D(0), D(0))

    def test_cryptocom_same_side_aggressor_never_fills(self) -> None:
        r = queue_step(resting("0"), trade("100", "5", Side.BUY), D(5), Venue.CRYPTOCOM)
        assert r.fill == 0
        assert r.queue_ahead == 0

    def test_coinbase_at_price_only_reduces_the_queue(self) -> None:
        r = queue_step(
            resting("0.5", Venue.COINBASE), trade("100", "5", None), D(5), Venue.COINBASE
        )
        assert (r.fill, r.queue_ahead) == (D(0), D(0))

    def test_trade_through_fills_up_to_traded_volume(self) -> None:
        r = queue_step(
            resting("9", Venue.COINBASE), trade("99", "0.004", None), D("0.004"), Venue.COINBASE
        )
        assert r.fill == D("0.004")
        sell = resting("9", side=Side.SELL)
        r = queue_step(sell, trade("101", "1", Side.BUY), D(1), Venue.CRYPTOCOM)
        assert r.fill == D("0.01")

    def test_prices_outside_the_order_never_fill(self) -> None:
        assert (
            queue_step(resting("0"), trade("101", "1", Side.SELL), D(1), Venue.CRYPTOCOM).fill == 0
        )
