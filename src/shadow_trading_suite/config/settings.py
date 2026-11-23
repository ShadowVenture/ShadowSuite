"""Explicit, fail-closed configuration loading.

Settings are read only from the mapping passed to `load_settings` (normally `os.environ`).
No `.env` file is loaded implicitly. Booleans accept only the literals `true` / `false`.
Paper trading is the only supported mode; any attempt to enable live trading is refused.
"""

import ipaddress
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from shadow_trading_suite.core.errors import ConfigError, SafetyViolationError
from shadow_trading_suite.core.instrument import Venue

PAPER_TRADING_ENV = "PAPER_TRADING"
LIVE_TRADING_ENV = "LIVE_TRADING_ENABLED"
ENV_PREFIX = "STS_"

_ENV_FIELDS: dict[str, str] = {
    "STS_JURISDICTION": "jurisdiction",
    "STS_VENUES": "venues",
    "STS_LOG_LEVEL": "log_level",
    "STS_DATA_DIR": "data_dir",
    "STS_API_HOST": "api_host",
    "STS_API_PORT": "api_port",
}

_CREDENTIAL_VENUE = re.compile(r"COINBASE|CRYPTO_?COM|CDP")
_CREDENTIAL_KIND = re.compile(r"KEY|SECRET|PASSPHRASE|TOKEN")
# PERMANENT: no wallet destination, payout target or key material is ever configurable.
# A bare "SEED" is not matched on purpose (PYTHONHASHSEED, test seeds).
_WALLET_CONFIG = re.compile(
    r"WALLET|PAYOUT|DESTINATION_ADDRESS|SEED_PHRASE|RECOVERY_PHRASE|PRIVATE_KEY|PUBLIC_KEY"
    r"|XPRV|KEYSTORE|BIP39|SEED_WORDS|MNEMONIC|XPUB"
    r"|PROFIT_SWEEP|PROFIT_SKIM"  # no automatic profit movement of any kind
)


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    paper_trading: bool = True
    live_trading_enabled: bool = False
    jurisdiction: Literal["DE"] = "DE"
    venues: tuple[Venue, ...] = Field(default=(Venue.COINBASE, Venue.CRYPTOCOM), min_length=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    data_dir: Path = Path("data")
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1024, le=65535)

    @field_validator("api_host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        try:
            is_loopback = ipaddress.ip_address(v).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ValueError(
                f"api_host must be a loopback IP address (127.0.0.1 or ::1), got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _paper_only(self) -> Self:
        if not self.paper_trading or self.live_trading_enabled:
            raise ValueError("only paper trading is supported; live trading cannot be enabled")
        return self


def enforce_paper_only(settings: Settings) -> None:
    """Runtime re-check of the paper-only invariant (guards against validation bypass)."""
    if settings.paper_trading is not True or settings.live_trading_enabled is not False:
        raise SafetyViolationError("settings are not paper-only; refusing to run")


def _check_safety_flags(environ: Mapping[str, str]) -> None:
    for name, raw in environ.items():
        key = name.upper()
        value = raw.strip().lower()
        if key == PAPER_TRADING_ENV and value != "true":
            raise SafetyViolationError(f"{PAPER_TRADING_ENV} must be 'true' (got {raw!r})")
        if _WALLET_CONFIG.search(key):
            raise SafetyViolationError(
                f"{name}: wallet destinations, payouts, profit sweeps and key material are not "
                "permitted (moving funds is manual, by the account holder, outside this software); "
                "hardware-based wallets are permanently outside this system's trust boundary"
            )
        if "LIVE_TRADING" in key and value != "false":
            raise SafetyViolationError(
                f"{name} must be 'false'; live trading is not supported "
                f"(hint: {LIVE_TRADING_ENV} must be false)"
            )


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Build Settings from an environment mapping. Raises on anything unsafe or ambiguous."""
    _check_safety_flags(environ)

    values: dict[str, Any] = {}
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        field = _ENV_FIELDS.get(name)
        if field is None:
            raise ConfigError(f"unknown configuration variable {name}")
        if field == "venues":
            try:
                values[field] = tuple(Venue(v.strip()) for v in raw.split(",") if v.strip())
            except ValueError as exc:
                raise ConfigError(f"invalid {name}: {exc}") from exc
        else:
            values[field] = raw.strip()

    try:
        settings = Settings.model_validate(values)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    enforce_paper_only(settings)
    return settings


def credential_env_names(environ: Mapping[str, str]) -> list[str]:
    """Names (never values) of variables that look like exchange credentials.

    Credentials are never required or read; callers log these names as a warning.
    """
    return sorted(
        name
        for name in environ
        if _CREDENTIAL_VENUE.search(name.upper()) and _CREDENTIAL_KIND.search(name.upper())
    )
