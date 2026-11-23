"""Double-entry journal primitives (paper only).

Sign convention: a posting's amount is positive for a debit and negative for a credit. Every
journal entry balances PER CURRENCY (no cross-currency netting); currencies meet only in
`fx:*` trading accounts, which therefore carry the cost basis. Amounts are Decimals quantised
to 1e-8 and never zero.

Account names: `{bot}@{venue}:{kind}[:{detail}]` for bot accounts and `p:{kind}[:{detail}]`
for portfolio accounts.
"""

import re
from collections import defaultdict
from collections.abc import MutableMapping
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Self

from pydantic import Field, field_validator, model_validator

from shadow_trading_suite.core.decimal import Dec
from shadow_trading_suite.core.instrument import Currency, Venue
from shadow_trading_suite.core.model import DomainModel

QUANTUM = Decimal("1e-8")
_SEGMENT = r"[A-Za-z0-9_\-]+"
ACCOUNT_PATTERN = re.compile(rf"^(p|[a-z0-9][a-z0-9_-]{{0,63}}@[a-z]+)(:{_SEGMENT})+$")
Balances = MutableMapping[tuple[str, str], Decimal]

# CLOSED set of account kinds. Every account is internal to the paper ledger: there is no
# external, wallet, payout or destination account, so no entry can represent funds leaving.
ALLOWED_KINDS = frozenset(
    {
        "cash",
        "reserved",
        "spot",
        "spot_reserved",
        "margin",
        "fx",
        "pnl",
        "expense",
        "funding",
        "sim",
        "treasury",
        "reserve",
        "equity",
        "payable",  # internal liability (unpaid simulated funding), never an external account
    }
)

# Accounts whose balance may never be negative (debit-normal holdings).
NON_NEGATIVE_KINDS = frozenset(
    {"cash", "reserved", "spot", "spot_reserved", "margin", "treasury", "reserve"}
)

# Liabilities (credit-normal): their balance may never be positive.
NON_POSITIVE_KINDS = frozenset({"payable"})


def up(value: Decimal) -> Decimal:
    """Round towards +infinity at 1e-8 (amounts charged to a bot)."""
    return value.quantize(QUANTUM, ROUND_CEILING)


def down(value: Decimal) -> Decimal:
    """Round towards -infinity at 1e-8 (amounts credited to a bot)."""
    return value.quantize(QUANTUM, ROUND_FLOOR)


def _checked(name: str) -> str:
    if not ACCOUNT_PATTERN.fullmatch(name):
        raise ValueError(f"invalid account name {name!r}")
    if name.split(":")[1] not in ALLOWED_KINDS:
        raise ValueError(f"account kind {name.split(':')[1]!r} is not an internal ledger account")
    return name


def bot_account(bot_id: str, venue: Venue, kind: str, detail: str | None = None) -> str:
    return _checked(f"{bot_id}@{venue.value}:{kind}" + (f":{detail}" if detail else ""))


def portfolio_account(kind: str, detail: str | None = None) -> str:
    return _checked(f"p:{kind}" + (f":{detail}" if detail else ""))


def account_kind(name: str) -> str:
    return name.split(":")[1]


class Posting(DomainModel):
    account: str
    currency: Currency
    amount: Dec

    @field_validator("account")
    @classmethod
    def _name(cls, value: str) -> str:
        return _checked(value)

    @field_validator("amount")
    @classmethod
    def _quantised(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("posting amount must be non-zero")
        if value != value.quantize(QUANTUM):
            raise ValueError(f"posting amount {value} is not quantised to {QUANTUM}")
        return value

    def row(self) -> tuple[str, str, Decimal]:
        return (self.account, self.currency, self.amount)


class JournalEntry(DomainModel):
    memo: str = Field(min_length=1)
    postings: tuple[Posting, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _balanced(self) -> Self:
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for posting in self.postings:
            totals[posting.currency] += posting.amount
        unbalanced = {c: t for c, t in totals.items() if t != 0}
        if unbalanced:
            raise ValueError(f"entry {self.memo!r} does not balance per currency: {unbalanced}")
        return self

    def rows(self) -> tuple[tuple[str, str, Decimal], ...]:
        return tuple(p.row() for p in self.postings)


def apply_entry(balances: Balances, entry: JournalEntry) -> None:
    for posting in entry.postings:
        key = (posting.account, posting.currency)
        balances[key] = balances.get(key, Decimal(0)) + posting.amount
