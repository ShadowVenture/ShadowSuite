"""Crypto.com Exchange public market-data WebSocket protocol (verified live 2026-10-19).

Channels: book.{sym}.{depth} (snapshot + book.update deltas chained by pu -> u; empty delta
every 5 s still carries sequence numbers), ticker, trade, candlestick, mark, index (index
symbol, e.g. BTCUSD-INDEX), funding, estimatedfunding. `settlement` is rejected for perps.
The venue sends public/heartbeat every 30 s and drops the connection unless answered within
5 s. Limits: 100 market-data requests/s; wait 1 s after connecting before subscribing.
"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from shadow_trading_suite.core.instrument import Venue
from shadow_trading_suite.core.market import (
    Candle,
    FundingRate,
    IndexPrice,
    MarkPrice,
    Ticker,
    Trade,
)
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.exchanges import parsing as p
from shadow_trading_suite.exchanges.errors import ResponseFormatError, TransportPolicyError
from shadow_trading_suite.streaming.events import (
    Ack,
    BookDeltaMsg,
    BookSnapshotMsg,
    HeartbeatReply,
    ParseResult,
    StreamEvent,
    StreamKind,
    Subscription,
)
from shadow_trading_suite.streaming.transport import ALLOWED_WS_URLS, WsEndpoint

BOOK_DEPTH = 10  # default subscribed depth; 50 VERIFIED by the R4 probe (2026-10-19)
BOOK_DEPTHS = (10, 50)  # the depths the venue documents (and the outbound validator allows)
CANDLE_TIMEFRAME, CANDLE_INTERVAL = "1m", timedelta(minutes=1)
FUNDING_INTERVAL = timedelta(hours=1)
_SYMBOL = r"[A-Z0-9][A-Z0-9_\-]{0,63}"
_CHANNEL = re.compile(
    rf"^(book\.{_SYMBOL}\.(10|50)|ticker\.{_SYMBOL}|trade\.{_SYMBOL}|candlestick\.1m\.{_SYMBOL}"
    rf"|mark\.{_SYMBOL}|index\.{_SYMBOL}|funding\.{_SYMBOL}|estimatedfunding\.{_SYMBOL})$"
)
_PREFIX = {
    StreamKind.TRADES: "trade",
    StreamKind.TICKER: "ticker",
    StreamKind.MARK: "mark",
    StreamKind.FUNDING: "funding",
    StreamKind.ESTIMATED_FUNDING: "estimatedfunding",
}


def validate_outbound(text: str) -> None:
    """Only public subscribe/unsubscribe and heartbeat replies; exact shapes; no credentials."""
    try:
        msg = json.loads(text)
    except ValueError as exc:
        raise TransportPolicyError("outbound frame is not JSON") from exc
    if (
        not isinstance(msg, dict)
        or not isinstance(msg.get("id"), int)
        or isinstance(msg.get("id"), bool)
    ):
        raise TransportPolicyError("outbound frame needs an integer id")
    method = msg.get("method")
    if method == "public/respond-heartbeat":
        if set(msg) != {"id", "method"}:
            raise TransportPolicyError("heartbeat reply has unexpected fields")
        return
    if method not in ("subscribe", "unsubscribe") or set(msg) - {"id", "method", "params"}:
        raise TransportPolicyError(f"method {method!r} / fields not allowed")
    params = msg.get("params")
    if not isinstance(params, dict) or set(params) - {
        "channels",
        "book_subscription_type",
        "book_update_frequency",
    }:
        raise TransportPolicyError("unexpected params")
    channels = params.get("channels")
    if (
        not isinstance(channels, list)
        or not channels
        or not all(isinstance(c, str) and _CHANNEL.fullmatch(c) for c in channels)
    ):
        raise TransportPolicyError("channel not allowlisted")
    if params.get("book_subscription_type", "SNAPSHOT_AND_UPDATE") != "SNAPSHOT_AND_UPDATE":
        raise TransportPolicyError("unsupported book subscription type")
    if params.get("book_update_frequency", 10) not in (10, 100):
        raise TransportPolicyError("unsupported book update frequency")


ENDPOINT = WsEndpoint(Venue.CRYPTOCOM, ALLOWED_WS_URLS[Venue.CRYPTOCOM], validate_outbound)


def _index_symbol(sub: Subscription) -> str:
    if sub.venue_symbol:
        return sub.venue_symbol
    symbol = sub.instrument_id.symbol
    if symbol.endswith("-PERP"):
        return symbol[: -len("-PERP")] + "-INDEX"
    raise ValueError(f"index stream for {symbol} needs an explicit venue_symbol")


class CryptoComStreamProtocol:
    venue = Venue.CRYPTOCOM
    endpoint = ENDPOINT
    supported = frozenset(StreamKind)
    connect_delay_s = 1.0
    messages_per_second = 100.0
    max_silence = timedelta(seconds=40)  # heartbeat every 30 s (+ book deltas every 5 s)

    def __init__(self, book_depth: int = BOOK_DEPTH) -> None:
        if book_depth not in BOOK_DEPTHS:
            raise ValueError(f"book depth must be one of {BOOK_DEPTHS}")
        self.book_depth = book_depth
        self._next_id = 1
        self._by_channel: dict[str, Subscription] = {}

    def reset(self) -> None:
        pass  # book sequence state lives in the session's local books

    def subscription_for_channel(self, channel: str) -> Subscription | None:
        return self._by_channel.get(channel)

    def _channel(self, sub: Subscription) -> str:
        symbol = sub.instrument_id.symbol
        if sub.kind is StreamKind.BOOK:
            return f"book.{symbol}.{self.book_depth}"
        if sub.kind is StreamKind.CANDLES:
            return f"candlestick.{CANDLE_TIMEFRAME}.{symbol}"
        if sub.kind is StreamKind.INDEX:
            return f"index.{_index_symbol(sub)}"
        return f"{_PREFIX[sub.kind]}.{symbol}"

    def _frame(self, method: str, subs: Sequence[Subscription]) -> list[str]:
        channels = []
        for sub in subs:
            channel = self._channel(sub)
            self._by_channel[channel] = sub
            channels.append(channel)
        self._next_id += 1
        params: dict[str, Any] = {"channels": channels}
        if any(s.kind is StreamKind.BOOK for s in subs):
            params.update(book_subscription_type="SNAPSHOT_AND_UPDATE", book_update_frequency=10)
        return [json.dumps({"id": self._next_id, "method": method, "params": params})]

    def subscribe_frames(self, subs: Sequence[Subscription]) -> list[str]:
        return self._frame("subscribe", subs)

    def unsubscribe_frames(self, subs: Sequence[Subscription]) -> list[str]:
        return self._frame("unsubscribe", subs)

    def parse(self, text: str, received_at: datetime, epoch: int) -> ParseResult:
        result = ParseResult()
        msg = p.obj(json.loads(text), "frame")
        if msg.get("method") == "public/heartbeat":
            reply = {"id": p.integer(msg.get("id"), "id"), "method": "public/respond-heartbeat"}
            result.controls.append(HeartbeatReply(json.dumps(reply)))
            return result
        body = msg.get("result")
        if body is None:  # subscription acknowledgement or error
            result.controls.append(
                Ack(str(msg.get("channel", "")), msg.get("code") == 0, str(msg.get("message", "")))
            )
            return result
        body = p.obj(body, "result")
        sub = self._by_channel.get(str(body.get("subscription")))
        if sub is None:
            raise ResponseFormatError(f"data for unknown subscription {body.get('subscription')!r}")
        channel = body.get("channel")
        rows = [p.obj(r, "data") for r in p.arr(body.get("data", []), "data")]
        for row in rows:
            if channel == "book":
                result.controls.append(
                    BookSnapshotMsg(
                        sub,
                        _levels(row.get("bids")),
                        _levels(row.get("asks")),
                        p.from_unix_ms(row.get("t"), "t"),
                        p.integer(row.get("u"), "u"),
                    )
                )
            elif channel == "book.update":
                update = p.obj(row.get("update", {}), "update")
                changes = tuple(
                    ("bid", pr, q) for pr, q in _levels(update.get("bids", []))
                ) + tuple(("ask", pr, q) for pr, q in _levels(update.get("asks", [])))
                result.controls.append(
                    BookDeltaMsg(
                        sub,
                        changes,
                        p.from_unix_ms(row.get("t"), "t"),
                        p.integer(row.get("u"), "u"),
                        p.integer(row.get("pu"), "pu"),
                    )
                )
            else:
                result.events.append(
                    self._event(
                        str(channel), sub, row, received_at, epoch, body.get("instrument_name")
                    )
                )
        return result

    def _event(
        self,
        channel: str,
        sub: Subscription,
        row: Mapping[str, Any],
        received: datetime,
        epoch: int,
        name: Any,
    ) -> StreamEvent:
        iid, symbol = sub.instrument_id, sub.instrument_id.symbol
        ts = p.from_unix_ms(row.get("t"), "t")
        common: dict[str, Any] = {"instrument_id": iid, "received_at": received, "epoch": epoch}
        if channel == "ticker":
            payload: Any = p.build(
                Ticker,
                "ticker",
                instrument_id=iid,
                ts=ts,
                last=p.opt_decimal(row.get("a")),
                bid=p.opt_decimal(row.get("b")),
                bid_qty=p.opt_decimal(row.get("bs")),
                ask=p.opt_decimal(row.get("k")),
                ask_qty=p.opt_decimal(row.get("ks")),
            )
            return p.build(
                StreamEvent,
                "event",
                event_id=f"cryptocom:ticker:{symbol}:{ts.timestamp():.3f}",
                kind=StreamKind.TICKER,
                exchange_ts=ts,
                payload=payload,
                **common,
            )
        if channel == "trade":
            side = {"BUY": Side.BUY, "SELL": Side.SELL}.get(str(row.get("s")).upper())
            trade = p.build(
                Trade,
                "trade",
                instrument_id=iid,
                trade_id=p.text(row.get("d"), "d"),
                ts=ts,
                price=row.get("p"),
                quantity=row.get("q"),
                aggressor=side,
            )
            return p.build(
                StreamEvent,
                "event",
                event_id=f"cryptocom:trade:{symbol}:{trade.trade_id}",
                kind=StreamKind.TRADES,
                exchange_ts=ts,
                payload=trade,
                **common,
            )
        if channel == "candlestick":
            candle = p.build(
                Candle,
                "candle",
                instrument_id=iid,
                interval=CANDLE_INTERVAL,
                open_time=ts,
                open=row.get("o"),
                high=row.get("h"),
                low=row.get("l"),
                close=row.get("c"),
                volume=row.get("v"),
            )
            updated = p.from_unix_ms(row["ut"], "ut") if "ut" in row else candle.close_time
            closed = updated >= candle.close_time
            return p.build(
                StreamEvent,
                "event",
                event_id=f"cryptocom:candle:{symbol}:{int(ts.timestamp())}:{updated.timestamp():.3f}",
                kind=StreamKind.CANDLES,
                exchange_ts=updated,
                payload=candle,
                candle_closed=closed,
                **common,
            )
        value = p.opt_decimal(row.get("v"))
        if channel == "mark":
            payload = p.build(MarkPrice, "mark", instrument_id=iid, ts=ts, mark=value)
            kind = StreamKind.MARK
        elif channel == "index":
            payload = p.build(
                IndexPrice,
                "index",
                instrument_id=iid,
                ts=ts,
                value=value,
                index_symbol=p.opt_text(name),
            )
            kind = StreamKind.INDEX
        elif channel in ("funding", "estimatedfunding"):
            payload = p.build(
                FundingRate,
                "funding",
                instrument_id=iid,
                ts=ts,
                rate=value,
                interval=FUNDING_INTERVAL,
            )
            kind = StreamKind.FUNDING if channel == "funding" else StreamKind.ESTIMATED_FUNDING
        else:
            raise ResponseFormatError(f"unknown crypto.com channel {channel!r}")
        return p.build(
            StreamEvent,
            "event",
            event_id=f"cryptocom:{kind.value}:{symbol}:{ts.timestamp():.3f}",
            kind=kind,
            exchange_ts=ts,
            payload=payload,
            **common,
        )


def _levels(raw: Any) -> tuple[tuple[Decimal, Decimal], ...]:
    out = []
    for level in p.arr(raw if raw is not None else [], "levels"):
        row = p.arr(level, "level")
        price, qty = (
            (p.opt_decimal(row[0]), p.opt_decimal(row[1])) if len(row) >= 2 else (None, None)
        )
        if price is None or qty is None:
            raise ResponseFormatError(f"malformed book level {row}")
        out.append((price, qty))
    return tuple(out)
