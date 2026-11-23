"""Trading ledger: golden journal examples (proposal §4), reservations, margin, positions."""

import contextlib
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.accounting.journal import bot_account, portfolio_account
from shadow_trading_suite.accounting.trading import (
    CapitalError,
    FillSpec,
    InstrumentTerms,
    InsufficientCapitalError,
    LedgerError,
    LiquidationExecution,
    LiquidationTrigger,
    OrderSpec,
    OrderState,
    TradingLedger,
)
from shadow_trading_suite.core.instrument import InstrumentId, InstrumentKind, Venue
from shadow_trading_suite.core.orders import Liquidity, Side
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.storage.ledger import LedgerStore

D = Decimal
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
RATE = D("0.86")
COST = D("0.002")
BOT, VENUE = "bot-03", Venue.CRYPTOCOM
START_CASH = D("1740.69767441")
SPOT = InstrumentTerms(
    instrument_id=InstrumentId(venue=VENUE, symbol="BTC_USD"),
    kind=InstrumentKind.SPOT,
    base="BTC",
    quote="USD",
)
PERP = InstrumentTerms(
    instrument_id=InstrumentId(venue=VENUE, symbol="BTCUSD-PERP"),
    kind=InstrumentKind.PERPETUAL,
    base="BTC",
    quote="USD",
    settle="USD",
)


def a(kind: str, detail: str | None = None) -> str:
    return bot_account(BOT, VENUE, kind, detail)


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.store = LedgerStore.open(tmp / "data", clock=ManualClock(NOW))
        self.ledger = TradingLedger(self.store, ManualClock(NOW), COST)
        self.ledger.deposit_paper_capital("seed", D(5000))
        self.ledger.allocate("alloc", BOT, VENUE, "USD", D(1500), RATE)

    def bal(self, account: str, ccy: str = "USD") -> Decimal:
        return self.ledger.balance(account, ccy)

    def order(
        self,
        oid: str,
        inst: InstrumentTerms,
        side: Side,
        qty: str,
        price: str,
        fee: str = "0.006",
        lev: str = "1",
        reduce_only: bool = False,
    ) -> None:
        self.ledger.open_order(
            OrderSpec(
                order_id=oid,
                bot_id=BOT,
                instrument=inst,
                side=side,
                quantity=D(qty),
                reserve_price=D(price),
                reserve_fee_rate=D(fee),
                leverage=D(lev),
                reduce_only=reduce_only,
                created_at=NOW,
            )
        )

    def fill(
        self, fid: str, oid: str, price: str, qty: str, fee: str, liq: Liquidity = Liquidity.TAKER
    ) -> None:
        self.ledger.book_fill(
            FillSpec(
                fill_id=fid,
                order_id=oid,
                price=D(price),
                quantity=D(qty),
                fee_rate=D(fee),
                liquidity=liq,
                ts=NOW,
            )
        )

    def reopen(self) -> TradingLedger:
        self.store.close()
        self.store = LedgerStore.open(self.tmp / "data", clock=ManualClock(NOW))
        self.ledger = TradingLedger(self.store, ManualClock(NOW), COST)
        return self.ledger


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.store.close()


def trial_balance_is_zero(ledger: TradingLedger) -> None:
    totals: dict[str, Decimal] = {}
    for (_, ccy), amount in ledger.balances().items():
        totals[ccy] = totals.get(ccy, D(0)) + amount
    assert all(v == 0 for v in totals.values()), totals


