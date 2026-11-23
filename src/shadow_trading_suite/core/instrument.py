"""Exchange-neutral instrument definitions for spot, perpetual and dated-future markets.

Account eligibility is deliberately NOT part of an instrument; see instruments.eligibility.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from shadow_trading_suite.core.model import DomainModel, LeverageDec, NonNegativeDec, PositiveDec
from shadow_trading_suite.core.time import UtcDatetime


class Venue(StrEnum):
    COINBASE = "coinbase"
    CRYPTOCOM = "cryptocom"


class InstrumentKind(StrEnum):
    SPOT = "spot"
    PERPETUAL = "perpetual"
    DATED_FUTURE = "dated_future"


class AssetClass(StrEnum):
    """Underlying asset class. Venues list tokenised equities and commodities too."""

    CRYPTO = "crypto"
    EQUITY = "equity"
    COMMODITY = "commodity"
    FX = "fx"
    INDEX = "index"
    UNKNOWN = "unknown"


class VenueDetails(DomainModel):
    """Base for exchange-specific static metadata (subclassed per venue under exchanges/).

    Holds raw venue values as published. Derived figures are exposed as properties, never
    stored as if published. Dynamic market values (prices, volumes, funding, open interest)
    are not metadata and are not stored here.
    """

    unrecognized: dict[str, str] = Field(
        default_factory=dict,
        description="Unknown scalar fields preserved verbatim for forward compatibility.",
    )


Symbol = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^\S+$")]
Currency = Annotated[str, Field(min_length=1, max_length=20, pattern=r"^\S+$")]


class InstrumentId(DomainModel):
    venue: Venue
    symbol: Symbol

    def __str__(self) -> str:
        return f"{self.venue.value}:{self.symbol}"


class Instrument(DomainModel):
    id: InstrumentId
    kind: InstrumentKind
    asset_class: AssetClass = AssetClass.CRYPTO
    base: Currency
    quote: Currency
    settle: Currency | None = None
    tick_size: PositiveDec
    lot_size: PositiveDec
    min_qty: NonNegativeDec
    min_notional: NonNegativeDec | None = None
    contract_size: PositiveDec = Decimal(1)
    published_max_leverage: LeverageDec | None = Field(
        default=None,
        description=(
            "Maximum leverage as published by the venue for this product, if published. "
            "Informational only: not an account entitlement and never used for live trading. "
            "Paper risk limits apply their own, lower caps."
        ),
    )
    expiry: UtcDatetime | None = None
    sub_venue: str | None = None
    listed_tradable: bool
    venue_details: VenueDetails | None = None

    @model_validator(mode="after")
    def _kind_consistency(self) -> Self:
        if self.kind is InstrumentKind.SPOT:
            if self.settle is not None or self.expiry is not None:
                raise ValueError("spot instruments have no settle currency or expiry")
            if self.published_max_leverage is not None or self.contract_size != 1:
                raise ValueError(
                    "spot instruments are unleveraged (no published_max_leverage) "
                    "and have contract_size=1"
                )
        else:
            if self.settle is None:
                raise ValueError(f"{self.kind.value} requires a settle currency")
            if self.kind is InstrumentKind.PERPETUAL and self.expiry is not None:
                raise ValueError("perpetual instruments must not have an expiry")
            if self.kind is InstrumentKind.DATED_FUTURE and self.expiry is None:
                raise ValueError("dated futures require an expiry")
        return self

    @property
    def is_derivative(self) -> bool:
        return self.kind is not InstrumentKind.SPOT
