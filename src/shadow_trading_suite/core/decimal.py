"""Exact decimal arithmetic helpers. Binary floats are never accepted for financial values."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Annotated

from pydantic import BeforeValidator


class InvalidDecimalError(ValueError):
    """Raised when a value cannot be represented as an exact, finite Decimal."""


def to_decimal(value: object) -> Decimal:
    """Convert int, str or Decimal to a finite Decimal. Floats and bools are rejected."""
    if isinstance(value, bool | float):
        raise InvalidDecimalError(
            f"{type(value).__name__} is not allowed for financial values; use str or Decimal"
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int | str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise InvalidDecimalError(f"not a decimal number: {value!r}") from exc
    else:
        raise InvalidDecimalError(f"unsupported type for decimal: {type(value).__name__}")
    if not result.is_finite():
        raise InvalidDecimalError(f"decimal must be finite, got {result}")
    return result


def _check_step(step: Decimal) -> None:
    if not step.is_finite() or step <= 0:
        raise InvalidDecimalError(f"step must be a positive finite decimal, got {step}")


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Largest multiple of `step` that is <= `value`."""
    _check_step(step)
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Smallest multiple of `step` that is >= `value`."""
    _check_step(step)
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def is_multiple_of(value: Decimal, step: Decimal) -> bool:
    _check_step(step)
    return value % step == 0


Dec = Annotated[Decimal, BeforeValidator(to_decimal)]
"""Pydantic field type: exact Decimal, rejects floats, bools and non-finite values."""