class TestGoldenExamples:
    def test_42_fx_allocation(self, env: Env) -> None:
        assert env.bal(portfolio_account("treasury"), "EUR") == D(3500)
        assert env.bal(portfolio_account("fx", "EUR-USD"), "EUR") == D(1497)
        assert env.bal(portfolio_account("fx", "EUR-USD"), "USD") == -START_CASH
        assert env.bal(portfolio_account("expense", "conversion_cost"), "EUR") == D(3)
        assert env.bal(a("cash")) == START_CASH
        trial_balance_is_zero(env.ledger)

    def test_43_spot_purchase_partial_fill_then_cancel(self, env: Env) -> None:
        env.order("b1", SPOT, Side.BUY, "0.02", "82000")
        assert env.bal(a("reserved")) == D("1649.84")
        env.fill("f1", "b1", "82000", "0.005", "0.004", Liquidity.MAKER)
        assert env.bal(a("fx", "BTC-USD")) == D(410)
        assert env.bal(a("fx", "BTC-USD"), "BTC") == D("-0.005")
        assert env.bal(a("spot"), "BTC") == D("0.005")
        assert env.bal(a("expense", "trading_fee")) == D("1.64")
        assert env.bal(a("reserved")) == D("1237.38")
        assert env.ledger.order("b1").state is OrderState.PARTIALLY_FILLED
        env.ledger.close_order("b1", OrderState.CANCELLED, NOW)
        assert env.bal(a("reserved")) == 0
        assert env.bal(a("cash")) == START_CASH - D("411.64")
        assert env.ledger.lots(BOT, SPOT.instrument_id)[0].cost == D(410)
        trial_balance_is_zero(env.ledger)

    def test_44_spot_sale_fifo_realised(self, env: Env) -> None:
        env.order("b1", SPOT, Side.BUY, "0.005", "82000")
        env.fill("f1", "b1", "82000", "0.005", "0.004", Liquidity.MAKER)
        cash = env.bal(a("cash"))
        env.order("s1", SPOT, Side.SELL, "0.005", "83000")
        assert env.bal(a("spot_reserved"), "BTC") == D("0.005")
        env.fill("f2", "s1", "83000", "0.005", "0.006")
        assert env.bal(a("cash")) == cash + D(415) - D("2.49")
        assert env.bal(a("pnl", "realized_price")) == D(-5)  # credit balance = +5 income
        assert env.bal(a("fx", "BTC-USD")) == 0
        assert env.bal(a("fx", "BTC-USD"), "BTC") == 0
        assert env.bal(a("expense", "trading_fee")) == D("4.13")
        assert env.ledger.lots(BOT, SPOT.instrument_id) == ()
        trial_balance_is_zero(env.ledger)

    def test_45_derivative_margin_fees_funding_close(self, env: Env) -> None:
        env.order("l1", PERP, Side.BUY, "0.01", "82000", fee="0.0006", lev="5")
        assert env.bal(a("reserved")) == D("164.492")
        env.fill("f1", "l1", "82000", "0.01", "0.0006")
        assert env.bal(a("margin", "BTCUSD-PERP")) == D(164)
        assert env.bal(a("reserved")) == 0
        env.ledger.book_funding("fund:1", BOT, PERP.instrument_id, D("0.00001"), D(82500), NOW)
        assert env.bal(a("funding", "paid")) == D("0.00825")
        env.order("c1", PERP, Side.SELL, "0.01", "83000", fee="0.0006", reduce_only=True)
        env.fill("f2", "c1", "83000", "0.01", "0.0006")
        assert env.bal(a("pnl", "realized_price")) == D(-10)
        assert env.bal(a("margin", "BTCUSD-PERP")) == 0
        assert env.ledger.position(BOT, PERP.instrument_id) is None
        assert env.bal(a("cash")) == START_CASH - D("0.492") - D("0.00825") + D(10) - D("0.498")
        trial_balance_is_zero(env.ledger)

    def test_46_liquidation_with_bad_debt(self, env: Env) -> None:
        env.order("l1", PERP, Side.BUY, "0.01", "82000", fee="0.0006", lev="20")
        env.fill("f1", "l1", "82000", "0.01", "0.0006")
        assert env.bal(a("margin", "BTCUSD-PERP")) == D(41)
        report = env.ledger.settle_liquidation(
            "liq:1",
            BOT,
            PERP.instrument_id,
            [LiquidationExecution(price=D(77500), quantity=D("0.01"))],
            D("0.005"),
            LiquidationTrigger(
                mark=D(78300), maintenance_requirement=D("3.9150"), position_equity=D(-2), ts=NOW
            ),
        )
        assert report.execution_loss == D(45)
        assert report.liquidation_fee == D("3.875")
        assert report.insurance_absorbed == D("7.875")
        assert report.margin_returned == 0
        assert env.bal(a("sim", "insurance_absorption")) == D("-7.875")
        assert env.bal(a("expense", "liquidation_fee")) == D("3.875")
        assert env.bal(a("margin", "BTCUSD-PERP")) == 0
        assert env.bal(a("cash")) == START_CASH - D("41.492")
        assert env.ledger.bad_debt(BOT, VENUE, "USD") == D("7.875")
        trial_balance_is_zero(env.ledger)

    def test_46_liquidation_variant_without_bad_debt(self, env: Env) -> None:
        env.order("l1", PERP, Side.BUY, "0.01", "82000", fee="0.0006", lev="20")
        env.fill("f1", "l1", "82000", "0.01", "0.0006")
        report = env.ledger.settle_liquidation(
            "liq:1",
            BOT,
            PERP.instrument_id,
            [LiquidationExecution(price=D(78300), quantity=D("0.01"))],
            D("0.005"),
            LiquidationTrigger(
                mark=D(78300), maintenance_requirement=D("3.9150"), position_equity=D("3.9"), ts=NOW
            ),
        )
        assert (report.execution_loss, report.liquidation_fee) == (D(37), D("3.915"))
        assert report.margin_returned == D("0.085")
        assert report.insurance_absorbed == 0
        assert env.bal(a("cash")) == START_CASH - D("0.492") - D(41) + D("0.085")


