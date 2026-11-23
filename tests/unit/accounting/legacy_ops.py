"""Historical ledger operations that production code can no longer create (tests only).

Discretionary profit reserves are not supported and
`TradingLedger.transfer_to_reserve` refuses new bookings. Ledgers written before that may still
contain `reserve` operations, which must keep replaying: this helper books one exactly
as the removed code path did, through the same planner.
"""

from decimal import Decimal

from shadow_trading_suite.accounting.journal import up
from shadow_trading_suite.accounting.trading import AccountKey, TradingLedger


def book_legacy_reserve(
    ledger: TradingLedger,
    op_id: str,
    debits: dict[AccountKey, Decimal],
    eur_per_unit: Decimal,
    credit_eur: Decimal | None = None,
) -> Decimal:
    payload = {
        "debits": sorted([b, v.value, c, up(a)] for (b, v, c), a in debits.items()),
        "rate": eur_per_unit,
        "credit": credit_eur,
        "cost": ledger._cost,
    }
    return Decimal(ledger._run(op_id, "reserve", payload))
