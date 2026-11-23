"""Coinbase Advanced Trade public WebSocket protocol (verified live 2026-10-19).

Public, unauthenticated channels: heartbeats, ticker, level2 (full-depth snapshot + updates),
market_trades, candles (5-minute buckets; snapshot skips empty buckets), status.
NOT published on the public socket: mark price, index price, funding (REST current funding only).
`sequence_num` is per connection and contiguous across all channels: any gap invalidates the
connection's state. Limits: 8 connections/s and 8 unauthenticated messages/s per IP.
"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.market import Candle, Ticker, Trade
from shadow_trading_suite.core.time import ensure_utc
from shadow_trading_suite.exchanges import parsing as p
from shadow_trading_suite.exchanges.errors import ResponseFormatError, TransportPolicyError
from shadow_trading_suite.streaming.events import (
    Ack,
    BookDeltaMsg,
    BookSnapshotMsg,
    ConnectionGap,
    Heartbeat,
    ParseResult,
    StreamEvent,
    StreamKind,
    Subscription,
)
from shadow_trading_suite.streaming.transport import ALLOWED_WS_URLS, WsEndpoint

CHANNELS = {
    StreamKind.TRADES: "market_trades",
    StreamKind.BOOK: "level2",
    StreamKind.TICKER: "ticker",
    StreamKind.CANDLES: "candles",
}
_ALLOWED_CHANNELS = frozenset({*CHANNELS.values(), "heartbeats"})
_PRODUCT = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,63}$")
CANDLE_INTERVAL = timedelta(minutes=5)
MESSAGES_PER_SECOND = 8.0  # documented per-IP limit for unauthenticated messages


def validate_outbound(text: str) -> None:
    """Only subscribe/unsubscribe to allowlisted public channels; exact keys; no credentials."""
    try:
        msg = json.loads(text)
    except ValueError as exc:
        raise TransportPolicyError("outbound frame is not JSON") from exc
    if not isinstance(msg, dict) or set(msg) - {"type", "channel", "product_ids"}:
        raise TransportPolicyError("outbound frame has unexpected fields")
    if msg.get("type") not in ("subscribe", "unsubscribe"):
        raise TransportPolicyError("only subscribe/unsubscribe frames are allowed")
    if msg.get("channel") not in _ALLOWED_CHANNELS:
        raise TransportPolicyError(f"channel {msg.get('channel')!r} not allowlisted")
    products = msg.get("product_ids", [])
    if not isinstance(products, list) or not all(
        isinstance(x, str) and _PRODUCT.fullmatch(x) for x in products
    ):
        raise TransportPolicyError("invalid product_ids")


ENDPOINT = WsEndpoint(Venue.COINBASE, ALLOWED_WS_URLS[Venue.COINBASE], validate_outbound)


class CoinbaseStreamProtocol:
    venue = Venue.COINBASE
    endpoint = ENDPOINT
    supported = frozenset(CHANNELS)
    connect_delay_s = 0.0
    messages_per_second = MESSAGES_PER_SECOND
    max_silence = timedelta(seconds=10)  # heartbeats arrive every second

    def __init__(self) -> None:
        self._last_seq: int | None = None
        self._subs: dict[tuple[str, StreamKind], Subscription] = {}

    def reset(self) -> None:
        self._last_seq = None

    def subscription_for_channel(self, channel: str) -> Subscription | None:
        return None  # Coinbase reports subscription problems as connection-level errors

    def _frames(self, kind: str, subs: Sequence[Subscription]) -> list[str]:
        by_channel: dict[str, list[str]] = {}
        for sub in subs:
            if sub.kind not in CHANNELS:
                raise ValueError(f"coinbase public websocket does not publish {sub.kind.value}")
            self._subs[(sub.instrument_id.symbol, sub.kind)] = sub
            by_channel.setdefault(CHANNELS[sub.kind], []).append(sub.instrument_id.symbol)
        frames = [
            json.dumps({"type": kind, "channel": ch, "product_ids": sorted(set(ids))})
            for ch, ids in sorted(by_channel.items())
        ]
        if kind == "subscribe":
            frames.insert(0, json.dumps({"type": "subscribe", "channel": "heartbeats"}))
        return frames

    def subscribe_frames(self, subs: Sequence[Subscription]) -> list[str]:
        return self._frames("subscribe", subs)

    def unsubscribe_frames(self, subs: Sequence[Subscription]) -> list[str]:
        return self._frames("unsubscribe", subs)

    def _iid(self, symbol: str) -> InstrumentId:
        return InstrumentId(venue=Venue.COINBASE, symbol=symbol)

    def parse(self, text: str, received_at: datetime, epoch: int) -> ParseResult:
        result = ParseResult()
        msg = p.obj(json.loads(text), "frame")
        if msg.get("type") == "error":
            result.controls.append(ConnectionGap(f"venue error: {msg.get('message', '')}"))
            return result
        seq = p.integer(msg.get("sequence_num"), "sequence_num")
        if self._last_seq is not None:
            if seq <= self._last_seq:
                return result  # duplicate or replayed frame
            if seq != self._last_seq + 1:
                result.controls.append(ConnectionGap(f"sequence gap {self._last_seq} -> {seq}"))
        self._last_seq = seq
        ts = ensure_utc(datetime.fromisoformat(p.text(msg.get("timestamp"), "timestamp")))
        channel = msg.get("channel")
        events = p.arr(msg.get("events", []), "events")
        handler = {
            "heartbeats": lambda e: result.controls.append(Heartbeat()),
            "subscriptions": lambda e: result.controls.append(Ack("subscriptions", True)),
            "ticker": lambda e: self._tickers(e, ts, received_at, epoch, seq, result),
            "l2_data": lambda e: self._book(e, ts, seq, result),
            "market_trades": lambda e: self._trades(e, received_at, epoch, seq, result),
            "candles": lambda e: self._candles(e, ts, received_at, epoch, seq, result),
            "status": lambda e: None,
        }.get(str(channel))
        if handler is None:
            raise ResponseFormatError(f"unknown coinbase channel {channel!r}")
        for event in events:
            handler(p.obj(event, "event"))
        return result

    def _event(self, **fields: Any) -> StreamEvent:
        return p.build(StreamEvent, "stream event", **fields)

    def _tickers(
        self,
        e: Mapping[str, Any],
        ts: datetime,
        received: datetime,
        epoch: int,
        seq: int,
        out: ParseResult,
    ) -> None:
        for raw in p.arr(e.get("tickers", []), "tickers"):
            t = p.obj(raw, "ticker")
            iid = self._iid(p.text(t.get("product_id"), "product_id"))
            ticker = p.build(
                Ticker,
                "ticker",
                instrument_id=iid,
                ts=ts,
                last=p.opt_decimal(t.get("price")),
                bid=p.opt_decimal(t.get("best_bid")),
                bid_qty=p.opt_decimal(t.get("best_bid_quantity")),
                ask=p.opt_decimal(t.get("best_ask")),
                ask_qty=p.opt_decimal(t.get("best_ask_quantity")),
            )
            out.events.append(
                self._event(
                    event_id=f"coinbase:ticker:{iid.symbol}:{epoch}:{seq}",
                    kind=StreamKind.TICKER,
                    instrument_id=iid,
                    exchange_ts=ts,
                    received_at=received,
                    epoch=epoch,
                    sequence=seq,
                    payload=ticker,
                )
            )

    def _book(self, e: Mapping[str, Any], ts: datetime, seq: int, out: ParseResult) -> None:
        symbol = p.text(e.get("product_id"), "product_id")
        sub = self._subs.get(
            (symbol, StreamKind.BOOK), Subscription(self._iid(symbol), StreamKind.BOOK)
        )
        changes = tuple(_level(p.obj(u, "update")) for u in p.arr(e.get("updates", []), "updates"))
        if e.get("type") == "snapshot":
            bids = tuple((pr, q) for side, pr, q in changes if side == "bid")
            asks = tuple((pr, q) for side, pr, q in changes if side == "ask")
            out.controls.append(BookSnapshotMsg(sub, bids, asks, ts, None))
        else:
            out.controls.append(BookDeltaMsg(sub, changes, ts, None, None))

    def _trades(
        self, e: Mapping[str, Any], received: datetime, epoch: int, seq: int, out: ParseResult
    ) -> None:
        for raw in p.arr(e.get("trades", []), "trades"):
            t = p.obj(raw, "trade")
            iid = self._iid(p.text(t.get("product_id"), "product_id"))
            trade = p.build(
                Trade,
                "trade",
                instrument_id=iid,
                trade_id=p.text(t.get("trade_id"), "trade_id"),
                ts=p.from_iso(t.get("time"), "time"),
                price=t.get("price"),
                quantity=t.get("size"),
                aggressor=None,  # side semantics not documented unambiguously (see M2)
            )
            out.events.append(
                self._event(
                    event_id=f"coinbase:trade:{iid.symbol}:{trade.trade_id}",
                    kind=StreamKind.TRADES,
                    instrument_id=iid,
                    exchange_ts=trade.ts,
                    received_at=received,
                    epoch=epoch,
                    sequence=seq,
                    payload=trade,
                )
            )

    def _candles(
        self,
        e: Mapping[str, Any],
        ts: datetime,
        received: datetime,
        epoch: int,
        seq: int,
        out: ParseResult,
    ) -> None:
        for raw in p.arr(e.get("candles", []), "candles"):
            c = p.obj(raw, "candle")
            iid = self._iid(p.text(c.get("product_id"), "product_id"))
            candle = p.build(
                Candle,
                "candle",
                instrument_id=iid,
                interval=CANDLE_INTERVAL,
                open_time=p.from_unix_s(c.get("start"), "start"),
                open=c.get("open"),
                high=c.get("high"),
                low=c.get("low"),
                close=c.get("close"),
                volume=c.get("volume"),
            )
            closed = candle.close_time <= ts
            discriminator = "closed" if closed else f"{epoch}:{seq}"
            out.events.append(
                self._event(
                    event_id=f"coinbase:candle:{iid.symbol}:{int(candle.open_time.timestamp())}:{discriminator}",
                    kind=StreamKind.CANDLES,
                    instrument_id=iid,
                    exchange_ts=ts,
                    received_at=received,
                    epoch=epoch,
                    sequence=seq,
                    payload=candle,
                    candle_closed=closed,
                )
            )


def _level(u: Mapping[str, Any]) -> tuple[str, Decimal, Decimal]:
    side = {"bid": "bid", "offer": "ask"}.get(str(u.get("side")))
    price, qty = p.opt_decimal(u.get("price_level")), p.opt_decimal(u.get("new_quantity"))
    if side is None or price is None or qty is None:
        raise ResponseFormatError(f"malformed level2 update {dict(u)}")
    return side, price, qty
