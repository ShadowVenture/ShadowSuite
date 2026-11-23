"""Strict helpers for reading untrusted venue JSON. Shape errors raise ResponseFormatError."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ValidationError

from shadow_trading_suite.exchanges.errors import ResponseFormatError


def obj(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResponseFormatError(f"{what}: expected object, got {type(value).__name__}")
    return value


def arr(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise ResponseFormatError(f"{what}: expected array, got {type(value).__name__}")
    return value


def text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ResponseFormatError(f"{what}: expected non-empty string")
    return value


def opt_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def integer(value: Any, what: str) -> int:
    """Accept JSON ints or digit strings (venues mix both)."""
    if isinstance(value, bool):
        raise ResponseFormatError(f"{what}: expected integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    raise ResponseFormatError(f"{what}: expected integer, got {value!r}")


def opt_decimal(value: Any) -> Decimal | None:
    """Decimal from a non-empty string/number, else None ("" and null mean 'not provided')."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(value) if isinstance(value, str | int) else value
    except InvalidOperation:
        return None
    return result if isinstance(result, Decimal) and result.is_finite() else None


def from_unix_ms(value: Any, what: str) -> datetime:
    ms = integer(value, what)
    return datetime.fromtimestamp(ms // 1000, UTC) + timedelta(milliseconds=ms % 1000)


def from_unix_s(value: Any, what: str) -> datetime:
    return datetime.fromtimestamp(integer(value, what), UTC)


def from_iso(value: Any, what: str) -> datetime:
    raw = text(value, what)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResponseFormatError(f"{what}: invalid timestamp {raw!r}") from exc
    if parsed.tzinfo is None:
        raise ResponseFormatError(f"{what}: timestamp has no timezone")
    return parsed.astimezone(UTC)


def to_unix_s(value: datetime) -> int:
    return int(value.timestamp())


def to_unix_ms(value: datetime) -> int:
    return int(value.timestamp()) * 1000 + value.microsecond // 1000


def build[M: BaseModel](model: type[M], what: str, /, **fields: Any) -> M:
    """Construct a domain model; validation failures become ResponseFormatError."""
    try:
        return model(**fields)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ResponseFormatError(f"{what}: {exc}") from exc


def opt_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


MAX_UNRECOGNIZED_FIELDS = 50
MAX_UNRECOGNIZED_VALUE_LEN = 200


def unrecognized_scalars(raw: Mapping[str, Any], consumed: frozenset[str]) -> dict[str, str]:
    """Unknown scalar fields as strings (bounded). Nested objects and nulls are skipped."""
    out: dict[str, str] = {}
    for key in sorted(raw):
        if key in consumed or len(key) > 64:
            continue
        value = raw[key]
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, str | int | Decimal):
            rendered = str(value)
        else:
            continue
        if len(rendered) <= MAX_UNRECOGNIZED_VALUE_LEN:
            out[key] = rendered
        if len(out) >= MAX_UNRECOGNIZED_FIELDS:
            break
    return out
