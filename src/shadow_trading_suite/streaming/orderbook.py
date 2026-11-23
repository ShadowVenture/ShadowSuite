"""Local order-book reconstruction from a snapshot plus incremental deltas.

The book is only usable while `synced`. A sequence gap, a crossed result or a delta before
any snapshot desynchronises it; it stays unusable until a fresh snapshot is applied.

Each side keeps a sorted price index next to its level map, so the best price and the top-N
levels cost O(depth) per update instead of a scan of the whole book (Coinbase level2 books
hold thousands of levels and update many times per second on one shared event loop).
"""

from bisect import bisect_left, insort
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from shadow_trading_suite.core.instrument import InstrumentId
from shadow_trading_suite.core.market import BookLevel, OrderBookSnapshot

Level = tuple[Decimal, Decimal]
Change = tuple[str, Decimal, Decimal]  # (side "bid" | "ask", price, new quantity; 0 = delete)


class BookSyncError(Exception):
    """The local book cannot be trusted; a fresh snapshot is required."""


class LocalOrderBook:
    def __init__(self, instrument_id: InstrumentId) -> None:
        self.instrument_id = instrument_id
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._bid_prices: list[Decimal] = []  # ascending; best bid last
        self._ask_prices: list[Decimal] = []  # ascending; best ask first
        self._levels: dict[tuple[str, Decimal], BookLevel] = {}  # validated, until it changes
        self.synced = False
        self.sequence: int | None = None
        self.ts: datetime | None = None

    def invalidate(self) -> None:
        self.synced = False
        self._bids.clear()
        self._asks.clear()
        self._bid_prices.clear()
        self._ask_prices.clear()
        self._levels.clear()

    def _check_uncrossed(self) -> None:
        if self._bids and self._asks and self._bid_prices[-1] >= self._ask_prices[0]:
            self.invalidate()
            raise BookSyncError(f"{self.instrument_id}: book crossed or locked")

    def apply_snapshot(
        self,
        bids: Iterable[Level],
        asks: Iterable[Level],
        ts: datetime,
        sequence: int | None = None,
    ) -> None:
        self._bids = {p: q for p, q in bids if q > 0}
        self._asks = {p: q for p, q in asks if q > 0}
        self._bid_prices = sorted(self._bids)
        self._ask_prices = sorted(self._asks)
        self._levels.clear()
        self.synced = True
        self._check_uncrossed()
        self.sequence, self.ts = sequence, ts

    def apply_delta(
        self,
        changes: Iterable[Change],
        ts: datetime,
        sequence: int | None = None,
        previous: int | None = None,
    ) -> None:
        if not self.synced:
            raise BookSyncError(f"{self.instrument_id}: delta received while not synced")
        if previous is not None and previous != self.sequence:
            self.invalidate()
            raise BookSyncError(
                f"{self.instrument_id}: sequence gap (expected previous {self.sequence}, "
                f"got {previous})"
            )
        for side, price, quantity in changes:
            if side == "bid":
                levels, index = self._bids, self._bid_prices
            elif side == "ask":
                levels, index = self._asks, self._ask_prices
            else:
                self.invalidate()
                raise BookSyncError(f"{self.instrument_id}: unknown side {side!r}")
            self._levels.pop((side, price), None)
            if quantity == 0:
                if levels.pop(price, None) is not None:
                    del index[bisect_left(index, price)]
            else:
                if price not in levels:
                    insort(index, price)
                levels[price] = quantity
        self._check_uncrossed()
        self.sequence, self.ts = sequence if sequence is not None else self.sequence, ts

    def top(self, depth: int) -> OrderBookSnapshot:
        if not self.synced or self.ts is None:
            raise BookSyncError(f"{self.instrument_id}: book not synced")
        n = max(depth, 0)
        return OrderBookSnapshot(
            instrument_id=self.instrument_id,
            ts=self.ts,
            sequence=self.sequence,
            bids=tuple(
                self._level("bid", p, self._bids)
                for p in reversed(self._bid_prices[max(len(self._bid_prices) - n, 0) :])
            ),
            asks=tuple(self._level("ask", p, self._asks) for p in self._ask_prices[:n]),
        )

    def _level(self, side: str, price: Decimal, levels: dict[Decimal, Decimal]) -> BookLevel:
        level = self._levels.get((side, price))
        if level is None:
            level = self._levels[(side, price)] = BookLevel(price=price, quantity=levels[price])
        return level
