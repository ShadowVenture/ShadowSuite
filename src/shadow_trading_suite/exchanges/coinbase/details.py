"""Coinbase-specific static product metadata (raw values as published by the venue)."""

import re
from collections.abc import Mapping
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from pydantic import Field

from shadow_trading_suite.core.decimal import Dec
from shadow_trading_suite.core.instrument import VenueDetails
from shadow_trading_suite.core.model import DomainModel
from shadow_trading_suite.exchanges import parsing as p


class CoinbaseMarginRate(DomainModel):
    long_margin_rate: Dec | None = None
    short_margin_rate: Dec | None = None


class CoinbaseFutureDetails(DomainModel):
    venue: str | None = None
    contract_code: str | None = None
    contract_root_unit: str | None = None
    contract_display_name: str | None = None
    contract_size: Dec | None = None
    contract_expiry_type: str | None = None
    contract_expiry: str | None = None  # raw string as published
    contract_expiry_timezone: str | None = None
    contract_expiry_name: str | None = None
    funding_interval: str | None = None  # raw, e.g. "3600s"
    risk_managed_by: str | None = None
    futures_asset_type: str | None = None
    futures_asset_types: tuple[str, ...] = ()
    trading_hours_type: str | None = None
    underlying_type: str | None = None
    twenty_four_by_seven: bool | None = None
    non_crypto: bool | None = None
    published_max_leverage: Dec | None = None  # perpetual_details.max_leverage, if published
    intraday_margin_rate: CoinbaseMarginRate | None = None
    overnight_margin_rate: CoinbaseMarginRate | None = None
    unrecognized: dict[str, str] = Field(default_factory=dict)

    @property
    def derived_max_leverage_from_overnight_margin(self) -> Decimal | None:
        """DERIVED, not published: floor(1 / worst overnight margin rate). None if unknown."""
        rate = self.overnight_margin_rate
        if rate is None or rate.long_margin_rate is None or rate.short_margin_rate is None:
            return None
        worst = max(rate.long_margin_rate, rate.short_margin_rate)
        if worst <= 0:
            return None
        return max((1 / worst).to_integral_value(rounding=ROUND_FLOOR), Decimal(1))


class CoinbaseDetails(VenueDetails):
    product_type: str
    product_venue: str | None = None
    status: str | None = None
    display_name: str | None = None
    is_disabled: bool | None = None
    trading_disabled: bool | None = None
    cancel_only: bool | None = None
    limit_only: bool | None = None
    post_only: bool | None = None
    auction_mode: bool | None = None
    view_only: bool | None = None
    price_increment: Dec | None = None
    quote_increment: Dec | None = None
    base_increment: Dec | None = None
    base_min_size: Dec | None = None
    base_max_size: Dec | None = None
    quote_min_size: Dec | None = None
    quote_max_size: Dec | None = None
    alias: str | None = None
    alias_to: tuple[str, ...] = ()
    future: CoinbaseFutureDetails | None = None


_PRODUCT_DECIMALS = (
    "price_increment",
    "quote_increment",
    "base_increment",
    "base_min_size",
    "base_max_size",
    "quote_min_size",
    "quote_max_size",
)
_PRODUCT_FLAGS = (
    "is_disabled",
    "trading_disabled",
    "cancel_only",
    "limit_only",
    "post_only",
    "auction_mode",
    "view_only",
)
_PRODUCT_TEXT = ("product_venue", "status", "display_name", "alias")
# Normalised elsewhere, dynamic market data, or presentation-only: not preserved.
_PRODUCT_NOT_METADATA = frozenset(
    {
        "product_id",
        "product_type",
        "base_currency_id",
        "quote_currency_id",
        "alias_to",
        "future_product_details",
        "fcm_trading_session_details",
        "price",
        "price_percentage_change_24h",
        "volume_24h",
        "volume_percentage_change_24h",
        "mid_market_price",
        "approximate_quote_24h_volume",
        "market_cap",
        "best_bid_price",
        "best_ask_price",
        "high_24h",
        "low_24h",
        "watched",
        "new",
        "new_at",
        "icon_color",
        "icon_url",
        "about_description",
        "hidden_from_discovery",
        "is_alpha_testing",
        "base_name",
        "quote_name",
        "base_display_symbol",
        "quote_display_symbol",
        "display_name_overwrite",
        "base_cbrn",
        "quote_cbrn",
        "product_cbrn",
    }
)

