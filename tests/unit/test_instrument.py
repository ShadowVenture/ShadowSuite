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
    VenueDetails,
)

BTC_USD = InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD")


def spot(**overrides: Any) -> Instrument:
    fields: dict[str, Any] = {
        "id": BTC_USD,
        "kind": InstrumentKind.SPOT,
        "base": "BTC",
        "quote": "USD",
        "tick_size": "0.01",
        "lot_size": "0.00000001",
        "min_qty": "0.00000001",
        "listed_tradable": True,
    }
    fields.update(overrides)
    return Instrument.model_validate(fields)


def perp(**overrides: Any) -> Instrument:
    fields: dict[str, Any] = {
        "id": InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTCUSD-PERP"),
        "kind": InstrumentKind.PERPETUAL,
        "base": "BTC",
        "quote": "USD",
        "settle": "USD",
        "tick_size": "0.1",
        "lot_size": "0.0001",
        "min_qty": "0.0001",
        "published_max_leverage": "50",
        "listed_tradable": True,
    }
    fields.update(overrides)
    return Instrument.model_validate(fields)


class TestInstrumentId:
    def test_str_is_venue_qualified(self) -> None:
        assert str(BTC_USD) == "coinbase:BTC-USD"

    @pytest.mark.parametrize("symbol", ["", " ", "BTC USD", "BTC-USD\n"])
    def test_rejects_blank_or_whitespace_symbol(self, symbol: str) -> None:
        with pytest.raises(ValidationError):
            InstrumentId(venue=Venue.COINBASE, symbol=symbol)

    def test_is_hashable_and_immutable(self) -> None:
        assert {BTC_USD: 1}[InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD")] == 1
        with pytest.raises(ValidationError):
            BTC_USD.symbol = "ETH-USD"  # type: ignore[misc]


class TestSpot:
    def test_valid_spot_uses_exact_decimals(self) -> None:
        inst = spot()
        assert inst.tick_size == Decimal("0.01")
        assert inst.published_max_leverage is None
        assert inst.contract_size == Decimal(1)
        assert not inst.is_derivative

    @pytest.mark.parametrize(
        "bad",
        [
            {"settle": "USD"},
            {"expiry": datetime(2027, 1, 1, tzinfo=UTC)},
            {"published_max_leverage": "2"},
            {"contract_size": "0.01"},
        ],
    )
    def test_rejects_derivative_only_fields(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            spot(**bad)


class TestDerivatives:
    def test_valid_perpetual(self) -> None:
        inst = perp()
        assert inst.is_derivative
        assert inst.published_max_leverage == Decimal(50)

    def test_perpetual_requires_settle_currency(self) -> None:
        with pytest.raises(ValidationError, match="settle"):
            perp(settle=None)

    def test_perpetual_must_not_expire(self) -> None:
        with pytest.raises(ValidationError, match="expiry"):
            perp(expiry=datetime(2027, 1, 1, tzinfo=UTC))

    def test_dated_future_requires_expiry(self) -> None:
        with pytest.raises(ValidationError, match="expiry"):
            perp(kind=InstrumentKind.DATED_FUTURE)

    def test_valid_dated_future(self) -> None:
        inst = perp(
            id=InstrumentId(venue=Venue.COINBASE, symbol="BIT-30OCT26-CDE"),
            kind=InstrumentKind.DATED_FUTURE,
            expiry=datetime(2026, 10, 30, 16, tzinfo=UTC),
            sub_venue="FCM",
        )
        assert inst.expiry == datetime(2026, 10, 30, 16, tzinfo=UTC)


class TestSizingFields:
    @pytest.mark.parametrize("field", ["tick_size", "lot_size", "contract_size"])
    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_steps_must_be_positive(self, field: str, value: str) -> None:
        with pytest.raises(ValidationError):
            perp(**{field: value})

    def test_leverage_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            perp(published_max_leverage="0.5")

    def test_float_inputs_rejected(self) -> None:
        with pytest.raises(ValidationError, match="float"):
            spot(tick_size=0.01)


class TestExchangeNeutralDefinition:
    """C1 regression: account eligibility is not part of the instrument definition."""

    def test_has_no_account_eligibility_fields(self) -> None:
        assert not {"account_access", "eligibility"} & set(Instrument.model_fields)
        assert not hasattr(Instrument, "tradable_for_account")

    def test_rejects_eligibility_input(self) -> None:
        with pytest.raises(ValidationError, match="account_access"):
            spot(account_access="available")


class TestAssetClass:
    def test_defaults_to_crypto(self) -> None:
        assert spot().asset_class is AssetClass.CRYPTO

    def test_non_crypto_and_unknown_classes_supported(self) -> None:
        assert perp(asset_class=AssetClass.EQUITY).asset_class is AssetClass.EQUITY
        assert perp(asset_class="unknown").asset_class is AssetClass.UNKNOWN


class TestPublishedLeverage:
    """No claim that a venue's published leverage is available to the account."""

    def test_derivative_may_have_no_published_leverage(self) -> None:
        assert perp(published_max_leverage=None).published_max_leverage is None

    def test_old_ambiguous_field_name_removed(self) -> None:
        assert "max_leverage" not in Instrument.model_fields
        with pytest.raises(ValidationError, match="max_leverage"):
            perp(max_leverage="100")

    def test_field_documents_it_is_not_an_account_entitlement(self) -> None:
        description = Instrument.model_fields["published_max_leverage"].description or ""
        assert "not an account entitlement" in description
        assert "live" in description


class TestVenueDetailsSlot:
    def test_defaults_to_none(self) -> None:
        assert spot().venue_details is None

    def test_subclass_instance_preserved(self) -> None:
        class _Details(VenueDetails):
            flag: bool

        inst = spot(venue_details=_Details(flag=True))
        assert isinstance(inst.venue_details, _Details)
        assert inst.venue_details.flag is True

    def test_unrecognized_defaults_empty(self) -> None:
        assert VenueDetails().unrecognized == {}
