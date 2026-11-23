"""B3: per-fill liquidation accounting (`begin_liquidation` -> `book_liquidation_fill`* ->
`finish_liquidation`), reconciled against the M6-A whole-position `settle_liquidation` and an
independent reference model.

Reference model (pooled isolated margin): with M = margin at the trigger, pnl_i each fill's
price PnL (losses rounded up, gains down) and fee_i = ceil(0.5% x notional_i), the per-fill
booking ends with margin returned = max(0, X) and insurance absorbed = max(0, -X), where
X = M + sum(pnl_i) - sum(fee_i), independent of the fill order. The whole-position settlement
nets the PnL and rounds once, so economic results agree within 2(n + 1) x 1e-8 for n fills.
The LEDGER views also agree, except when the total price result is a gain and the fee exceeds
the margin: the whole settlement pays the gain to cash and absorbs the fee shortfall, while the
pooled per-fill model uses the gain to pay the fee (less absorption, same economic equity).
"""

from collections.abc import Iterator
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shadow_trading_suite.accounting.trading import (
    LedgerError,
    LiquidationExecution,
    LiquidationTrigger,
    OrderState,
)
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.storage.ledger import LedgerStore
from tests.unit.accounting.test_trading import BOT, NOW, PERP, START_CASH, Env, a

D = Decimal
Q = D("1e-8")
FEE = D("0.005")
IID = PERP.instrument_id
MARGIN = a("margin", "BTCUSD-PERP")
INSURANCE = a("sim", "insurance_absorption")
TRIGGER = LiquidationTrigger(
    mark=D("78300.0"), maintenance_requirement=D("3.915"), position_equity=D("-2.0"), ts=NOW
)


def open_position(env: Env, side: Side = Side.BUY, qty: str = "0.01", lev: str = "20") -> None:
    env.order("p1", PERP, side, qty, "82000", fee="0.0006", lev=lev)
    env.fill("p1f", "p1", "82000", qty, "0.0006")


def fills(env: Env, execs: list[tuple[str, str]]) -> None:
    env.ledger.begin_liquidation("liq:p:begin", BOT, IID, TRIGGER)
    for n, (price, qty) in enumerate(execs):
        env.ledger.book_liquidation_fill(
            f"liq:p:fill:{n}", BOT, IID, LiquidationExecution(price=D(price), quantity=D(qty)), FEE
        )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.store.close()


class TestGolden:
    def test_two_fills_reproduce_the_section_4_6_bad_debt_example(self, env: Env) -> None:
        open_position(env)  # margin 41.00, open fee 0.492
        fills(env, [("77600", "0.005"), ("77400", "0.005")])  # average 77,500 as in §4.6
        # fill 1: loss 22.00 -> margin 19.00; fee 1.94 -> margin 17.06
        # fill 2: loss 23.00 -> margin 0, absorbed 5.94; fee 1.935 -> absorbed 7.875
        assert env.bal(MARGIN) == 0
        assert -env.bal(INSURANCE) == D("7.875")
        assert env.ledger.position(BOT, IID) is None
        report = env.ledger.finish_liquidation("liq:p:finish", BOT, IID)
        assert report.execution_loss == D(45)
        assert report.liquidation_fee == D("3.875")
        assert (report.margin_before, report.margin_returned) == (D(41), D(0))
        assert report.insurance_absorbed == D("7.875")
        assert report.average_price == D(77500)
        assert env.ledger.bad_debt(BOT, PERP.instrument_id.venue, "USD") == D("7.875")
        # same balances as the single-operation M6-A golden example
        assert env.bal(a("cash")) == START_CASH - D(41) - D("0.492")
        assert env.ledger.liquidation(BOT, IID) is None
        assert env.ledger.verify_replay() == []

    def test_each_fills_fee_is_rounded_up_to_the_quantum(self, env: Env) -> None:
        open_position(env)
        fills(env, [("77600.123", "0.0013"), ("77600", "0.0087")])
        # fee 1: 100.8801599 x 0.5% = 0.5044007995 -> 0.50440080 (never rounded down)
        # fee 2: 675.12 x 0.5% = 3.3756
        report = env.ledger.finish_liquidation("liq:p:finish", BOT, IID)
        assert report.liquidation_fee == D("3.88000080")
        assert env.ledger.verify_replay() == []

    def test_a_later_gain_reverses_absorption_before_refilling_margin(self, env: Env) -> None:
        open_position(env)
        fills(env, [("72000", "0.005"), ("86000", "0.005")])
        # fill 1: loss 50 -> margin 0, absorbed 9; fee 1.80 -> absorbed 10.80
        # fill 2: gain 20 -> repays 10.80, margin 9.20; fee 2.15 -> margin 7.05
        assert (env.bal(MARGIN), env.bal(INSURANCE)) == (D("7.05"), D(0))
        assert env.ledger.bad_debt(BOT, PERP.instrument_id.venue, "USD") == 0
        report = env.ledger.finish_liquidation("liq:p:finish", BOT, IID)
        assert report.margin_returned == D("7.05")
        assert report.insurance_absorbed == 0
        assert report.execution_loss == D(30)
        assert env.bal(a("cash")) == START_CASH - D(41) - D("0.492") + D("7.05")

    def test_margin_stays_locked_until_flat(self, env: Env) -> None:
        open_position(env)
        cash = env.bal(a("cash"))
        fills(env, [("81000", "0.004")])  # loss 4.00, fee 1.62 from margin; no release
        assert env.bal(a("cash")) == cash
        assert env.bal(MARGIN) == D(41) - D(4) - D("1.62")
        assert env.ledger.position(BOT, IID).quantity == D("0.006")  # type: ignore[union-attr]
        with pytest.raises(LedgerError, match="not flat"):
            env.ledger.finish_liquidation("liq:p:finish", BOT, IID)