class TestReservationsAndLimits:
    def test_cannot_reserve_more_than_free_cash(self, env: Env) -> None:
        with pytest.raises(InsufficientCapitalError):
            env.order("big", SPOT, Side.BUY, "1", "82000")
        assert env.bal(a("reserved")) == 0

    def test_cannot_sell_inventory_not_held(self, env: Env) -> None:
        with pytest.raises(InsufficientCapitalError):
            env.order("s", SPOT, Side.SELL, "0.001", "82000")

    def test_fill_beyond_remaining_refused(self, env: Env) -> None:
        env.order("b", SPOT, Side.BUY, "0.002", "82000")
        with pytest.raises(LedgerError, match="remaining"):
            env.fill("f", "b", "82000", "0.003", "0.006")

    def test_fill_on_terminal_order_refused(self, env: Env) -> None:
        env.order("b", SPOT, Side.BUY, "0.002", "82000")
        env.ledger.close_order("b", OrderState.CANCELLED, NOW)
        with pytest.raises(LedgerError, match="terminal"):
            env.fill("f", "b", "82000", "0.001", "0.006")

    def test_fill_above_reservation_is_trued_up_from_free_cash(self, env: Env) -> None:
        env.order("b", SPOT, Side.BUY, "0.002", "82000", fee="0.004")
        env.fill("f", "b", "83000", "0.002", "0.006")  # worse price and taker fee
        assert env.bal(a("reserved")) == 0
        assert env.bal(a("cash")) == START_CASH - D(166) - D("0.996")

    def test_duplicate_fill_is_idempotent_and_conflict_refused(self, env: Env) -> None:
        env.order("b", SPOT, Side.BUY, "0.002", "82000")
        env.fill("f", "b", "82000", "0.001", "0.006")
        env.fill("f", "b", "82000", "0.001", "0.006")
        assert env.ledger.order("b").filled_qty == D("0.001")
        with pytest.raises(CapitalError, match="conflict"):
            env.fill("f", "b", "82000", "0.0015", "0.006")

    def test_reduce_only_cannot_exceed_or_flip_position(self, env: Env) -> None:
        env.order("l", PERP, Side.BUY, "0.01", "82000", fee="0.0006", lev="5")
        env.fill("f", "l", "82000", "0.01", "0.0006")
        with pytest.raises(LedgerError, match="reduce"):
            env.order("c", PERP, Side.SELL, "0.02", "82000", fee="0.0006", reduce_only=True)
        env.order("c1", PERP, Side.SELL, "0.006", "82000", fee="0.0006", reduce_only=True)
        with pytest.raises(LedgerError, match="reduce"):  # pending reduce counts
            env.order("c2", PERP, Side.SELL, "0.005", "82000", fee="0.0006", reduce_only=True)
        with pytest.raises(LedgerError, match="opposite"):
            env.order("x", PERP, Side.SELL, "0.001", "82000", fee="0.0006", lev="5")

    def test_no_opposing_open_orders_on_a_flat_instrument(self, env: Env) -> None:
        env.order("l", PERP, Side.BUY, "0.001", "82000", fee="0.0006", lev="5")
        with pytest.raises(LedgerError, match="opposite"):
            env.order("s", PERP, Side.SELL, "0.001", "82000", fee="0.0006", lev="5")

    def test_partial_close_realises_pro_rata_and_weighted_average_entry(self, env: Env) -> None:
        env.order("l", PERP, Side.BUY, "0.02", "84000", fee="0.0006", lev="5")
        env.fill("f1", "l", "80000", "0.01", "0.0006")
        env.fill("f2", "l", "84000", "0.01", "0.0006")
        pos = env.ledger.position(BOT, PERP.instrument_id)
        assert pos is not None
        assert pos.avg_entry == D(82000)
        margin = env.bal(a("margin", "BTCUSD-PERP"))
        assert margin == D(160) + D(168)
        env.order("c", PERP, Side.SELL, "0.005", "81000", fee="0.0006", reduce_only=True)
        env.fill("f3", "c", "81000", "0.005", "0.0006")
        assert env.bal(a("pnl", "realized_price")) == D(5)  # loss of 5 (debit balance)
        assert env.bal(a("margin", "BTCUSD-PERP")) == (margin - 5) * D("0.75")
        pos = env.ledger.position(BOT, PERP.instrument_id)
        assert pos is not None
        assert pos.quantity == D("0.015")

    def test_funding_from_margin_when_cash_is_exhausted(self, env: Env) -> None:
        env.order("l", PERP, Side.BUY, "0.01", "82000", fee="0.0006", lev="5")
        env.fill("f", "l", "82000", "0.01", "0.0006")
        env.ledger.commit("hold-all", BOT, VENUE, "USD", env.bal(a("cash")))
        env.ledger.book_funding("fund", BOT, PERP.instrument_id, D("0.001"), D(82000), NOW)
        assert env.bal(a("margin", "BTCUSD-PERP")) == D(164) - D("0.82")

    def test_funding_received_rounds_down(self, env: Env) -> None:
        env.order("s", PERP, Side.SELL, "0.01", "82000", fee="0.0006", lev="5")
        env.fill("f", "s", "82000", "0.01", "0.0006")
        env.ledger.book_funding("fund", BOT, PERP.instrument_id, D("0.000000000123"), D(82000), NOW)
        assert env.bal(a("funding", "received")) == D("-0.00000010")

    def test_order_transitions_are_recorded(self, env: Env) -> None:
        env.order("b", SPOT, Side.BUY, "0.002", "82000")
        env.fill("f1", "b", "82000", "0.001", "0.006")
        env.ledger.request_cancel("b", NOW)
        env.fill("f2", "b", "82000", "0.001", "0.006")  # fills may arrive before the cancel
        states = [(t.from_state, t.to_state) for t in env.store.transitions()]
        assert states == [
            ("new", "accepted"),
            ("accepted", "partially_filled"),
            ("partially_filled", "cancel_requested"),
            ("cancel_requested", "filled"),
        ]


