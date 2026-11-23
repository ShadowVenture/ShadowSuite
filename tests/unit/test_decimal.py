from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from shadow_trading_suite.core.decimal import (
    Dec,
    InvalidDecimalError,
    ceil_to_step,
    floor_to_step,
    is_multiple_of,
    to_decimal,
)

positive_steps = st.sampled_from([Decimal(s) for s in ("0.00001", "0.01", "0.5", "1", "25")])
amounts = st.decimals(min_value=0, max_value=10**9, allow_nan=False, allow_infinity=False, places=8)


class TestToDecimal:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("0.1", Decimal("0.1")), (5, Decimal(5)), (Decimal("1.50"), Decimal("1.50"))],
    )
    def test_accepts_exact_inputs(self, raw: object, expected: Decimal) -> None:
        assert to_decimal(raw) == expected

    def test_string_input_is_exact_not_binary_float(self) -> None:
        assert to_decimal("0.1") + to_decimal("0.2") == Decimal("0.3")

    @pytest.mark.parametrize("raw", [0.1, 1.0, True, None, "abc", "", "NaN", "Infinity", "-inf"])
    def test_rejects_float_bool_and_non_finite(self, raw: object) -> None:
        with pytest.raises(InvalidDecimalError):
            to_decimal(raw)

    def test_rejects_non_finite_decimal(self) -> None:
        with pytest.raises(InvalidDecimalError):
            to_decimal(Decimal("NaN"))


class TestSteps:
    def test_floor_to_step(self) -> None:
        assert floor_to_step(Decimal("1.23456"), Decimal("0.01")) == Decimal("1.23")

    def test_ceil_to_step(self) -> None:
        assert ceil_to_step(Decimal("1.23001"), Decimal("0.01")) == Decimal("1.24")

    def test_exact_multiple_is_unchanged(self) -> None:
        assert floor_to_step(Decimal("2.50"), Decimal("0.5")) == Decimal("2.5")
        assert ceil_to_step(Decimal("2.50"), Decimal("0.5")) == Decimal("2.5")

    def test_non_decimal_step_tick(self) -> None:
        assert floor_to_step(Decimal("107"), Decimal("25")) == Decimal("100")

    @pytest.mark.parametrize("step", [Decimal(0), Decimal("-0.01")])
    def test_rejects_non_positive_step(self, step: Decimal) -> None:
        with pytest.raises(InvalidDecimalError):
            floor_to_step(Decimal(1), step)

    def test_is_multiple_of(self) -> None:
        assert is_multiple_of(Decimal("0.03"), Decimal("0.01"))
        assert not is_multiple_of(Decimal("0.035"), Decimal("0.01"))

    @given(value=amounts, step=positive_steps)
    def test_floor_never_exceeds_value_and_is_on_grid(self, value: Decimal, step: Decimal) -> None:
        floored = floor_to_step(value, step)
        assert floored <= value < floored + step
        assert is_multiple_of(floored, step)

    @given(value=amounts, step=positive_steps)
    def test_ceil_never_below_value_and_is_on_grid(self, value: Decimal, step: Decimal) -> None:
        ceiled = ceil_to_step(value, step)
        assert ceiled - step < value <= ceiled
        assert is_multiple_of(ceiled, step)


class _Model(BaseModel):
    amount: Dec


class TestDecField:
    def test_parses_string(self) -> None:
        assert _Model(amount="1.10").amount == Decimal("1.10")  # type: ignore[arg-type]

    def test_rejects_float(self) -> None:
        with pytest.raises(ValidationError, match="float"):
            _Model(amount=0.1)  # type: ignore[arg-type]
