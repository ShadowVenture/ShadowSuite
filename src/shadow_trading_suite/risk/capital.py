"""Central paper-capital manager (M5 API) over the double-entry trading ledger (M6-A).

`CapitalManager` keeps the M5 interface — accounts per (bot, venue, currency) with balance,
committed and free = balance - committed; treasury, a historical profit reserve (RETIRED: new
reserves are refused: not supported by the suite) and conversion
costs — but
its state is now the journal projection in `accounting.trading`: every change is an atomic,
idempotent, replayable operation; two concurrent requests can never spend the same free cash.
No real funds exist here.
"""

from shadow_trading_suite.accounting.trading import (
    AMOUNT_QUANTUM,
    Account,
    AccountKey,
    CapitalError,
    InsufficientCapitalError,
    TradingLedger,
)

__all__ = [
    "AMOUNT_QUANTUM",
    "Account",
    "AccountKey",
    "CapitalError",
    "CapitalManager",
    "InsufficientCapitalError",
]


class CapitalManager(TradingLedger):
    """The portfolio's single capital authority (one instance per ledger store)."""
