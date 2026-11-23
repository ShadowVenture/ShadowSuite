"""Base model and constrained Decimal types shared by all domain objects."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from shadow_trading_suite.core.decimal import Dec

PositiveDec = Annotated[Dec, Field(gt=0)]
NonNegativeDec = Annotated[Dec, Field(ge=0)]
LeverageDec = Annotated[Dec, Field(ge=1)]


class DomainModel(BaseModel):
    """Immutable, closed-schema base for domain values."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)
