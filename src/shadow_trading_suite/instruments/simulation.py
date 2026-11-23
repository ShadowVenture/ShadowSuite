"""Per-bot PAPER-simulation permission.

Independent of live-account eligibility by design: a bot may simulate instruments the
account could not trade live (e.g. Crypto.com derivatives in Germany), using public market
data only. This permission never enables live orders, private APIs or credentials — the
model has no field that could express any of those. Default: deny everything.
"""

from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, model_validator

from shadow_trading_suite.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentId,
    InstrumentKind,
    Venue,
)
from shadow_trading_suite.core.model import DomainModel, LeverageDec


class SimulationPermission(DomainModel):
    bot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    venue: Venue
    instrument_ids: frozenset[InstrumentId] = frozenset()
    instrument_kinds: frozenset[InstrumentKind] = frozenset()
    asset_classes: frozenset[AssetClass] = frozenset({AssetClass.CRYPTO})

    @model_validator(mode="after")
    def _ids_on_bot_venue(self) -> Self:
        foreign = sorted(str(i) for i in self.instrument_ids if i.venue is not self.venue)
        if foreign:
            raise ValueError(f"instrument_ids not on venue {self.venue.value}: {foreign}")
        return self

    def allows(self, inst: Instrument) -> bool:
        """True only if explicitly allowlisted by id or kind, on this bot's venue."""
        return (
            inst.id.venue is self.venue
            and inst.asset_class in self.asset_classes
            and (inst.id in self.instrument_ids or inst.kind in self.instrument_kinds)
        )


MAX_SIMULATED_LEVERAGE = Decimal(100)
"""Hard ceiling for HYPOTHETICAL paper leverage. Not a venue or account capability."""


class SimulationNotPermittedError(ValueError):
    pass


class BotSimulationProfile(DomainModel):
    """Per-bot paper-simulation profile: what may be simulated and at what hypothetical leverage."""

    bot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    permission: SimulationPermission
    max_simulated_leverage: LeverageDec = Field(le=MAX_SIMULATED_LEVERAGE)

    @model_validator(mode="after")
    def _same_bot(self) -> Self:
        if self.permission.bot_id != self.bot_id:
            raise ValueError("permission.bot_id must equal bot_id")
        return self


class SimulatedLeverage(DomainModel):
    """A hypothetical leverage decision. Independent of account eligibility by construction."""

    value: LeverageDec
    hypothetical: Literal[True] = True
    venue_published_max: LeverageDec | None  # informational; None = venue publishes none
    beyond_venue_published: bool | None  # None when the venue publishes no maximum


def simulated_leverage(
    profile: BotSimulationProfile, inst: Instrument, requested: Decimal
) -> SimulatedLeverage:
    if not profile.permission.allows(inst):
        raise SimulationNotPermittedError(f"{inst.id} is not permitted for {profile.bot_id}")
    if inst.kind is InstrumentKind.SPOT and requested != 1:
        raise SimulationNotPermittedError("spot simulations are unleveraged (leverage must be 1)")
    if not Decimal(1) <= requested <= profile.max_simulated_leverage:
        raise SimulationNotPermittedError(
            f"simulated leverage {requested} outside 1..{profile.max_simulated_leverage}"
        )
    published = inst.published_max_leverage
    return SimulatedLeverage(
        value=requested,
        venue_published_max=published,
        beyond_venue_published=None if published is None else requested > published,
    )
