"""The suite configuration file (TOML). Explicit and fail-closed; floats and unknown keys refused.

```toml
[suite]
data_dir = "data"                       # relative to this file; the paper ledger lives here
portfolio = "portfolio.example.toml"    # relative to this file (risk limits, fictitious capital)

# No [[plugins]] table: the suite starts with no bot (the default).
[[plugins]]
factory = "my_bot.plugin:create"        # OR: entry_point = "my-bot"
[plugins.config]
some_setting = "value"
```
"""

import tomllib
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from shadow_trading_suite.core.decimal import Dec
from shadow_trading_suite.core.model import DomainModel
from shadow_trading_suite.risk.config import PortfolioConfig, load_portfolio_config


class SuiteSection(DomainModel):
    data_dir: Path = Path("data")
    portfolio: Path = Path("portfolio.example.toml")


class PluginSpec(DomainModel):
    factory: str | None = None
    entry_point: str | None = None
    config: dict[str, str | int | bool | Dec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.factory is None) == (self.entry_point is None):
            raise ValueError("a plugin needs exactly one of `factory` or `entry_point`")
        return self


class SuiteConfig(DomainModel):
    suite: SuiteSection = SuiteSection()
    plugins: tuple[PluginSpec, ...] = ()


class LoadedConfig(DomainModel):
    path: Path
    config: SuiteConfig
    data_dir: Path
    portfolio: PortfolioConfig


def load_suite_config(path: Path) -> LoadedConfig:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    cfg = SuiteConfig.model_validate(raw)
    base = path.resolve().parent
    data_dir = (base / cfg.suite.data_dir).resolve()
    portfolio = load_portfolio_config((base / cfg.suite.portfolio).resolve())
    return LoadedConfig(path=path.resolve(), config=cfg, data_dir=data_dir, portfolio=portfolio)
