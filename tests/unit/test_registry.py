from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from shadow_trading_suite.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentId,
    InstrumentKind,
    Venue,
)
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.exchanges.base import DiscoveryResult, RejectedInstrument
from shadow_trading_suite.exchanges.errors import ExchangeUnavailableError
from shadow_trading_suite.instruments.eligibility import (
    EligibilityResolver,
    EligibilitySource,
    EligibilityStatus,
    OwnerVerification,
    germany_policy,
)
from shadow_trading_suite.instruments.registry import (
    InstrumentRegistry,
    UnknownInstrumentError,
    discover_all,
    unified_symbol,
)

T0 = datetime(2026, 10, 19, 12, tzinfo=UTC)


def inst(
    venue: Venue, symbol: str, kind: InstrumentKind = InstrumentKind.SPOT, **kw: Any
) -> Instrument:
    fields: dict[str, Any] = {
        "id": InstrumentId(venue=venue, symbol=symbol),
        "kind": kind,
        "base": "BTC",
        "quote": "USD",
        "tick_size": "0.01",
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


CB_SPOT = inst(Venue.COINBASE, "BTC-USD")
CB_FCM = inst(Venue.COINBASE, "BIT-30OCT26-CDE", InstrumentKind.DATED_FUTURE, sub_venue="FCM")
CB_INTX = inst(Venue.COINBASE, "BTC-PERP-INTX", InstrumentKind.PERPETUAL, sub_venue="INTX")
CC_SPOT = inst(Venue.CRYPTOCOM, "BTC_USD")
CC_PERP = inst(Venue.CRYPTOCOM, "BTCUSD-PERP", InstrumentKind.PERPETUAL)
CC_FUT = inst(Venue.CRYPTOCOM, "BTCUSD-261030", InstrumentKind.DATED_FUTURE)
CC_EQUITY = inst(
    Venue.CRYPTOCOM, "AAOIUSD-PERP", InstrumentKind.PERPETUAL, asset_class=AssetClass.EQUITY,
    base="AAOI",
)  # fmt: skip


def result(venue: Venue, *instruments: Instrument, rejected: int = 0) -> DiscoveryResult:
    return DiscoveryResult(
        venue,
        tuple(instruments),
        tuple(RejectedInstrument(f"bad{i}", "reason") for i in range(rejected)),
    )


T_VERIFIED = datetime(2026, 10, 18, tzinfo=UTC)


def resolver(
    available: frozenset[InstrumentId] = frozenset(),
    unavailable: frozenset[InstrumentId] = frozenset(),
) -> EligibilityResolver:
    verifications = {
        iid: OwnerVerification.model_validate(
            {
                "instrument_id": iid,
                "status": status,
                "verified_at": T_VERIFIED,
                "note": "account UI",
            }
        )
        for ids, status in (
            (available, EligibilityStatus.AVAILABLE),
            (unavailable, EligibilityStatus.UNAVAILABLE),
        )
        for iid in ids
    }
    return EligibilityResolver(policy=germany_policy(), verifications=verifications)


def registry(eligibility: EligibilityResolver | None = None) -> InstrumentRegistry:
    reg = InstrumentRegistry(eligibility=eligibility or resolver(), clock=ManualClock(T0))
    reg.refresh(
        [
            result(Venue.COINBASE, CB_SPOT, CB_FCM, CB_INTX),
            result(Venue.CRYPTOCOM, CC_SPOT, CC_PERP, CC_FUT, CC_EQUITY),
        ]
    )
    return reg


class TestUnifiedSymbol:
    @pytest.mark.parametrize(
        ("instrument", "expected"),
        [
            (CB_SPOT, "BTC/USD"),
            (CC_SPOT, "BTC/USD"),
            (CC_PERP, "BTC/USD:USD"),
            (CC_FUT, "BTC/USD:USD-261030"),
        ],
    )
    def test_format(self, instrument: Instrument, expected: str) -> None:
        assert unified_symbol(instrument) == expected

    def test_cross_venue_lookup(self) -> None:
        matches = registry().by_unified("BTC/USD")
        assert [str(i.id) for i in matches] == ["coinbase:BTC-USD", "cryptocom:BTC_USD"]


class TestEligibilityIsStoredSeparately:
    def test_registry_instruments_carry_no_eligibility(self) -> None:
        assert "account_access" not in registry().get(CB_SPOT.id).model_fields_set

    def test_unverified_instruments_unknown(self) -> None:
        reg = registry()
        for iid in (CB_SPOT.id, CB_INTX.id, CC_SPOT.id):
            e = reg.eligibility(iid)
            assert e.status is EligibilityStatus.UNKNOWN
            assert e.source is EligibilitySource.DEFAULT
            assert e.evaluated_at == T0

    def test_policy_restrictions(self) -> None:
        reg = registry()
        for iid in (CB_FCM.id, CC_PERP.id, CC_FUT.id):
            e = reg.eligibility(iid)
            assert e.status is EligibilityStatus.UNAVAILABLE
            assert e.source is EligibilitySource.JURISDICTION_POLICY

    def test_owner_verification_makes_available(self) -> None:
        e = registry(resolver(available=frozenset({CB_SPOT.id}))).eligibility(CB_SPOT.id)
        assert e.is_available
        assert e.verified_at == T_VERIFIED

    def test_policy_conflict_reported_and_stays_unavailable(self) -> None:
        reg = InstrumentRegistry(
            eligibility=resolver(available=frozenset({CB_FCM.id})), clock=ManualClock(T0)
        )
        report = reg.refresh([result(Venue.COINBASE, CB_FCM)])
        assert reg.eligibility(CB_FCM.id).status is EligibilityStatus.UNAVAILABLE
        assert report.eligibility_conflicts == (CB_FCM.id,)

    def test_verified_unavailable_wins(self) -> None:
        both = frozenset({CC_SPOT.id})
        reg = registry(resolver(available=both, unavailable=both))
        assert reg.eligibility(CC_SPOT.id).status is EligibilityStatus.UNAVAILABLE

    def test_unknown_instrument_has_no_eligibility(self) -> None:
        with pytest.raises(UnknownInstrumentError):
            registry().eligibility(InstrumentId(venue=Venue.COINBASE, symbol="NOPE"))


class TestQueries:
    def test_default_find_excludes_non_crypto(self) -> None:
        symbols = {i.id.symbol for i in registry().find()}
        assert "AAOIUSD-PERP" not in symbols
        assert len(symbols) == 6

    def test_find_with_explicit_asset_classes(self) -> None:
        found = registry().find(asset_classes=frozenset({AssetClass.EQUITY}))
        assert [i.id.symbol for i in found] == ["AAOIUSD-PERP"]

    def test_find_by_venue_and_kind(self) -> None:
        found = registry().find(venue=Venue.CRYPTOCOM, kind=InstrumentKind.PERPETUAL)
        assert [i.id.symbol for i in found] == ["BTCUSD-PERP"]

    def test_find_by_eligibility(self) -> None:
        reg = registry(resolver(available=frozenset({CC_SPOT.id})))
        assert [i.id for i in reg.find(eligibility=EligibilityStatus.AVAILABLE)] == [CC_SPOT.id]
        unavailable = reg.find(eligibility=EligibilityStatus.UNAVAILABLE)
        assert {i.id.symbol for i in unavailable} == {
            "BIT-30OCT26-CDE",
            "BTCUSD-PERP",
            "BTCUSD-261030",
        }

    def test_results_are_deterministically_ordered(self) -> None:
        ids = [str(i.id) for i in registry().find()]
        assert ids == sorted(ids)

    def test_unknown_instrument(self) -> None:
        with pytest.raises(UnknownInstrumentError):
            registry().get(InstrumentId(venue=Venue.COINBASE, symbol="NOPE"))


class TestRefresh:
    def test_refresh_replaces_venue_snapshot_atomically(self) -> None:
        reg = registry()
        reg.refresh([result(Venue.COINBASE, CB_SPOT)])
        assert {i.id.symbol for i in reg.find(venue=Venue.COINBASE)} == {"BTC-USD"}
        # other venue untouched
        assert len(reg.find(venue=Venue.CRYPTOCOM)) == 3

    def test_refresh_timestamp_per_venue(self) -> None:
        clock = ManualClock(T0)
        reg = InstrumentRegistry(clock=clock)
        reg.refresh([result(Venue.COINBASE, CB_SPOT)])
        clock.advance(timedelta(minutes=5))
        reg.refresh([result(Venue.CRYPTOCOM, CC_SPOT)])
        assert reg.refreshed_at(Venue.COINBASE) == T0
        assert reg.refreshed_at(Venue.CRYPTOCOM) == T0 + timedelta(minutes=5)

    def test_never_refreshed_venue(self) -> None:
        assert InstrumentRegistry(clock=ManualClock(T0)).refreshed_at(Venue.COINBASE) is None

    def test_report_counts_rejections_and_duplicates(self) -> None:
        reg = InstrumentRegistry(clock=ManualClock(T0))
        report = reg.refresh([result(Venue.COINBASE, CB_SPOT, CB_SPOT, rejected=2)])
        assert report.counts == {Venue.COINBASE: 1}
        assert report.rejected[Venue.COINBASE] == 2
        assert report.duplicates == (CB_SPOT.id,)

    def test_result_venue_mismatch_rejected(self) -> None:
        reg = InstrumentRegistry(clock=ManualClock(T0))
        with pytest.raises(ValueError, match="venue"):
            reg.refresh([result(Venue.COINBASE, CC_SPOT)])
        assert len(reg) == 0


class _Adapter:
    def __init__(self, venue: Venue, outcome: DiscoveryResult | Exception) -> None:
        self.venue = venue
        self.outcome = outcome

    def list_instruments(self) -> DiscoveryResult:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_discover_all_isolates_venue_failures() -> None:
    results, errors = discover_all(
        [
            _Adapter(Venue.COINBASE, ExchangeUnavailableError("down")),
            _Adapter(Venue.CRYPTOCOM, result(Venue.CRYPTOCOM, CC_SPOT)),
        ]
    )
    assert [r.venue for r in results] == [Venue.CRYPTOCOM]
    assert list(errors) == [Venue.COINBASE]
    assert "down" in str(errors[Venue.COINBASE])


def test_discover_all_never_swallows_safety_violations() -> None:
    from shadow_trading_suite.exchanges.errors import TransportPolicyError

    with pytest.raises(TransportPolicyError):
        discover_all([_Adapter(Venue.COINBASE, TransportPolicyError("not allowlisted"))])
