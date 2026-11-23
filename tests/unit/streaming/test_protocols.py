"""Venue stream protocols, driven by recorded real frames (2026-10-19)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.market import (
    Candle,
    FundingRate,
    IndexPrice,
    MarkPrice,
    Ticker,
    Trade,
)
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.exchanges.errors import TransportPolicyError
from shadow_trading_suite.streaming.coinbase import CoinbaseStreamProtocol
from shadow_trading_suite.streaming.cryptocom import CryptoComStreamProtocol
from shadow_trading_suite.streaming.events import (
    Ack,
    BookDeltaMsg,
    BookSnapshotMsg,
    ConnectionGap,
    Heartbeat,
    HeartbeatReply,
    StreamKind,
    Subscription,
)
from tests.unit.exchanges.fakes import fixture_json

RECEIVED = datetime(2026, 10, 19, 14, 44, tzinfo=UTC)
CB_BTC = InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD")
CC_BTC = InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTC_USD")
CC_PERP = InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTCUSD-PERP")


def frame(rel: str, **overrides: Any) -> str:
    data = fixture_json(f"ws/{rel}")
    data.update(overrides)
    return json.dumps(data)


class TestCoinbaseSubscriptions:
    def test_supported_kinds_match_public_channels(self) -> None:
        assert CoinbaseStreamProtocol().supported == frozenset(
            {StreamKind.TRADES, StreamKind.BOOK, StreamKind.TICKER, StreamKind.CANDLES}
        )

    def test_subscribe_frames_pass_outbound_policy(self) -> None:
        proto = CoinbaseStreamProtocol()
        subs = [Subscription(CB_BTC, k) for k in proto.supported]
        frames = proto.subscribe_frames(subs)
        for f in frames:
            proto.endpoint.validate_outbound(f)
        channels = {json.loads(f)["channel"] for f in frames}
        assert channels == {"heartbeats", "level2", "market_trades", "ticker", "candles"}

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "subscribe", "channel": "user", "product_ids": ["BTC-USD"]},
            {"type": "subscribe", "channel": "futures_balance_summary"},
            {"type": "subscribe", "channel": "level2", "product_ids": ["BTC-USD"], "jwt": "x"},
            {"type": "subscribe", "channel": "level2", "product_ids": ["BTC USD"]},
            {"type": "order", "channel": "level2"},
            "not json",
        ],
    )
    def test_outbound_policy_refuses_everything_else(self, bad: Any) -> None:
        with pytest.raises(TransportPolicyError):
            CoinbaseStreamProtocol().endpoint.validate_outbound(
                bad if isinstance(bad, str) else json.dumps(bad)
            )

    def test_unsupported_kind_refused(self) -> None:
        with pytest.raises(ValueError, match="mark"):
            CoinbaseStreamProtocol().subscribe_frames([Subscription(CB_BTC, StreamKind.MARK)])


class TestCoinbaseParsing:
    def parse(self, proto: CoinbaseStreamProtocol, rel: str, **kw: Any) -> Any:
        return proto.parse(frame(rel, **kw), RECEIVED, epoch=1)

    def test_ticker(self) -> None:
        proto = CoinbaseStreamProtocol()
        result = self.parse(proto, "coinbase/ticker_update.json", sequence_num=0)
        [event] = result.events
        assert event.kind is StreamKind.TICKER
        assert isinstance(event.payload, Ticker)
        assert event.payload.bid is not None
        assert event.payload.ask is not None
        assert event.payload.bid < event.payload.ask
        assert event.event_id == "coinbase:ticker:BTC-USD:1:0"
        assert event.received_at == RECEIVED

    def test_level2_snapshot_and_update_become_book_messages(self) -> None:
        proto = CoinbaseStreamProtocol()
        snap = self.parse(proto, "coinbase/l2_snapshot_trimmed.json", sequence_num=0)
        [msg] = snap.controls
        assert isinstance(msg, BookSnapshotMsg)
        assert len(msg.bids) == 10
        assert len(msg.asks) == 10
        upd = self.parse(proto, "coinbase/l2_update.json", sequence_num=1)
        [delta] = upd.controls
        assert isinstance(delta, BookDeltaMsg)
        assert {side for side, _, _ in delta.changes} <= {"bid", "ask"}

    def test_trades_have_stable_ids(self) -> None:
        proto = CoinbaseStreamProtocol()
        events = self.parse(proto, "coinbase/trades_snapshot.json", sequence_num=0).events
        assert all(isinstance(e.payload, Trade) for e in events)
        assert events[0].event_id.startswith("coinbase:trade:BTC-USD:")
        assert len({e.event_id for e in events}) == len(events)

    def test_candles_marked_open_or_closed(self) -> None:
        proto = CoinbaseStreamProtocol()
        events = self.parse(proto, "coinbase/candles_snapshot.json", sequence_num=0).events
        assert all(isinstance(e.payload, Candle) for e in events)
        assert events[-1].candle_closed in (True, False)
        assert all(e.candle_closed for e in events[:-1])

    def test_heartbeat_and_ack(self) -> None:
        proto = CoinbaseStreamProtocol()
        assert isinstance(
            self.parse(proto, "coinbase/heartbeat.json", sequence_num=0).controls[0], Heartbeat
        )
        ack = self.parse(proto, "coinbase/subscriptions.json", sequence_num=1).controls[0]
        assert isinstance(ack, Ack)
        assert ack.ok

    def test_connection_level_sequence_gap(self) -> None:
        proto = CoinbaseStreamProtocol()
        self.parse(proto, "coinbase/heartbeat.json", sequence_num=0)
        result = self.parse(proto, "coinbase/heartbeat.json", sequence_num=2)
        assert any(isinstance(c, ConnectionGap) for c in result.controls)

    def test_duplicate_sequence_ignored(self) -> None:
        proto = CoinbaseStreamProtocol()
        self.parse(proto, "coinbase/ticker_update.json", sequence_num=0)
        again = self.parse(proto, "coinbase/ticker_update.json", sequence_num=0)
        assert again.events == []
        assert again.controls == []

    def test_reset_for_new_connection(self) -> None:
        proto = CoinbaseStreamProtocol()
        self.parse(proto, "coinbase/heartbeat.json", sequence_num=5)
        proto.reset()
        assert not self.parse(proto, "coinbase/heartbeat.json", sequence_num=0).controls[1:]

    def test_error_frame_forces_reconnect(self) -> None:
        result = CoinbaseStreamProtocol().parse(
            json.dumps({"type": "error", "message": "rate limit"}), RECEIVED, epoch=1
        )
        assert isinstance(result.controls[0], ConnectionGap)


class TestCryptoComSubscriptions:
    def test_supported_kinds(self) -> None:
        assert CryptoComStreamProtocol().supported == frozenset(StreamKind)

    def test_single_subscribe_frame_passes_outbound_policy(self) -> None:
        proto = CryptoComStreamProtocol()
        subs = [Subscription(CC_BTC, StreamKind.BOOK), Subscription(CC_PERP, StreamKind.INDEX)]
        [f] = proto.subscribe_frames(subs)
        proto.endpoint.validate_outbound(f)
        params = json.loads(f)["params"]
        assert params["channels"] == ["book.BTC_USD.10", "index.BTCUSD-INDEX"]
        assert params["book_subscription_type"] == "SNAPSHOT_AND_UPDATE"

    def test_connect_delay_recommended_by_venue(self) -> None:
        assert CryptoComStreamProtocol().connect_delay_s == 1.0

    @pytest.mark.parametrize(
        "bad",
        [
            {"id": 1, "method": "public/auth", "api_key": "k", "sig": "s"},
            {"id": 1, "method": "private/create-order", "params": {}},
            {"id": 1, "method": "subscribe", "params": {"channels": ["user.order.BTC_USD"]}},
            {
                "id": 1,
                "method": "subscribe",
                "params": {"channels": ["book.BTC_USD.10"]},
                "sig": "x",
            },
            {"id": "1", "method": "public/respond-heartbeat"},
        ],
    )
    def test_outbound_policy_refuses_auth_private_and_unknown(self, bad: Any) -> None:
        with pytest.raises(TransportPolicyError):
            CryptoComStreamProtocol().endpoint.validate_outbound(json.dumps(bad))


class TestCryptoComParsing:
    def proto(self) -> CryptoComStreamProtocol:
        p = CryptoComStreamProtocol()
        p.subscribe_frames(
            [
                Subscription(CC_BTC, StreamKind.BOOK),
                Subscription(CC_BTC, StreamKind.TICKER),
                Subscription(CC_BTC, StreamKind.TRADES),
                Subscription(CC_BTC, StreamKind.CANDLES),
                Subscription(CC_PERP, StreamKind.MARK),
                Subscription(CC_PERP, StreamKind.INDEX),
                Subscription(CC_PERP, StreamKind.FUNDING),
                Subscription(CC_PERP, StreamKind.ESTIMATED_FUNDING),
            ]
        )
        return p

    def parse(self, p: CryptoComStreamProtocol, rel: str) -> Any:
        return p.parse(frame(f"cryptocom/{rel}"), RECEIVED, epoch=1)

    def test_heartbeat_produces_valid_reply(self) -> None:
        p = self.proto()
        [reply] = self.parse(p, "heartbeat.json").controls
        assert isinstance(reply, HeartbeatReply)
        p.endpoint.validate_outbound(reply.text)
        assert json.loads(reply.text)["method"] == "public/respond-heartbeat"

    def test_book_snapshot_and_chained_deltas(self) -> None:
        p = self.proto()
        [snap] = self.parse(p, "book_snapshot.json").controls
        assert isinstance(snap, BookSnapshotMsg)
        assert snap.sequence is not None
        [d1] = self.parse(p, "book_update_1.json").controls
        assert isinstance(d1, BookDeltaMsg)
        assert d1.previous == snap.sequence

    def test_ticker_trade_candle(self) -> None:
        p = self.proto()
        [t] = self.parse(p, "ticker.json").events
        assert isinstance(t.payload, Ticker)
        assert t.payload.last is not None
        trades = self.parse(p, "trade_snapshot.json").events
        assert all(
            isinstance(e.payload, Trade) and e.payload.aggressor in (Side.BUY, Side.SELL)
            for e in trades
        )
        [c] = self.parse(p, "candlestick_update.json").events
        assert isinstance(c.payload, Candle)
        assert c.candle_closed is False

    def test_mark_index_funding(self) -> None:
        p = self.proto()
        [m] = self.parse(p, "mark.json").events
        assert isinstance(m.payload, MarkPrice)
        assert m.instrument_id == CC_PERP
        [i] = self.parse(p, "index.json").events
        assert isinstance(i.payload, IndexPrice)
        assert i.instrument_id == CC_PERP
        assert i.payload.index_symbol == "BTCUSD-INDEX"
        [f] = self.parse(p, "funding.json").events
        assert isinstance(f.payload, FundingRate)
        assert f.kind is StreamKind.FUNDING
        [ef] = self.parse(p, "estimatedfunding.json").events
        assert ef.kind is StreamKind.ESTIMATED_FUNDING
        assert isinstance(ef.payload, FundingRate)
        assert ef.payload.rate == Decimal("0.000002234")

    def test_rejected_subscription_reported(self) -> None:
        p = self.proto()
        [ack] = self.parse(p, "ack_error_settlement.json").controls
        assert isinstance(ack, Ack)
        assert not ack.ok
        assert "Unsupported" in ack.message
