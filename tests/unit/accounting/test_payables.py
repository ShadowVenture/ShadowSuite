"""B5: funding that cash and margin cannot cover stays an outstanding internal liability.

Independent numerical examples (exact amounts, computed by hand in the comments) cover:
sufficient funding balance, partial shortfall, complete shortfall, subsequent cash inflow,
partial repayment and full repayment. Every step checks the signed-balance conventions
(debit +, credit -; a payable is credit-normal, so its balance is <= 0), the trial balance,
the income identity and the ledger / economic equity equations.
"""

from collections import defaultdict
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.accounting.journal import bot_account
from shadow_trading_suite.accounting.reporting import value_bot
from shadow_trading_suite.accounting.trading import LedgerError
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.storage.ledger import LedgerStore
from tests.unit.accounting.test_trading import BOT, NOW, PERP, RATE, START_CASH, VENUE, Env, a

D = Decimal
RATES = {"USD": RATE}
MARKS = {PERP.instrument_id: D(82000)}
PAYABLE = a("payable", "funding:BTCUSD-PERP")
OTHER, OTHER_VENUE = "bot-04", VENUE


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    e.ledger.allocate("alloc-other", OTHER, OTHER_VENUE, "USD", D(1500), RATE)
    # long 0.01 BTCUSD-PERP at 82,000, 5x: margin 164.00, open fee 0.492 (§4.5)
    e.order("l1", PERP, Side.BUY, "0.01", "82000", fee="0.0006", lev="5")
    e.fill("f1", "l1", "82000", "0.01", "0.0006")
    yield e
    e.store.close()


def fund(env: Env, op: str, rate: str) -> None:
    env.ledger.book_funding(op, BOT, PERP.instrument_id, D(rate), D(82000), NOW)


def lock_cash(env: Env, ref: str, leave: str) -> Decimal:
    """Move all but `leave` USD of free cash into a hold (models cash that is not free)."""
    amount = env.bal(a("cash")) - D(leave)
    env.ledger.commit(ref, BOT, VENUE, "USD", amount)
    return amount


def check_identities(env: Env, funding_paid: Decimal) -> None:
    ledger = env.ledger
    # 1) every currency's trial balance is zero
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for (_, ccy), bal in ledger.balances().items():
        totals[ccy] += bal
    assert all(v == 0 for v in totals.values()), totals
    # 2) sign conventions: holdings >= 0, the payable <= 0
    for kind in ("cash", "reserved"):
        assert env.bal(a(kind)) >= 0
    assert env.bal(a("margin", "BTCUSD-PERP")) >= 0
    assert env.bal(PAYABLE) <= 0
    # 3) income identity (one account family per term): change of the bot's monetary assets
    #    plus its liabilities = - fees - funding incurred (paid or unpaid; counted once)
    assets = env.bal(a("cash")) + env.bal(a("reserved")) + env.bal(a("margin", "BTCUSD-PERP"))
    fees = env.bal(a("expense", "trading_fee"))
    assert env.bal(a("funding", "paid")) == funding_paid
    assert assets + env.bal(PAYABLE) - START_CASH == -fees - funding_paid
    # 4) equity equations (mark = entry, so unrealised PnL is 0); liability counted exactly once
    v = value_bot(ledger, BOT, VENUE, MARKS, RATES).currency("USD")
    assert v.payables == -env.bal(PAYABLE)
    assert v.ledger_equity == assets - v.payables
    assert v.economic_equity == v.ledger_equity - v.insurance_absorbed
    assert v.ledger_equity == START_CASH - fees - funding_paid


def test_sufficient_cash_pays_funding_without_a_liability(env: Env) -> None:
    fund(env, "fund-1", "0.0001")  # 0.01 x 82,000 x 0.0001 = 0.082 from cash
    assert env.bal(a("cash")) == START_CASH - D("164.492") - D("0.082")
    assert env.bal(a("margin", "BTCUSD-PERP")) == D(164)
    assert env.bal(PAYABLE) == 0
    check_identities(env, D("0.082"))


