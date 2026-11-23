"""C5: paper-simulation permission is per bot, allowlist-based and independent of eligibility."""

import ast
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from shadow_trading_suite.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentId,
    InstrumentKind,
    Venue,
)
from shadow_trading_suite.instruments import simulation
from shadow_trading_suite.instruments.eligibility import (
    EligibilityResolver,
    EligibilityStatus,
    germany_policy,
)
from shadow_trading_suite.instruments.simulation import (
    BotSimulationProfile,
    SimulationNotPermittedError,
    SimulationPermission,
    simulated_leverage,
)

T0 = datetime(2026, 10, 19, 12, tzinfo=UTC)


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


CC_SPOT = inst(Venue.CRYPTOCOM, "BTC_EUR", InstrumentKind.SPOT)
CC_PERP = inst(Venue.CRYPTOCOM, "BTCUSD-PERP", InstrumentKind.PERPETUAL)
CC_EQUITY_PERP = inst(
    Venue.CRYPTOCOM, "AAOIUSD-PERP", InstrumentKind.PERPETUAL, asset_class=AssetClass.EQUITY
)
CB_INTX = inst(
    Venue.COINBASE, "BTC-PERP-INTX", InstrumentKind.PERPETUAL, sub_venue="INTX",
    listed_tradable=False,
)  # fmt: skip
CB_FCM = inst(Venue.COINBASE, "BIT-30OCT26-CDE", InstrumentKind.DATED_FUTURE, sub_venue="FCM")
CB_SPOT = inst(Venue.COINBASE, "BTC-EUR", InstrumentKind.SPOT)


def permission(**kw: Any) -> SimulationPermission:
    fields: dict[str, Any] = {"bot_id": "cryptocom-short-term", "venue": Venue.CRYPTOCOM}
    fields.update(kw)
    return SimulationPermission.model_validate(fields)


class TestDefaultDeny:
    def test_empty_permission_allows_nothing(self) -> None:
        p = permission()
        assert not any(p.allows(i) for i in (CC_SPOT, CC_PERP, CC_EQUITY_PERP))


class TestAllowlisting:
    def test_by_instrument_id(self) -> None:
        p = permission(instrument_ids=frozenset({CC_SPOT.id}))
        assert p.allows(CC_SPOT)
        assert not p.allows(CC_PERP)

    def test_by_instrument_kind(self) -> None:
        p = permission(instrument_kinds=frozenset({InstrumentKind.PERPETUAL}))
        assert p.allows(CC_PERP)
        assert not p.allows(CC_SPOT)

    def test_other_venue_never_allowed(self) -> None:
        p = permission(instrument_kinds=frozenset(InstrumentKind))
        assert not p.allows(CB_SPOT)

    def test_non_crypto_requires_explicit_asset_class(self) -> None:
        kinds = frozenset({InstrumentKind.PERPETUAL})
        assert not permission(instrument_kinds=kinds).allows(CC_EQUITY_PERP)
        explicit = permission(
            instrument_kinds=kinds, asset_classes=frozenset({AssetClass.CRYPTO, AssetClass.EQUITY})
        )
        assert explicit.allows(CC_EQUITY_PERP)

    def test_instrument_ids_must_belong_to_bot_venue(self) -> None:
        with pytest.raises(ValidationError, match="venue"):
            permission(instrument_ids=frozenset({CB_SPOT.id}))

    @pytest.mark.parametrize("bot_id", ["", "Has Space", "UPPER", "x" * 65])
    def test_bot_id_format(self, bot_id: str) -> None:
        with pytest.raises(ValidationError):
            permission(bot_id=bot_id)


class TestIndependentFromEligibility:
    resolver = EligibilityResolver(policy=germany_policy())

    def test_cryptocom_derivatives_simulatable_while_ineligible(self) -> None:
        assert self.resolver.resolve(CC_PERP, T0).status is EligibilityStatus.UNAVAILABLE
        p = permission(instrument_kinds=frozenset({InstrumentKind.PERPETUAL}))
        assert p.allows(CC_PERP)

    @pytest.mark.parametrize(
        ("instrument", "status"),
        [(CB_INTX, EligibilityStatus.UNKNOWN), (CB_FCM, EligibilityStatus.UNAVAILABLE)],
    )
    def test_coinbase_derivatives_simulatable_when_unknown_or_unavailable(
        self, instrument: Instrument, status: EligibilityStatus
    ) -> None:
        assert self.resolver.resolve(instrument, T0).status is status
        p = SimulationPermission(
            bot_id="coinbase-short-term",
            venue=Venue.COINBASE,
            instrument_kinds=frozenset({InstrumentKind.PERPETUAL, InstrumentKind.DATED_FUTURE}),
        )
        assert p.allows(instrument)

    def test_module_does_not_consult_eligibility(self) -> None:
        tree = ast.parse(inspect.getsource(simulation))
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
            a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names
        }
        assert not any("eligibility" in (m or "") for m in imported)


