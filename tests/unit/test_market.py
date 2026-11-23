from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.market import (
    BookLevel,
    Candle,
    FundingRate,
    MarkPrice,
    OrderBookSnapshot,
    Trade,
)
from shadow_trading_suite.core.orders import Side

IID = InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def candle(**overrides: Any) -> Candle:
    fields: dict[str, Any] = {
        "instrument_id": IID,
        "interval": timedelta(minutes=1),
        "open_time": T0,
        "open": "100",
        "high": "110",
        "low": "95",
        "close": "105",
        "volume": "12.5",
    }
    fields.update(overrides)
    return Candle.model_validate(fields)


def lvl(price: str, qty: str) -> BookLevel:
    return BookLevel(price=Decimal(price), quantity=Decimal(qty))


class TestCandle:
    def test_valid(self) -> None:
        c = candle()
        assert c.close == Decimal(105)
        assert c.close_time == T0 + timedelta(minutes=1)

    @pytest.mark.parametrize(
        "bad",
        [
            {"high": "104"},  # high below close
            {"low": "101"},  # low above open
            {"low": "0"},
            {"volume": "-1"},
            {"interval": timedelta(0)},
        ],
    )
    def test_rejects_inconsistent_ohlcv(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            candle(**bad)


class TestTrade:
    def test_valid(self) -> None:
        t = Trade(
            instrument_id=IID,
            trade_id="1",
            ts=T0,
            price=Decimal(100),
            quantity=Decimal("0.5"),
            aggressor=Side.BUY,
        )
        assert t.notional == Decimal(50)

    def test_rejects_zero_quantity(self) -> None:
        with pytest.raises(ValidationError):
            Trade(instrument_id=IID, trade_id="1", ts=T0, price=Decimal(1), quantity=Decimal(0))


class TestOrderBook:
    def book(self, bids: list[BookLevel], asks: list[BookLevel]) -> OrderBookSnapshot:
        return OrderBookSnapshot(instrument_id=IID, ts=T0, bids=tuple(bids), asks=tuple(asks))

    def test_best_prices_mid_and_spread(self) -> None:
        b = self.book([lvl("99", "1"), lvl("98", "2")], [lvl("101", "1"), lvl("102", "3")])
        assert b.best_bid == lvl("99", "1")
        assert b.best_ask == lvl("101", "1")
        assert b.mid == Decimal(100)
        assert b.spread == Decimal(2)

    def test_rejects_crossed_book(self) -> None:
        with pytest.raises(ValidationError, match="crossed"):
            self.book([lvl("101", "1")], [lvl("100", "1")])

    def test_rejects_locked_book(self) -> None:
        with pytest.raises(ValidationError, match="crossed"):
            self.book([lvl("100", "1")], [lvl("100", "1")])

    def test_rejects_unsorted_levels(self) -> None:
        with pytest.raises(ValidationError, match="sorted"):
            self.book([lvl("98", "1"), lvl("99", "1")], [lvl("101", "1")])
        with pytest.raises(ValidationError, match="sorted"):
            self.book([lvl("99", "1")], [lvl("102", "1"), lvl("101", "1")])

    def test_empty_side_has_no_mid(self) -> None:
        b = self.book([], [lvl("101", "1")])
        assert b.best_bid is None
        assert b.mid is None
        assert b.spread is None


class TestDerivativeData:
    def test_funding_rate_may_be_negative(self) -> None:
        f = FundingRate(
            instrument_id=IID, ts=T0, rate=Decimal("-0.0001"), interval=timedelta(hours=1)
        )
        assert f.rate < 0

    def test_mark_price_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            MarkPrice(instrument_id=IID, ts=T0, mark=Decimal(0))
