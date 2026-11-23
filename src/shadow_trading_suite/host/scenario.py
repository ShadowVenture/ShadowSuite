"""Offline scenarios: drive a `PaperHost` with recorded or scripted public market events.

A scenario is a JSON document (no network, deterministic, injected clock):

```json
{
  "start": "2026-01-01T00:00:00+00:00",
  "instruments": {"coinbase": [<raw public product rows>], "cryptocom": [<raw instrument rows>]},
  "events": [
    {"at_ms": 0, "type": "live", "instrument": "coinbase:BTC-USD", "streams": ["book", "trades"]},
    {"at_ms": 5, "type": "ticker", "instrument": "coinbase:BTC-EUR",
     "bid": "70000", "ask": "70010"},
    {"at_ms": 10, "type": "book", "instrument": "coinbase:BTC-USD",
     "bids": [["82000", "1"]], "asks": [["82001", "1"]]},
    {"at_ms": 20, "type": "trade", "instrument": "coinbase:BTC-USD",
     "price": "82000.5", "quantity": "0.1", "side": "buy", "trade_id": "t1"},
    {"at_ms": 30, "type": "down", "instrument": "coinbase:BTC-USD", "streams": ["book"]},
    {"at_ms": 40, "type": "tick"},
    {"at_ms": 50, "type": "stop_plugin", "plugin": "my-bot"}
  ]
}
```

Instruments are parsed by the same public-data parsers as live discovery. Prices and
quantities are strings (exact decimals). Events must be in non-decreasing `at_ms` order.
"""

import json
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shadow_trading_suite.core.instrument import Instrument, InstrumentId, Venue
from shadow_trading_suite.core.market import BookLevel, OrderBookSnapshot, Ticker, Trade
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.exchanges.coinbase.adapter import parse_product
from shadow_trading_suite.exchanges.cryptocom.adapter import parse_instrument
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind
from shadow_trading_suite.streaming.state import StreamKey, StreamStatus

if TYPE_CHECKING:
    from shadow_trading_suite.host.paper import PaperHost


class ScenarioError(ValueError):
    pass


def _iid(text: str) -> InstrumentId:
    venue, _, symbol = text.partition(":")
    try:
        return InstrumentId(venue=Venue(venue), symbol=symbol)
    except ValueError as exc:
        raise ScenarioError(f"bad instrument {text!r} (expected venue:symbol)") from exc


def _levels(rows: Any) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in rows)


class Scenario:
    def __init__(self, doc: Mapping[str, Any]) -> None:
        try:
            self.start = datetime.fromisoformat(str(doc["start"]))
        except (KeyError, ValueError) as exc:
            raise ScenarioError("scenario needs an ISO `start` with a UTC offset") from exc
        if self.start.tzinfo is None:
            raise ScenarioError("scenario `start` must be timezone-aware")
        raw = doc.get("instruments", {})
        parsed: list[Instrument] = [parse_product(r) for r in raw.get("coinbase", [])]
        parsed += [parse_instrument(r) for r in raw.get("cryptocom", [])]
        self.instruments = {i.id: i for i in parsed}
        self.events = list(doc.get("events", []))
        last = 0
        for e in self.events:
            at = int(e.get("at_ms", 0))
            if at < last:
                raise ScenarioError("events must be ordered by at_ms")
            last = at

    @classmethod
    def load(cls, path: Path) -> "Scenario":
        return cls(json.loads(path.read_text("utf-8")))

    def clock(self) -> ManualClock:
        return ManualClock(self.start)

    def play(
        self,
        host: "PaperHost",
        clock: ManualClock,
        on_step: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> int:
        """Apply every event to the host; returns the number of events applied."""
        n = 0
        for i, step in enumerate(self._steps()):
            at = self.start + timedelta(milliseconds=int(step.get("at_ms", 0)))
            if at > clock.now():
                clock.set(at)
            self._apply(host, step, i)
            n += 1
            if on_step is not None:
                on_step(step)
        return n

    def _steps(self) -> Iterator[Mapping[str, Any]]:
        yield from self.events

    def _apply(self, host: "PaperHost", step: Mapping[str, Any], i: int) -> None:
        kind = step.get("type")
        now = host.now()
        if kind in ("live", "down"):
            status = StreamStatus.LIVE if kind == "live" else StreamStatus.DOWN
            iid = _iid(str(step["instrument"]))
            for s in step.get("streams", ["book", "trades"]):
                host.set_stream_status(StreamKey(iid, StreamKind(s)), status)
            return
        if kind == "tick":
            host.tick()
            return
        if kind == "stop_plugin":
            host.stop_plugin(str(step["plugin"]))
            return
        iid = _iid(str(step["instrument"]))
        payload: Any
        if kind == "book":
            payload = OrderBookSnapshot(
                instrument_id=iid, ts=now, bids=_levels(step["bids"]), asks=_levels(step["asks"])
            )
            stream = StreamKind.BOOK
        elif kind == "trade":
            side = step.get("side")
            payload = Trade(
                instrument_id=iid,
                trade_id=str(step["trade_id"]),
                ts=now,
                price=Decimal(str(step["price"])),
                quantity=Decimal(str(step["quantity"])),
                aggressor=Side(side) if side else None,
            )
            stream = StreamKind.TRADES
        elif kind == "ticker":
            payload = Ticker(
                instrument_id=iid,
                ts=now,
                bid=Decimal(str(step["bid"])),
                ask=Decimal(str(step["ask"])),
            )
            stream = StreamKind.TICKER
        else:
            raise ScenarioError(f"unknown scenario event type {kind!r}")
        host.on_event(
            StreamEvent(
                event_id=f"{iid.venue.value}:{stream.value}:{iid.symbol}:scenario:{i}",
                kind=stream,
                instrument_id=iid,
                exchange_ts=now,
                received_at=now,
                epoch=1,
                payload=payload,
            )
        )