_FUTURE_TEXT = (
    "venue",
    "contract_code",
    "contract_root_unit",
    "contract_display_name",
    "contract_expiry_type",
    "contract_expiry",
    "contract_expiry_timezone",
    "contract_expiry_name",
    "funding_interval",
    "risk_managed_by",
    "futures_asset_type",
    "trading_hours_type",
)
_FUTURE_NOT_METADATA = frozenset(
    {
        "contract_size",
        "futures_asset_types",
        "twenty_four_by_seven",
        "non_crypto",
        "perpetual_details",
        "intraday_margin_rate",
        "overnight_margin_rate",
        "open_interest",
        "funding_rate",
        "funding_time",
        "index_price",
        "settlement_price",
        "time_to_expiry_ms",
        "display_name",
        "group_description",
        "group_short_description",
        "region_enabled",
        "product_group_cbrn",
        "contract_code_display_name",
        "deribit_product_details",
    }
)


def _margin(value: Any) -> CoinbaseMarginRate | None:
    if not isinstance(value, Mapping):
        return None
    return CoinbaseMarginRate(
        long_margin_rate=p.opt_decimal(value.get("long_margin_rate")),
        short_margin_rate=p.opt_decimal(value.get("short_margin_rate")),
    )


def _future(details: Mapping[str, Any]) -> CoinbaseFutureDetails:
    perp = details.get("perpetual_details")
    perp = perp if isinstance(perp, Mapping) else {}
    asset_types = details.get("futures_asset_types")
    fields: dict[str, Any] = {k: p.opt_text(details.get(k)) for k in _FUTURE_TEXT}
    fields.update(
        contract_size=p.opt_decimal(details.get("contract_size")),
        futures_asset_types=tuple(
            t for t in (asset_types if isinstance(asset_types, list) else []) if isinstance(t, str)
        ),
        underlying_type=p.opt_text(perp.get("underlying_type")),
        twenty_four_by_seven=p.opt_bool(details.get("twenty_four_by_seven")),
        non_crypto=p.opt_bool(details.get("non_crypto")),
        published_max_leverage=p.opt_decimal(perp.get("max_leverage")),
        intraday_margin_rate=_margin(details.get("intraday_margin_rate")),
        overnight_margin_rate=_margin(details.get("overnight_margin_rate")),
        unrecognized=p.unrecognized_scalars(
            details, frozenset(_FUTURE_TEXT) | _FUTURE_NOT_METADATA
        ),
    )
    return CoinbaseFutureDetails.model_validate(fields)


def parse_details(product: Mapping[str, Any]) -> CoinbaseDetails:
    future = product.get("future_product_details")
    alias_to = product.get("alias_to")
    consumed = frozenset(_PRODUCT_DECIMALS + _PRODUCT_FLAGS + _PRODUCT_TEXT) | _PRODUCT_NOT_METADATA
    fields: dict[str, Any] = {k: p.opt_text(product.get(k)) for k in _PRODUCT_TEXT}
    fields.update({k: p.opt_bool(product.get(k)) for k in _PRODUCT_FLAGS})
    fields.update({k: p.opt_decimal(product.get(k)) for k in _PRODUCT_DECIMALS})
    fields.update(
        product_type=p.text(product.get("product_type"), "product_type"),
        alias_to=tuple(
            a for a in (alias_to if isinstance(alias_to, list) else []) if isinstance(a, str)
        ),
        future=_future(future) if isinstance(future, Mapping) else None,
        unrecognized=p.unrecognized_scalars(product, consumed),
    )
    return CoinbaseDetails.model_validate(fields)


_MONTHS = {
    m: i
    for i, m in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1
    )
}
_ENCODED_EXPIRY = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")


def encoded_expiry_date(symbol: str) -> date | None:
    """Expiry date encoded in a Coinbase futures product id, e.g. BIT-30OCT26-CDE → 2026-10-30."""
    match = _ENCODED_EXPIRY.search(symbol)
    if match is None or match.group(2) not in _MONTHS:
        return None
    day, month, year = int(match.group(1)), _MONTHS[match.group(2)], 2000 + int(match.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None
