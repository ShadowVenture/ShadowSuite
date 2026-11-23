"""Crypto.com-specific static instrument metadata (raw values as published by the venue)."""

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

from shadow_trading_suite.core.decimal import Dec
from shadow_trading_suite.core.instrument import VenueDetails
from shadow_trading_suite.exchanges import parsing as p


class CryptoComDetails(VenueDetails):
    inst_type: str
    product_type: str | None = None  # null for some live instruments; preserved as None
    display_name: str | None = None
    quote_decimals: int | None = None
    quantity_decimals: int | None = None
    # Raw venue figure. For spot pairs this is the venue's margin-trading leverage; it is
    # not an account entitlement and paper spot is unleveraged.
    published_max_leverage: Dec | None = None
    margin_buy_enabled: bool | None = None
    margin_sell_enabled: bool | None = None
    underlying_symbol: str | None = None
    contract_size: Dec | None = None
    expiry_timestamp_ms: int | None = None
    beta_product: bool | None = None


_TEXT = ("product_type", "display_name", "underlying_symbol")
_INTS = ("quote_decimals", "quantity_decimals", "expiry_timestamp_ms")
_FLAGS = ("margin_buy_enabled", "margin_sell_enabled", "beta_product")
# Normalised onto the Instrument elsewhere.
_NORMALISED = frozenset(
    {
        "symbol",
        "inst_type",
        "base_ccy",
        "quote_ccy",
        "price_tick_size",
        "qty_tick_size",
        "tradable",
        "max_leverage",
        "contract_size",
    }
)


def parse_details(raw: Mapping[str, Any]) -> CryptoComDetails:
    fields: dict[str, Any] = {k: p.opt_text(raw.get(k)) for k in _TEXT}
    fields.update({k: p.opt_int(raw.get(k)) for k in _INTS})
    fields.update({k: p.opt_bool(raw.get(k)) for k in _FLAGS})
    fields.update(
        inst_type=p.text(raw.get("inst_type"), "inst_type"),
        published_max_leverage=p.opt_decimal(raw.get("max_leverage")),
        contract_size=p.opt_decimal(raw.get("contract_size")),
        unrecognized=p.unrecognized_scalars(raw, frozenset(_TEXT + _INTS + _FLAGS) | _NORMALISED),
    )
    return CryptoComDetails.model_validate(fields)


_ENCODED_EXPIRY = re.compile(r"-(\d{2})(\d{2})(\d{2})$")


def encoded_expiry_date(symbol: str) -> date | None:
    """Expiry date encoded in a Crypto.com futures symbol, e.g. BTCUSD-261030 → 2026-10-30."""
    match = _ENCODED_EXPIRY.search(symbol)
    if match is None:
        return None
    try:
        return date(2000 + int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
