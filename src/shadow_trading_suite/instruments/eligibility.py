"""Live-account eligibility, kept separate from exchange-neutral instrument definitions.

Eligibility answers "could the owner's real account trade this?" It is informational:
live trading is permanently disabled in this project, and paper-simulation permission is
a separate, per-bot concept (`instruments.simulation`).

Invariants (enforced by the model): UNKNOWN never implies AVAILABLE; only an owner
verification can make something AVAILABLE; jurisdiction policy can only restrict.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from shadow_trading_suite.core.instrument import Instrument, InstrumentId, Venue
from shadow_trading_suite.core.model import DomainModel
from shadow_trading_suite.core.time import UtcDatetime
from shadow_trading_suite.exchanges.coinbase import eligibility as coinbase_rules
from shadow_trading_suite.exchanges.cryptocom import eligibility as cryptocom_rules


class EligibilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class EligibilitySource(StrEnum):
    DEFAULT = "default"  # nothing known about the account
    JURISDICTION_POLICY = "jurisdiction_policy"  # conservative configuration, not evidence
    OWNER_VERIFIED = "owner_verified"  # owner checked their own account


DEFAULT_REASON = "No account-level verification. Public listing does not imply account eligibility."


class Eligibility(DomainModel):
    instrument_id: InstrumentId
    status: EligibilityStatus
    source: EligibilitySource
    reason: str = Field(min_length=1)
    evaluated_at: UtcDatetime
    verified_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.status is EligibilityStatus.AVAILABLE and (
            self.source is not EligibilitySource.OWNER_VERIFIED
        ):
            raise ValueError("only an owner verification can mark an instrument available")
        if (
            self.source is EligibilitySource.DEFAULT
            and self.status is not EligibilityStatus.UNKNOWN
        ):
            raise ValueError("default eligibility is always unknown")
        if (
            self.source is EligibilitySource.JURISDICTION_POLICY
            and self.status is not EligibilityStatus.UNAVAILABLE
        ):
            raise ValueError("jurisdiction policy can only restrict")
        if (self.source is EligibilitySource.OWNER_VERIFIED) != (self.verified_at is not None):
            raise ValueError("verified_at is required for, and only for, owner verification")
        return self

    @property
    def is_available(self) -> bool:
        return self.status is EligibilityStatus.AVAILABLE


class OwnerVerification(DomainModel):
    """The owner's own check of their account (e.g. account UI), with a timestamp."""

    instrument_id: InstrumentId
    status: Literal[EligibilityStatus.AVAILABLE, EligibilityStatus.UNAVAILABLE]
    verified_at: UtcDatetime
    note: str = Field(min_length=1)


PolicyRule = Callable[[Instrument], str | None]
"""Returns a restriction reason, or None if the rule does not apply."""


@dataclass(frozen=True)
class JurisdictionPolicy:
    jurisdiction: Literal["DE"]
    description: str
    rules: Mapping[Venue, tuple[PolicyRule, ...]]

    def restriction(self, inst: Instrument) -> str | None:
        for rule in self.rules.get(inst.id.venue, ()):
            reason = rule(inst)
            if reason is not None:
                return reason
        return None


def germany_policy() -> JurisdictionPolicy:
    return JurisdictionPolicy(
        jurisdiction="DE",
        description=(
            "Conservative, revisable jurisdiction policy for a German account. "
            "Policy configuration; not account-verified evidence."
        ),
        rules={
            Venue.COINBASE: coinbase_rules.DE_RULES,
            Venue.CRYPTOCOM: cryptocom_rules.DE_RULES,
        },
    )


@dataclass(frozen=True)
class EligibilityResolver:
    """Precedence: verified unavailable > policy restriction > verified available > unknown."""

    policy: JurisdictionPolicy
    verifications: Mapping[InstrumentId, OwnerVerification] = field(default_factory=dict)

    def resolve(self, inst: Instrument, now: datetime) -> Eligibility:
        verification = self.verifications.get(inst.id)
        if verification is not None and verification.status is EligibilityStatus.UNAVAILABLE:
            return self._verified(inst, verification, now)
        restriction = self.policy.restriction(inst)
        if restriction is not None:
            return Eligibility(
                instrument_id=inst.id,
                status=EligibilityStatus.UNAVAILABLE,
                source=EligibilitySource.JURISDICTION_POLICY,
                reason=restriction,
                evaluated_at=now,
            )
        if verification is not None:
            return self._verified(inst, verification, now)
        return Eligibility(
            instrument_id=inst.id,
            status=EligibilityStatus.UNKNOWN,
            source=EligibilitySource.DEFAULT,
            reason=DEFAULT_REASON,
            evaluated_at=now,
        )

    def conflicts(self, inst: Instrument) -> bool:
        """An owner verification says AVAILABLE but the conservative policy restricts it."""
        verification = self.verifications.get(inst.id)
        return (
            verification is not None
            and verification.status is EligibilityStatus.AVAILABLE
            and self.policy.restriction(inst) is not None
        )

    @staticmethod
    def _verified(inst: Instrument, v: OwnerVerification, now: datetime) -> Eligibility:
        return Eligibility(
            instrument_id=inst.id,
            status=v.status,
            source=EligibilitySource.OWNER_VERIFIED,
            reason=f"Owner verification: {v.note}",
            evaluated_at=now,
            verified_at=v.verified_at,
        )
