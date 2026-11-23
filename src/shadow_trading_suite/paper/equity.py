"""Durable start-of-day and peak equity snapshots for the M5 loss limits (M6-C, X6).

The daily risk-accounting boundary is 00:00 UTC. The first valuation of a UTC day records that
day's start (per bot and for the portfolio); every valuation above the recorded peak records a
new peak. Peaks never reset (unchanged M5 drawdown semantics). Values are rounded UP to the
cent, so the daily loss and drawdown are never understated, and a peak is recorded at most once
per cent of new high. Snapshots are hash-chained audit records in the ledger store (one unit of
work per observation) and are restored exactly on restart. If the process is down at 00:00, the
day starts at the first valuation after the restart (recorded with its time).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from shadow_trading_suite.core.time import ensure_utc
from shadow_trading_suite.storage.ledger import LedgerStore

PORTFOLIO = "portfolio"
DAY_START = "equity.day_start"
PEAK = "equity.peak"
CENT = Decimal("0.01")


def cents_up(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_CEILING)


def utc_day(at: datetime) -> str:
    return ensure_utc(at).date().isoformat()


@dataclass(frozen=True)
class EquityMarks:
    day: str
    start_of_day: Decimal
    peak: Decimal


class EquitySnapshots:
    def __init__(self, store: LedgerStore) -> None:
        self._store = store
        self._marks: dict[str, EquityMarks] = {}
        for rec in store.audit_records():
            if rec.kind not in (DAY_START, PEAK) or rec.subject is None:
                continue
            p, key = rec.payload, rec.subject
            old = self._marks.get(key)
            value = Decimal(p["equity_eur"])
            if rec.kind == DAY_START:
                peak = old.peak if old else value
                self._marks[key] = EquityMarks(p["day"], value, peak)
            elif old is not None:
                self._marks[key] = EquityMarks(old.day, old.start_of_day, value)

    def current(self) -> dict[str, EquityMarks]:
        return dict(self._marks)

    def observe(self, now: datetime, values: dict[str, Decimal]) -> dict[str, EquityMarks]:
        """Record what the valuation at `now` changes; returns the marks for every key."""
        day = utc_day(now)
        for key, value in values.items():
            if value <= 0:
                raise ValueError(f"{key}: equity {value} is not positive; no snapshot taken")
        updated = dict(self._marks)
        with self._store.unit_of_work() as uow:
            for key in sorted(values):
                value = cents_up(values[key])
                old = updated.get(key)
                if old is None or old.day != day:
                    peak = max(old.peak, value) if old else value
                    uow.add_audit(
                        DAY_START, key, {"day": day, "equity_eur": value, "at": now.isoformat()}
                    )
                    if old is not None and value > old.peak:
                        uow.add_audit(PEAK, key, {"equity_eur": value, "at": now.isoformat()})
                    updated[key] = EquityMarks(day, value, peak)
                elif value > old.peak:
                    uow.add_audit(PEAK, key, {"equity_eur": value, "at": now.isoformat()})
                    updated[key] = EquityMarks(old.day, old.start_of_day, value)
            uow.on_commit(lambda: setattr(self, "_marks", updated))
        return {k: updated[k] for k in values}