class TestSafety:
    def test_each_fill_is_booked_exactly_once(self, env: Env) -> None:
        open_position(env)
        fills(env, [("80000", "0.004")])
        before = env.ledger.state_digest()
        env.ledger.book_liquidation_fill(  # replayed event: same op id -> no-op
            "liq:p:fill:0", BOT, IID, LiquidationExecution(price=D(80000), quantity=D("0.004")), FEE
        )
        assert env.ledger.state_digest() == before
        with pytest.raises(LedgerError, match="exceeds position"):
            env.ledger.book_liquidation_fill(
                "liq:p:fill:9", BOT, IID, LiquidationExecution(price=D(1), quantity=D(1)), FEE
            )

    def test_orders_fills_and_whole_settlement_are_refused_while_liquidating(
        self, env: Env
    ) -> None:
        open_position(env)
        env.order("r1", PERP, Side.SELL, "0.002", "82000", fee="0.0006", lev="20", reduce_only=True)
        with pytest.raises(LedgerError, match="reduce-only"):
            env.ledger.begin_liquidation("liq:p:begin", BOT, IID, TRIGGER)
        env.ledger.close_order("r1", OrderState.CANCELLED, NOW)
        env.ledger.begin_liquidation("liq:p:begin", BOT, IID, TRIGGER)
        with pytest.raises(LedgerError, match="being liquidated"):
            env.order("x1", PERP, Side.BUY, "0.001", "82000", fee="0.0006", lev="20")
        with pytest.raises(LedgerError, match="already"):
            env.ledger.begin_liquidation("liq:p:begin2", BOT, IID, TRIGGER)
        with pytest.raises(LedgerError, match="per-fill liquidation"):
            env.ledger.settle_liquidation(
                "liq:whole",
                BOT,
                IID,
                [LiquidationExecution(price=D(78000), quantity=D("0.01"))],
                FEE,
                TRIGGER,
            )

    def test_restart_between_fills_continues_without_double_counting(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_position(env)
        fills(env, [("77600", "0.005")])
        digest = env.ledger.state_digest()

        def boom(self: LedgerStore) -> None:
            raise RuntimeError("crash at COMMIT")

        monkeypatch.setattr(LedgerStore, "_commit", boom)
        with pytest.raises(RuntimeError):
            env.ledger.book_liquidation_fill(
                "liq:p:fill:1",
                BOT,
                IID,
                LiquidationExecution(price=D(77400), quantity=D("0.005")),
                FEE,
            )
        monkeypatch.undo()
        ledger = env.reopen()
        assert ledger.state_digest() == digest  # the failed fill left nothing
        progress = ledger.liquidation(BOT, IID)
        assert progress is not None
        assert progress.fills == 1
        ledger.book_liquidation_fill(
            "liq:p:fill:0", BOT, IID, LiquidationExecution(price=D(77600), quantity=D("0.005")), FEE
        )  # re-delivered first fill after restart: no-op
        ledger.book_liquidation_fill(
            "liq:p:fill:1", BOT, IID, LiquidationExecution(price=D(77400), quantity=D("0.005")), FEE
        )
        report = ledger.finish_liquidation("liq:p:finish", BOT, IID)
        assert report.insurance_absorbed == D("7.875")
        assert ledger.verify_replay() == []

    def test_funding_during_liquidation_uses_locked_margin_then_payable(self, env: Env) -> None:
        open_position(env)
        env.ledger.commit("lock", BOT, PERP.instrument_id.venue, "USD", env.bal(a("cash")))
        fills(env, [("77600", "0.005")])  # margin 17.06 left for 0.005 contracts
        env.ledger.book_funding("fund-1", BOT, IID, D("0.5"), D(78000), NOW)  # 195.00 due
        assert env.bal(MARGIN) == 0
        assert env.bal(a("payable", "funding:BTCUSD-PERP")) == -(D(195) - D("17.06"))


# -- reconciliation against M6-A and the independent reference model ---------------------------


def _up(v: Decimal) -> Decimal:
    return v.quantize(Q, ROUND_CEILING)


def _down(v: Decimal) -> Decimal:
    return v.quantize(Q, ROUND_FLOOR)


@st.composite
def scenarios(draw: st.DrawFn) -> tuple[Side, str, int, list[tuple[Decimal, Decimal]]]:
    side = draw(st.sampled_from([Side.BUY, Side.SELL]))
    lev = draw(st.sampled_from(["5", "10", "20", "50"]))
    lots = draw(st.integers(min_value=2, max_value=20))  # 0.001 BTC lots
    cuts = sorted(draw(st.sets(st.integers(1, lots - 1), max_size=min(5, lots - 1))))
    sizes = [b - a for a, b in zip([0, *cuts], [*cuts, lots], strict=True)]
    prices = [D(draw(st.integers(140_000, 188_000))) / 2 for _ in sizes]  # 70k..94k, tick 0.5
    return side, lev, lots, [(p, D(s) / 1000) for p, s in zip(prices, sizes, strict=True)]


@settings(max_examples=40, deadline=None)
@given(scenarios())
def test_split_fills_reconcile_with_whole_settlement_and_reference(
    tmp_path_factory: pytest.TempPathFactory,
    scenario: tuple[Side, str, int, list[tuple[Decimal, Decimal]]],
) -> None:
    side, lev, lots, execs = scenario
    qty = str(D(lots) / 1000)
    whole, split = Env(tmp_path_factory.mktemp("w")), Env(tmp_path_factory.mktemp("s"))
    try:
        for e in (whole, split):
            open_position(e, side, qty, lev)
        margin = whole.bal(MARGIN)
        whole.ledger.settle_liquidation(
            "liq:whole",
            BOT,
            IID,
            [LiquidationExecution(price=p, quantity=q) for p, q in execs],
            FEE,
            TRIGGER,
        )
        split.ledger.begin_liquidation("liq:p:begin", BOT, IID, TRIGGER)
        for n, (p, q) in enumerate(execs):
            split.ledger.book_liquidation_fill(
                f"liq:p:fill:{n}", BOT, IID, LiquidationExecution(price=p, quantity=q), FEE
            )
        report = split.ledger.finish_liquidation("liq:p:finish", BOT, IID)

        # independent reference model
        direction = 1 if side is Side.BUY else -1
        pnls = [(p - D(82000)) * q * direction for p, q in execs]
        rounded = [_down(x) if x > 0 else -_up(-x) for x in pnls]
        fees = [_up(p * q * FEE) for p, q in execs]
        x = margin + sum(rounded, D(0)) - sum(fees, D(0))
        assert report.margin_returned == max(x, D(0))
        assert report.insurance_absorbed == max(-x, D(0))
        assert -split.bal(INSURANCE) == report.insurance_absorbed
        venue = PERP.instrument_id.venue
        assert split.ledger.bad_debt(BOT, venue, "USD") == report.insurance_absorbed
        assert split.bal(MARGIN) == 0
        assert split.ledger.position(BOT, IID) is None

        # economic equivalence with the M6-A whole-position settlement (rounding only)
        def economic(e: Env) -> Decimal:
            return e.bal(a("cash")) + e.bal(a("reserved")) + e.bal(INSURANCE)

        tolerance = 2 * (len(execs) + 1) * Q
        assert abs(economic(split) - economic(whole)) <= tolerance
        net = sum(pnls, D(0))
        if net <= 0 or sum(fees, D(0)) <= margin:  # ledger views agree too (see module doc)
            assert abs(split.bal(a("cash")) - whole.bal(a("cash"))) <= tolerance
            assert abs(split.bal(INSURANCE) - whole.bal(INSURANCE)) <= tolerance
        for e in (whole, split):
            assert e.ledger.verify_replay() == []
    finally:
        whole.store.close()
        split.store.close()