def test_shortfall_sequence_partial_complete_inflow_and_repayment(env: Env) -> None:
    # free cash after the open: 1,740.69767441 - 164.492 = 1,576.20567441
    small = lock_cash(env, "h-big", "20.05")  # 1,556.15567441 held, 20.05 free
    env.ledger.commit("h-small", BOT, VENUE, "USD", D(20))  # 0.05 free
    assert small == D("1556.15567441")
    assert env.bal(a("cash")) == D("0.05")

    # partial shortfall: 0.01 x 82,000 x 0.25 = 205.00 = 0.05 cash + 164.00 margin + 40.95 unpaid
    fund(env, "fund-1", "0.25")
    assert env.bal(a("cash")) == 0
    assert env.bal(a("margin", "BTCUSD-PERP")) == 0
    assert env.bal(PAYABLE) == D("-40.95")
    check_identities(env, D(205))

    # complete shortfall: 0.01 x 82,000 x 0.01 = 8.20, nothing available -> all unpaid
    fund(env, "fund-2", "0.01")
    assert env.bal(PAYABLE) == D("-49.15")
    check_identities(env, D("213.20"))
    equity = value_bot(env.ledger, BOT, VENUE, MARKS, RATES).currency("USD").ledger_equity
    assert equity == START_CASH - D("0.492") - D("213.20")  # 1,527.00567441

    # nothing to pay from yet
    with pytest.raises(LedgerError, match="nothing to settle"):
        env.ledger.settle_payable("pay-0", BOT, VENUE, "USD")

    # subsequent cash inflow of 20.00 (hold released) -> partial repayment 20.00
    env.ledger.release("h-small")
    assert env.bal(a("cash")) == D(20)
    check_identities(env, D("213.20"))  # an inflow from reserved changes no equity
    assert env.ledger.settle_payable("pay-1", BOT, VENUE, "USD") == D(20)
    assert (env.bal(a("cash")), env.bal(PAYABLE)) == (D(0), D("-29.15"))
    check_identities(env, D("213.20"))  # settlement: asset -20, liability +20, equity unchanged

    # larger inflow -> full repayment of the remaining 29.15
    env.ledger.release("h-big")
    assert env.ledger.settle_payable("pay-2", BOT, VENUE, "USD") == D("29.15")
    assert env.bal(PAYABLE) == 0
    assert env.bal(a("cash")) == D("1556.15567441") - D("29.15")
    check_identities(env, D("213.20"))
    after = value_bot(env.ledger, BOT, VENUE, MARKS, RATES).currency("USD").ledger_equity
    assert after == equity  # repayment never changes equity: the liability was counted once
    assert env.ledger.verify_replay() == []


def test_settlement_never_uses_another_bots_capital(env: Env) -> None:
    other_cash = env.ledger.balance(bot_account(OTHER, OTHER_VENUE, "cash"), "USD")
    lock_cash(env, "h", "0")
    fund(env, "fund-1", "0.25")  # 205 = 164 margin + 41 unpaid
    assert env.bal(PAYABLE) == D(-41)
    with pytest.raises(LedgerError, match="nothing to settle"):
        env.ledger.settle_payable("pay-1", BOT, VENUE, "USD")
    with pytest.raises(LedgerError, match="nothing to settle"):  # bot-04 has no liability
        env.ledger.settle_payable("pay-x", OTHER, OTHER_VENUE, "USD")
    assert env.ledger.balance(bot_account(OTHER, OTHER_VENUE, "cash"), "USD") == other_cash
    assert env.ledger.treasury_eur == D(2000)
    assert env.bal(PAYABLE) == D(-41)


def test_no_duplicate_charge_or_settlement_after_replay_and_restart(env: Env) -> None:
    lock_cash(env, "h", "0")
    fund(env, "fund-1", "0.25")
    fund(env, "fund-1", "0.25")  # same op id: no-op
    assert env.bal(a("funding", "paid")) == D(205)
    env.ledger.release("h")
    paid = env.ledger.settle_payable("pay-1", BOT, VENUE, "USD")
    assert paid == D(41)
    ledger = env.reopen()
    assert ledger.settle_payable("pay-1", BOT, VENUE, "USD") == D(41)  # stored result, no-op
    ledger.book_funding("fund-1", BOT, PERP.instrument_id, D("0.25"), D(82000), NOW)
    assert ledger.balance(a("funding", "paid"), "USD") == D(205)
    assert ledger.balance(PAYABLE, "USD") == 0
    assert ledger.verify_replay() == []
    with pytest.raises(LedgerError, match="nothing to settle"):
        ledger.settle_payable("pay-2", BOT, VENUE, "USD")


def test_crash_between_charge_and_settlement_keeps_the_liability(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_cash(env, "h", "0")
    fund(env, "fund-1", "0.25")
    env.ledger.release("h")

    def boom(self: LedgerStore) -> None:
        raise RuntimeError("crash at COMMIT")

    monkeypatch.setattr(LedgerStore, "_commit", boom)
    with pytest.raises(RuntimeError):
        env.ledger.settle_payable("pay-1", BOT, VENUE, "USD")
    monkeypatch.undo()
    ledger = env.reopen()
    assert ledger.balance(PAYABLE, "USD") == D(-41)  # still outstanding, nothing half-booked
    assert ledger.settle_payable("pay-1", BOT, VENUE, "USD") == D(41)
    assert ledger.verify_replay() == []


def test_funding_received_does_not_touch_the_liability(env: Env) -> None:
    lock_cash(env, "h", "0")
    fund(env, "fund-1", "0.25")
    fund(env, "fund-2", "-0.001")  # long receives 0.82 into cash
    assert env.bal(a("cash")) == D("0.82")
    assert env.bal(PAYABLE) == D(-41)
    assert env.ledger.settle_payable("pay-1", BOT, VENUE, "USD") == D("0.82")
    assert env.bal(PAYABLE) == D("-40.18")
