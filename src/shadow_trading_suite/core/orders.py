"""Order intents produced by strategies and fills produced by the paper simulator.

There is deliberately no type representing a real exchange order.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from shadow_trading_suite.core.decimal import Dec
from shadow_trading_suite.core.instrument import Currency, InstrumentId
from shadow_trading_suite.core.model import DomainModel, LeverageDec, PositiveDec
from shadow_trading_suite.core.time import UtcDatetime


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class OrderIntent(DomainModel):
    """A strategy's request to trade. Must pass the risk engine before paper execution."""

    intent_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    instrument_id: InstrumentId
    side: Side
    order_type: OrderType
    quantity: PositiveDec
    limit_price: PositiveDec | None = None
    reduce_only: bool = False
    leverage: LeverageDec = Decimal(1)
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _price_matches_type(self) -> Self:
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market orders must not set limit_price")
        return self


class SimulatedFill(DomainModel):
    """A paper fill. `simulated` is fixed to True and cannot be overridden."""

    fill_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    instrument_id: InstrumentId
    side: Side
    price: PositiveDec
    quantity: PositiveDec
    fee: Dec  # negative = maker rebate
    fee_currency: Currency
    liquidity: Liquidity
    ts: UtcDatetime
    simulated: Literal[True] = True

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity
