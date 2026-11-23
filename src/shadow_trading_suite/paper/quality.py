"""Market-data quality report for the paper runtime (M6-C): per stream and per venue.

Read-only and JSON-ready. Ages are measured against the injected clock from the local receive
time; latency is receive time minus the venue's exchange timestamp (clock skew included).
`top_of_book` publishes best bid/ask only from fresh LIVE two-sided books (never a stale or
one-sided value) for the read-only dashboard.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.market import OrderBookSnapshot
from shadow_trading_suite.streaming.events import StreamKind, Subscription
from shadow_trading_suite.streaming.session import SessionHealth
from shadow_trading_suite.streaming.state import MarketSnapshot, StreamKey, StreamStatus


def market_data_report(
    snapshot: MarketSnapshot,
    health: Mapping[Venue, SessionHealth],
    now: datetime,
    subscriptions: Iterable[Subscription],
) -> dict[str, Any]:
    streams: dict[str, Any] = {}
    for sub in subscriptions:
        key = StreamKey(sub.instrument_id, sub.kind)
        status = snapshot.status.get(key, StreamStatus.DOWN)
        event = snapshot.entries.get(key)
        row: dict[str, Any] = {"status": status.value}
        if event is not None:
            row |= {
                "age_s": round((now - event.received_at).total_seconds(), 3),
                "latency_s": round((event.received_at - event.exchange_ts).total_seconds(), 3),
                "exchange_ts": event.exchange_ts.isoformat(),
                "epoch": event.epoch,
            }
        streams[f"{sub.instrument_id}:{sub.kind.value}"] = row
    venues = {
        v.value: {
            "connected": h.connected,
            "epoch": h.epoch,
            "connects": h.connects,
            "connect_failures": h.connect_failures,
            "disconnects": h.disconnects,
            "sequence_gaps": h.gaps,
            "book_resyncs": h.resyncs,
            "last_message_at": h.last_message_at.isoformat() if h.last_message_at else None,
            "last_error": h.last_error,
        }
        for v, h in sorted(health.items(), key=lambda kv: kv[0].value)
    }
    live = sum(1 for r in streams.values() if r["status"] == StreamStatus.LIVE.value)
    return {
        "as_of": now.isoformat(),
        "snapshot_version": snapshot.version,
        "streams_live": live,
        "streams_total": len(streams),
        "streams": streams,
        "venues": venues,
    }


def top_of_book(
    snapshot: MarketSnapshot, max_age: Mapping[InstrumentId, timedelta]
) -> dict[str, Any]:
    """Best bid/ask and spread per instrument, only where the book stream is LIVE, younger than
    the instrument's max age and two-sided. Anything else is omitted, never filled."""
    out: dict[str, Any] = {}
    for iid, age in sorted(max_age.items(), key=lambda kv: str(kv[0])):
        try:
            event = snapshot.require(StreamKey(iid, StreamKind.BOOK), age)
        except DataQualityError:
            continue
        book = event.payload
        if not isinstance(book, OrderBookSnapshot):
            continue
        if book.best_bid is None or book.best_ask is None:
            continue
        bid, ask = book.best_bid.price, book.best_ask.price
        mid = (bid + ask) / 2
        out[str(iid)] = {
            "bid": str(bid),
            "ask": str(ask),
            "spread_bps": str(((ask - bid) / mid * Decimal(10_000)).quantize(Decimal("0.01"))),
            "exchange_ts": event.exchange_ts.isoformat(),
            "age_s": round((snapshot.as_of - event.received_at).total_seconds(), 3),
        }
    return out