class TestNeverEnablesLive:
    @pytest.mark.parametrize(
        "field", ["live", "live_trading", "live_trading_enabled", "private_api", "credentials"]
    )
    def test_no_live_or_private_fields_accepted(self, field: str) -> None:
        with pytest.raises(ValidationError):
            permission(**{field: True})

    def test_fields_are_simulation_only(self) -> None:
        assert set(SimulationPermission.model_fields) == {
            "bot_id",
            "venue",
            "instrument_ids",
            "instrument_kinds",
            "asset_classes",
        }

    def test_is_immutable(self) -> None:
        p = permission()
        with pytest.raises(ValidationError):
            p.instrument_kinds = frozenset(InstrumentKind)  # type: ignore[misc]


class TestHypotheticalSimulatedLeverage:
    """Req. 4: up to 100x hypothetical leverage, separate from venue leverage and eligibility."""

    def profile(self, venue: Venue = Venue.CRYPTOCOM, max_lev: str = "100") -> BotSimulationProfile:
        bot = "cdc-01" if venue is Venue.CRYPTOCOM else "bot-01"
        return BotSimulationProfile(
            bot_id=bot,
            permission=SimulationPermission(
                bot_id=bot,
                venue=venue,
                instrument_kinds=frozenset({InstrumentKind.PERPETUAL, InstrumentKind.SPOT}),
            ),
            max_simulated_leverage=Decimal(max_lev),
        )

    def test_100x_allowed_even_when_venue_publishes_less_and_flagged(self) -> None:
        perp = inst(
            Venue.CRYPTOCOM, "ETHUSD-PERP", InstrumentKind.PERPETUAL, published_max_leverage="50"
        )
        lev = simulated_leverage(self.profile(), perp, Decimal(100))
        assert lev.value == Decimal(100)
        assert lev.hypothetical is True
        assert lev.venue_published_max == Decimal(50)
        assert lev.beyond_venue_published is True

    def test_within_published_limit_not_flagged(self) -> None:
        perp = inst(
            Venue.CRYPTOCOM, "BTCUSD-PERP", InstrumentKind.PERPETUAL, published_max_leverage="100"
        )
        assert simulated_leverage(self.profile(), perp, Decimal(20)).beyond_venue_published is False

    def test_unpublished_venue_leverage_is_unknown_not_invented(self) -> None:
        lev = simulated_leverage(self.profile(), CC_PERP, Decimal(10))
        assert lev.venue_published_max is None
        assert lev.beyond_venue_published is None

    def test_eligibility_unavailable_does_not_matter(self) -> None:
        resolver = EligibilityResolver(policy=germany_policy())
        assert resolver.resolve(CC_PERP, T0).status is EligibilityStatus.UNAVAILABLE
        assert simulated_leverage(self.profile(), CC_PERP, Decimal(100)).value == Decimal(100)

    @pytest.mark.parametrize("requested", ["0.5", "100.01", "250"])
    def test_out_of_range_refused(self, requested: str) -> None:
        with pytest.raises(SimulationNotPermittedError, match="leverage"):
            simulated_leverage(self.profile(), CC_PERP, Decimal(requested))

    def test_bot_cap_below_100_enforced(self) -> None:
        with pytest.raises(SimulationNotPermittedError, match="25"):
            simulated_leverage(self.profile(max_lev="25"), CC_PERP, Decimal(30))

    def test_profile_cap_cannot_exceed_100(self) -> None:
        with pytest.raises(ValidationError):
            self.profile(max_lev="101")

    def test_spot_is_never_leveraged(self) -> None:
        with pytest.raises(SimulationNotPermittedError, match="spot"):
            simulated_leverage(self.profile(), CC_SPOT, Decimal(2))
        assert simulated_leverage(self.profile(), CC_SPOT, Decimal(1)).value == Decimal(1)

    def test_not_permitted_instrument_refused(self) -> None:
        with pytest.raises(SimulationNotPermittedError, match="not permitted"):
            simulated_leverage(self.profile(), CB_INTX, Decimal(5))

    def test_profile_and_permission_bot_must_match(self) -> None:
        with pytest.raises(ValidationError, match="bot_id"):
            BotSimulationProfile(
                bot_id="cdc-02",
                permission=SimulationPermission(bot_id="cdc-01", venue=Venue.CRYPTOCOM),
                max_simulated_leverage=Decimal(10),
            )
