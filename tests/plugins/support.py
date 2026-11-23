"""Deterministic host + scripted public-market feed for the plugin tests (no network)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from shadow_trading_suite.core.instrument import Instrument, InstrumentId, Venue
from shadow_trading_suite.core.market import BookLevel, OrderBookSnapshot, Ticker, Trade
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.exchanges.coinbase.adapter import parse_product
from shadow_trading_suite.host.paper import PaperHost
from shadow_trading_suite.risk.config import (
    BotRiskProfile,
    PortfolioConfig,
    default_portfolio_config,
)
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind
from shadow_trading_suite.streaming.state import StreamKey, StreamStatus

Payload = Trade | Ticker | OrderBookSnapshot
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
D = Decimal


def cb(symbol: str) -> InstrumentId:
    return InstrumentId(venue=Venue.COINBASE, symbol=symbol)


BTC_USD = cb("BTC-USD")
FX = (cb("BTC-EUR"), cb("BTC-USD"), cb("ETH-EUR"), cb("ETH-USD"))


def instruments() -> dict[InstrumentId, Instrument]:
    rows = json.loads((FIXTURES / "coinbase/products_spot.json").read_text())["products"]
    inst = parse_product(next(r for r in rows if r["product_id"] == "BTC-USD"))
    return {inst.id: inst}


def profile(bot_id: str = "test-bot", allocation: int = 1000) -> BotRiskProfile:
    return BotRiskProfile(
        bot_id=bot_id,
        display_name=bot_id,
        venue=Venue.COINBASE,
        allocation_eur=D(allocation),
        max_leverage=D(1),
        max_position_margin=D("0.20"),
        max_total_margin=D("0.50"),
        max_effective_leverage=D(1),
        min_liquidation_distance=D("0.05"),
        daily_loss_limit=D("0.10"),
        max_drawdown=D("0.25"),
        max_open_positions=3,
        max_orders_per_minute=10,
    )


def portfolio(*bot_ids: str) -> PortfolioConfig:
    return default_portfolio_config(
        tuple(profile(b) for b in bot_ids), starting_capital_eur=D(5000)
    )


class Feed:
    """Scripted public market events applied through the host's normal event path."""

    def __init__(self, host: PaperHost, clock: ManualClock) -> None:
        self.host, self.clock, self.n = host, clock, 0
        self.price = D(82000)

    def advance(self, seconds: float) -> None:
        self.clock.set(self.clock.now() + timedelta(seconds=seconds))

    def live(self, iid: InstrumentId, *kinds: StreamKind) -> None:
        for k in kinds:
            self.host.set_stream_status(StreamKey(iid, k), StreamStatus.LIVE)

    def down(self, iid: InstrumentId, *kinds: StreamKind) -> None:
        for k in kinds:
            self.host.set_stream_status(StreamKey(iid, k), StreamStatus.DOWN)

    def _event(self, iid: InstrumentId, kind: StreamKind, payload: Payload) -> None:
        self.n += 1
        now = self.clock.now()
        self.host.on_event(
            StreamEvent(
                event_id=f"{iid.venue.value}:{kind.value}:{iid.symbol}:t:{self.n}",
                kind=kind,
                instrument_id=iid,
                exchange_ts=now,
                received_at=now,
                epoch=1,
                payload=payload,
            )
        )

    def ticker(self, iid: InstrumentId, bid: str, ask: str) -> None:
        now = self.clock.now()
        self._event(
            iid, StreamKind.TICKER, Ticker(instrument_id=iid, ts=now, bid=D(bid), ask=D(ask))
        )

    def fx(self) -> None:
        """Fresh EUR-rate tickers (BTC and ETH routes agree: ~0.86 EUR per USD)."""
        for iid in FX:
            self.live(iid, StreamKind.TICKER)
        self.ticker(cb("BTC-EUR"), "70519", "70521")
        self.ticker(cb("BTC-USD"), "81999", "82001")
        self.ticker(cb("ETH-EUR"), "2579", "2581")
        self.ticker(cb("ETH-USD"), "2999", "3001")

    def book(self, bid: str | None = None, ask: str | None = None, qty: str = "2") -> None:
        bid = bid or str(self.price - 1)
        ask = ask or str(self.price + 1)
        self.live(BTC_USD, StreamKind.BOOK, StreamKind.TRADES)
        self._event(
            BTC_USD,
            StreamKind.BOOK,
            OrderBookSnapshot(
                instrument_id=BTC_USD,
                ts=self.clock.now(),
                bids=(BookLevel(price=D(bid), quantity=D(qty)),),
                asks=(BookLevel(price=D(ask), quantity=D(qty)),),
            ),
        )

    def trade(self, price: Decimal) -> None:
        self.live(BTC_USD, StreamKind.BOOK, StreamKind.TRADES)
        self._event(
            BTC_USD,
            StreamKind.TRADES,
            Trade(
                instrument_id=BTC_USD,
                trade_id=f"t{self.n}",
                ts=self.clock.now(),
                price=price,
                quantity=D("0.01"),
                aggressor=Side.BUY,
            ),
        )

    def warm(self, minutes: int = 33) -> None:
        """Minute-close trades so the volatility estimate exists (fail-closed before that)."""
        for i in range(minutes):
            self.price = D(82000) + (D(15) if i % 2 else D(-15))
            self.trade(self.price)
            self.advance(60)
        self.price = D(82000)
        self.trade(self.price)

    def ready(self) -> None:
        """Fresh FX + book right now (the inputs every BUY needs)."""
        self.fx()
        self.book()


def make_host(tmp: Path, *bot_ids: str, clock: ManualClock | None = None) -> tuple[PaperHost, Feed]:
    clock = clock or ManualClock(T0)
    host = PaperHost(
        portfolio=portfolio(*bot_ids),
        data_dir=tmp / "data",
        instruments=instruments(),
        clock=clock,
        status_dir=tmp / "data" / "status",
    )
    return host, Feed(host, clock)