class TestDurabilityAndReplay:
    def test_restart_reproduces_state_and_replay_verifies(self, env: Env) -> None:
        env.order("b", SPOT, Side.BUY, "0.002", "82000")
        env.fill("f1", "b", "82000", "0.001", "0.004", Liquidity.MAKER)
        env.order("l", PERP, Side.BUY, "0.01", "82000", fee="0.0006", lev="5")
        env.fill("f2", "l", "82000", "0.01", "0.0006")
        env.ledger.book_funding("fund", BOT, PERP.instrument_id, D("0.00001"), D(82500), NOW)
        digest = env.ledger.state_digest()
        ledger = env.reopen()
        assert ledger.state_digest() == digest
        assert ledger.verify_replay() == []
        assert ledger.order("b").filled_qty == D("0.001")

    def test_tampered_posting_detected_by_replay(self, env: Env) -> None:
        env.order("b", SPOT, Side.BUY, "0.002", "82000")
        env.store._con.execute(
            "UPDATE ledger_postings SET amount = amount + 1 "
            "WHERE seq = (SELECT max(seq) FROM ledger_postings)"
        )
        env.store._con.execute(
            "UPDATE ledger_postings SET amount = amount - 1 "
            "WHERE seq = (SELECT max(seq) - 1 FROM ledger_postings)"
        )
        assert env.reopen().verify_replay() != []

    def test_multi_operation_unit_is_atomic(self, env: Env) -> None:
        before = env.ledger.state_digest()

        def two_holds_in_one_unit() -> None:
            with env.store.unit_of_work():
                env.ledger.commit("h1", BOT, VENUE, "USD", D(1000))
                env.ledger.commit("h2", BOT, VENUE, "USD", D(1000))  # sees h1: insufficient

        with pytest.raises(InsufficientCapitalError):
            two_holds_in_one_unit()
        assert env.ledger.state_digest() == before
        assert env.reopen().state_digest() == before

    def test_concurrent_orders_never_overspend(self, env: Env) -> None:
        def worker(i: int) -> None:
            for j in range(5):
                with contextlib.suppress(InsufficientCapitalError):
                    env.order(f"o{i}-{j}", SPOT, Side.BUY, "0.002", "82000")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert env.bal(a("cash")) >= 0
        assert env.bal(a("reserved")) == D(164) * D("1.006") * len(env.ledger.orders())
        trial_balance_is_zero(env.ledger)
