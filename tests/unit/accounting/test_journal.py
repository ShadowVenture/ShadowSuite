"""Double-entry journal: balanced per currency, quantised, named accounts."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from shadow_trading_suite.accounting.journal import (
    JournalEntry,
    Posting,
    apply_entry,
    bot_account,
    down,
    portfolio_account,
    up,
)
from shadow_trading_suite.core.instrument import Venue

D = Decimal
CASH = bot_account("bot-03", Venue.CRYPTOCOM, "cash")
RESERVED = bot_account("bot-03", Venue.CRYPTOCOM, "reserved")


def p(account: str, ccy: str, amount: str) -> Posting:
    return Posting(account=account, currency=ccy, amount=D(amount))


def test_account_names_are_structured() -> None:
    assert CASH == "bot-03@cryptocom:cash"
    assert bot_account("bot-03", Venue.CRYPTOCOM, "fx", "BTC-USD") == "bot-03@cryptocom:fx:BTC-USD"
    assert portfolio_account("treasury") == "p:treasury"
    with pytest.raises(ValueError, match="account"):
        bot_account("CB 03", Venue.CRYPTOCOM, "cash")


def test_entry_must_balance_per_currency() -> None:
    JournalEntry(memo="hold", postings=(p(RESERVED, "USD", "10"), p(CASH, "USD", "-10")))
    with pytest.raises(ValidationError, match="balance"):
        JournalEntry(memo="x", postings=(p(RESERVED, "USD", "10"), p(CASH, "USD", "-9.99")))


def test_currencies_never_net_against_each_other() -> None:
    with pytest.raises(ValidationError, match="balance"):
        JournalEntry(memo="x", postings=(p(RESERVED, "USD", "10"), p(CASH, "EUR", "-10")))


@pytest.mark.parametrize("amount", ["0", "0.000000001"])
def test_postings_are_nonzero_and_quantised(amount: str) -> None:
    with pytest.raises(ValidationError):
        p(CASH, "USD", amount)


def test_float_amounts_rejected() -> None:
    with pytest.raises(ValidationError):
        Posting(account=CASH, currency="USD", amount=1.5)  # type: ignore[arg-type]


def test_entry_needs_two_postings() -> None:
    with pytest.raises(ValidationError):
        JournalEntry(memo="x", postings=(p(CASH, "USD", "1"),))


def test_apply_entry_updates_balances() -> None:
    balances: dict[tuple[str, str], Decimal] = {}
    apply_entry(
        balances, JournalEntry(memo="h", postings=(p(RESERVED, "USD", "10"), p(CASH, "USD", "-10")))
    )
    assert balances == {(RESERVED, "USD"): D(10), (CASH, "USD"): D(-10)}


def test_rounding_helpers_are_directional() -> None:
    assert up(D("1.000000001")) == D("1.00000001")
    assert down(D("1.000000019")) == D("1.00000001")
    assert up(D("-1.000000001")) == D("-1.00000000")
