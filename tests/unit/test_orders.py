from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.orders import (
    Liquidity,
    OrderIntent,
    OrderType,
    Side,
    SimulatedFill,
)

IID = InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTC_USD")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def intent(**overrides: Any) -> OrderIntent:
    fields: dict[str, Any] = {
        "intent_id": "i-1",
        "strategy_id": "s-1",
        "instrument_id": IID,
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "quantity": "0.5",
        "limit_price": "100",
        "created_at": T0,
    }
    fields.update(overrides)
    return OrderIntent.model_validate(fields)


class TestOrderIntent:
    def test_valid_limit(self) -> None:
        i = intent()
        assert i.leverage == Decimal(1)
        assert not i.reduce_only

    def test_limit_requires_price(self) -> None:
        with pytest.raises(ValidationError, match="limit_price"):
            intent(limit_price=None)

    def test_market_must_not_have_price(self) -> None:
        with pytest.raises(ValidationError, match="limit_price"):
            intent(order_type=OrderType.MARKET)

    @pytest.mark.parametrize("bad", [{"quantity": "0"}, {"limit_price": "-1"}, {"leverage": "0.5"}])
    def test_rejects_non_positive_values(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            intent(**bad)

    def test_is_immutable(self) -> None:
        with pytest.raises(ValidationError):
            intent().quantity = Decimal(5)  # type: ignore[misc]


class TestSimulatedFill:
    def fill(self, **overrides: Any) -> SimulatedFill:
        fields: dict[str, Any] = {
            "fill_id": "f-1",
            "intent_id": "i-1",
            "instrument_id": IID,
            "side": Side.SELL,
            "price": "100",
            "quantity": "0.5",
            "fee": "0.05",
            "fee_currency": "USD",
            "liquidity": Liquidity.TAKER,
            "ts": T0,
        }
        fields.update(overrides)
        return SimulatedFill.model_validate(fields)

    def test_is_always_marked_simulated(self) -> None:
        f = self.fill()
        assert f.simulated is True
        assert f.notional == Decimal(50)

    def test_cannot_be_marked_real(self) -> None:
        with pytest.raises(ValidationError):
            self.fill(simulated=False)

    def test_maker_rebate_allows_negative_fee(self) -> None:
        assert self.fill(fee="-0.01", liquidity=Liquidity.MAKER).fee == Decimal("-0.01")
