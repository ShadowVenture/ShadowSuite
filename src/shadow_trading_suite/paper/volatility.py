"""Volatility input for the M5 liquidation buffer (sigma over the configured horizon).

Estimator `minute-close-v1` (ASSUMPTION, a simulation parameter, not venue data): the last
public trade price of each UTC minute (by exchange timestamp) is that minute's close; returns
between consecutive closes are normalised per minute (divided by sqrt of the gap in minutes);
sigma = sample standard deviation of the last `window` returns x sqrt(horizon minutes).
Nothing is interpolated: a trade gap longer than `max_gap_minutes` restarts the window, and
fewer than `min_returns` returns, or no trade within `max_gap_minutes`, fail closed. The tracker
is a pure fold over the recorded events, so the same events always give the same sigma.
After a restart the window may be seeded from closed, quality-checked public 1-minute candles
(the same "last trade of the minute" close), recorded in the audit as `paper.volatility_seed`.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import InstrumentId
from shadow_trading_suite.core.market import Trade
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind

ESTIMATOR = "minute-close-v1 (ASSUMPTION)"
MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class VolatilityPolicy:
    horizon_minutes: int = 60  # = MarginAssumptions.volatility_horizon_minutes
    min_returns: int = 30
    window: int = 120
    max_gap_minutes: int = 3


@dataclass
class _Series:
    bucket: datetime  # the open minute
    price: Decimal | None  # last trade in the open minute (None: no trade yet)
    last_ts: datetime
    closes: deque[tuple[datetime, Decimal]] = field(default_factory=deque)
    returns: deque[Decimal] = field(default_factory=deque)


def _minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


class VolatilityTracker:
    def __init__(self, policy: VolatilityPolicy | None = None) -> None:
        self.policy = policy or VolatilityPolicy()
        self._series: dict[InstrumentId, _Series] = {}

    def observe(self, event: StreamEvent) -> None:
        trade = event.payload
        if event.kind is not StreamKind.TRADES or not isinstance(trade, Trade):
            return
        ts, iid = event.exchange_ts, event.instrument_id
        minute = _minute(ts)
        s = self._series.get(iid)
        if s is None:
            self._series[iid] = _Series(minute, trade.price, ts)
            return
        if ts < s.last_ts:
            return  # out of order: never rewrites a close
        if minute > s.bucket:
            if s.price is not None:
                self._close(s, s.bucket, s.price)
            last = s.closes[-1][0] if s.closes else s.bucket
            if minute - last > self.policy.max_gap_minutes * MINUTE:
                s.closes.clear()  # restart: a gap is never bridged
                s.returns.clear()
            s.bucket = minute
        s.price, s.last_ts = trade.price, ts

    def seed(self, iid: InstrumentId, closes: list[tuple[datetime, Decimal]]) -> int:
        """Start the window from CLOSED public 1-minute candle closes (validated by the
        caller) unless the live window is already warm. The live open minute is kept.
        Returns the number of returns seeded."""
        existing = self._series.get(iid)
        if not closes or (
            existing is not None and len(existing.returns) >= self.policy.min_returns
        ):
            return 0
        ordered = sorted(closes)
        last_minute = ordered[-1][0]
        s = _Series(last_minute + MINUTE, None, last_minute + MINUTE)
        for minute, price in ordered:
            if s.closes and minute - s.closes[-1][0] > self.policy.max_gap_minutes * MINUTE:
                s.closes.clear()
                s.returns.clear()
            self._close(s, minute, price)
        if existing is not None and existing.bucket >= s.bucket:
            s.bucket, s.price, s.last_ts = existing.bucket, existing.price, existing.last_ts
        self._series[iid] = s
        return len(s.returns)

    def _close(self, s: _Series, minute: datetime, price: Decimal) -> None:
        if s.closes:
            prev_minute, prev = s.closes[-1]
            gap = (minute - prev_minute) // MINUTE
            ret = price / prev - 1
            s.returns.append(ret if gap == 1 else ret / Decimal(gap).sqrt())
            while len(s.returns) > self.policy.window:
                s.returns.popleft()
        s.closes.append((minute, price))
        while len(s.closes) > self.policy.window + 1:
            s.closes.popleft()

    def sigma(self, iid: InstrumentId, now: datetime) -> Decimal:
        s = self._series.get(iid)
        if s is None:
            raise DataQualityError(f"{iid}: no trades observed for volatility")
        if now - s.last_ts > self.policy.max_gap_minutes * MINUTE:
            raise DataQualityError(f"{iid}: volatility stale (no trade since {s.last_ts})")
        n = len(s.returns)
        if n < self.policy.min_returns:
            raise DataQualityError(
                f"{iid}: volatility warm-up ({n}/{self.policy.min_returns} minute returns)"
            )
        mean = sum(s.returns, Decimal(0)) / n
        var = sum(((r - mean) ** 2 for r in s.returns), Decimal(0)) / (n - 1)
        if var == 0:
            raise DataQualityError(f"{iid}: no price variation observed (sigma 0 is not evidence)")
        return var.sqrt() * Decimal(self.policy.horizon_minutes).sqrt()
