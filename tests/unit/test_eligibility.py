"""C1/C5: account eligibility is separate, sourced, timestamped — and unknown is never available."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from shadow_trading_suite.core.instrument import Instrument, InstrumentId, InstrumentKind, Venue
from shadow_trading_suite.instruments.eligibility import (
    Eligibility,
    EligibilityResolver,
    EligibilitySource,
    EligibilityStatus,
    OwnerVerification,
    germany_policy,
)

T0 = datetime(2026, 10, 19, 12, tzinfo=UTC)
T_VERIFIED = datetime(2026, 10, 18, 9, tzinfo=UTC)


def inst(venue: Venue, symbol: str, kind: InstrumentKind, **kw: Any) -> Instrument:
    fields: dict[str, Any] = {
        "id": InstrumentId(venue=venue, symbol=symbol),
        "kind": kind,
        "base": "BTC",
        "quote": "USD",
        "tick_size": "0.1",
        "lot_size": "0.0001",
        "min_qty": "0.0001",
        "listed_tradable": True,
    }
    if kind is not InstrumentKind.SPOT:
        fields["settle"] = "USD"
    if kind is InstrumentKind.DATED_FUTURE:
        fields["expiry"] = datetime(2026, 10, 30, 8, tzinfo=UTC)
    fields.update(kw)
    return Instrument.model_validate(fields)


CB_SPOT = inst(Venue.COINBASE, "BTC-EUR", InstrumentKind.SPOT)
CB_INTX = inst(Venue.COINBASE, "BTC-PERP-INTX", InstrumentKind.PERPETUAL, sub_venue="INTX")
CB_FCM = inst(Venue.COINBASE, "BIT-30OCT26-CDE", InstrumentKind.DATED_FUTURE, sub_venue="FCM")
CC_SPOT = inst(Venue.CRYPTOCOM, "BTC_EUR", InstrumentKind.SPOT)
CC_PERP = inst(Venue.CRYPTOCOM, "BTCUSD-PERP", InstrumentKind.PERPETUAL)
CC_FUT = inst(Venue.CRYPTOCOM, "BTCUSD-261030", InstrumentKind.DATED_FUTURE)


def resolve(i: Instrument, **verifications: OwnerVerification) -> Eligibility:
    resolver = EligibilityResolver(
        policy=germany_policy(),
        verifications={v.instrument_id: v for v in verifications.values()},
    )
    return resolver.resolve(i, now=T0)


def verified(i: Instrument, status: EligibilityStatus) -> OwnerVerification:
    return OwnerVerification.model_validate(
        {"instrument_id": i.id, "status": status, "verified_at": T_VERIFIED, "note": "account UI"}
    )


class TestDefaults:
    @pytest.mark.parametrize("instrument", [CB_SPOT, CB_INTX, CC_SPOT])
    def test_unverified_instruments_are_unknown(self, instrument: Instrument) -> None:
        e = resolve(instrument)
        assert e.status is EligibilityStatus.UNKNOWN
        assert e.source is EligibilitySource.DEFAULT
        assert e.verified_at is None
        assert e.evaluated_at == T0
        assert "not imply" in e.reason

    def test_intx_stays_unknown_until_verified(self) -> None:
        assert resolve(CB_INTX).status is EligibilityStatus.UNKNOWN


class TestGermanyJurisdictionPolicy:
    def test_policy_is_labelled_as_configuration_not_evidence(self) -> None:
        policy = germany_policy()
        assert policy.jurisdiction == "DE"
        assert "not account-verified" in policy.description

    def test_coinbase_fcm_unavailable_by_policy_not_verification(self) -> None:
        e = resolve(CB_FCM)
        assert e.status is EligibilityStatus.UNAVAILABLE
        assert e.source is EligibilitySource.JURISDICTION_POLICY
        assert e.verified_at is None
        assert "not an account-specific determination" in e.reason

    @pytest.mark.parametrize("instrument", [CC_PERP, CC_FUT])
    def test_cryptocom_derivatives_unavailable_by_policy(self, instrument: Instrument) -> None:
        e = resolve(instrument)
        assert e.status is EligibilityStatus.UNAVAILABLE
        assert e.source is EligibilitySource.JURISDICTION_POLICY
        assert "revisable" in e.reason
        assert "not account-verified" in e.reason

    def test_cryptocom_spot_not_restricted_by_policy(self) -> None:
        assert resolve(CC_SPOT).status is EligibilityStatus.UNKNOWN

    def test_rules_live_in_venue_packages(self) -> None:
        rules = germany_policy().rules
        modules = {r.__module__ for venue_rules in rules.values() for r in venue_rules}
        assert modules == {
            "shadow_trading_suite.exchanges.coinbase.eligibility",
            "shadow_trading_suite.exchanges.cryptocom.eligibility",
        }


class TestOwnerVerification:
    def test_verified_available(self) -> None:
        e = resolve(CB_SPOT, a=verified(CB_SPOT, EligibilityStatus.AVAILABLE))
        assert e.status is EligibilityStatus.AVAILABLE
        assert e.source is EligibilitySource.OWNER_VERIFIED
        assert e.verified_at == T_VERIFIED

    def test_verified_unavailable_wins(self) -> None:
        e = resolve(CC_SPOT, a=verified(CC_SPOT, EligibilityStatus.UNAVAILABLE))
        assert e.status is EligibilityStatus.UNAVAILABLE
        assert e.source is EligibilitySource.OWNER_VERIFIED

    def test_policy_restriction_wins_over_verified_available_and_is_flagged(self) -> None:
        v = verified(CC_PERP, EligibilityStatus.AVAILABLE)
        resolver = EligibilityResolver(policy=germany_policy(), verifications={CC_PERP.id: v})
        assert resolver.resolve(CC_PERP, now=T0).status is EligibilityStatus.UNAVAILABLE
        assert resolver.conflicts(CC_PERP)

    def test_verification_cannot_be_unknown(self) -> None:
        with pytest.raises(ValidationError):
            OwnerVerification(
                instrument_id=CB_SPOT.id,
                status=EligibilityStatus.UNKNOWN,  # type: ignore[arg-type]
                verified_at=T_VERIFIED,
                note="x",
            )


class TestEligibilityInvariants:
    """Unknown must never imply available; AVAILABLE requires owner verification."""

    def base(self, **kw: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "instrument_id": CB_SPOT.id,
            "status": EligibilityStatus.UNKNOWN,
            "source": EligibilitySource.DEFAULT,
            "reason": "r",
            "evaluated_at": T0,
        }
        fields.update(kw)
        return fields

    @pytest.mark.parametrize(
        "bad",
        [
            {"status": EligibilityStatus.AVAILABLE},  # default source cannot grant
            {
                "status": EligibilityStatus.AVAILABLE,
                "source": EligibilitySource.JURISDICTION_POLICY,
            },
            {"status": EligibilityStatus.UNAVAILABLE},  # default is always unknown
            {"source": EligibilitySource.JURISDICTION_POLICY},  # policy only restricts
            {"source": EligibilitySource.OWNER_VERIFIED},  # needs verified_at
            {"verified_at": T_VERIFIED},  # verified_at only for owner verification
            {"reason": ""},
        ],
    )
    def test_invalid_combinations_rejected(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            Eligibility.model_validate(self.base(**bad))

    def test_is_available_only_for_available(self) -> None:
        unknown = Eligibility.model_validate(self.base())
        assert not unknown.is_available
