"""Paper profit reservation: not supported by the suite, for every bot.

There is no automatic reserve and no default values; profits stay in each bot's trading equity.
Historical reserve operations of older ledgers still replay (tests/unit/accounting).
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from shadow_trading_suite.risk.config import PortfolioConfig
from tests.support.portfolio import sample_portfolio_config


def test_the_default_portfolio_has_no_reservation() -> None:
    assert sample_portfolio_config().profit_reservation is None


def test_enabling_the_reservation_is_refused() -> None:
    data = sample_portfolio_config().model_dump()
    data["profit_reservation"] = {
        "enabled": True,
        "threshold_eur": Decimal(3000),
        "reserve_eur": Decimal(700),
        "max_per_day": 1,
        "timezone": "UTC",
        "settlement": "completed_day_at_0000_utc",
    }
    with pytest.raises(ValidationError, match="not supported"):
        PortfolioConfig.model_validate(data)
