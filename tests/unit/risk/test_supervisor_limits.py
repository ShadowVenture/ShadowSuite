"""Each portfolio limit of the pure `review_portfolio` in ISOLATION .

The integration tests in `test_supervisor.py` accept any of several cap failures, so an audit
mutation that disabled the per-underlying, per-venue, total gross, margin-in-use or stress cap
(or the snapshot-version check) survived the whole suite. Here every limit is the ONLY binding
one, at its boundary (the limits are inclusive: equal passes, one cent more fails), and the
aggregation scope of each cap is pinned (which existing exposures count).
"""

from decimal import Decimal

import pytest

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.instruments.simulation import SimulatedLeverage
from shadow_trading_suite.risk.config import PortfolioConfig
from shadow_trading_suite.risk.interfaces import ExposureRequest, ExposureTerms, StressTerms
from shadow_trading_suite.risk.supervisor import (
    Exposure,
    PortfolioEquity,
    PortfolioState,
    review_portfolio,
)
from tests.support.portfolio import sample_portfolio_config

D = Decimal
EQUITY = D(10000)
CB_BTC = InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD")
CC_BTC = InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTC_USD")
CC_ETH = InstrumentId(venue=Venue.CRYPTOCOM, symbol="ETH_USD")
VENUE = {CB_BTC: Venue.COINBASE, CC_BTC: Venue.CRYPTOCOM, CC_ETH: Venue.CRYPTOCOM}
UNDERLYING = {CB_BTC: "BTC", CC_BTC: "BTC", CC_ETH: "ETH"}


def stress(notional_eur: Decimal) -> StressTerms:
    """Spot, entry = mark, EUR-quoted: the gap loss at shock s is s x notional (fee 0)."""
    return StressTerms(
        side=Side.BUY,
        entry=D(100),
        mark=D(100),
        quantity=notional_eur / 100,
        contract_size=D(1),
        leverage=D(1),
        mmr=None,
        fee_rate=D(0),
        funding_cost=D(0),
        eur_per_quote=D(1),
    )


def request(iid: InstrumentId, notional: Decimal, margin: Decimal | None = None) -> ExposureRequest:
    return ExposureRequest(
        request_id="r-new",
        bot_id="bot-01",
        instrument_id=iid,
        side=Side.BUY,
        quantity=notional / 100,
        reference_price=D(100),
        simulated_leverage=SimulatedLeverage(
            value=D(1), venue_published_max=None, beyond_venue_published=None
        ),
        snapshot_version=7,
        terms=ExposureTerms(
            underlying=UNDERLYING[iid],
            venue=VENUE[iid],
            notional_eur=notional,
            margin_eur=notional if margin is None else margin,
            margin_quote=notional if margin is None else margin,
            margin_currency="EUR",
            stress=stress(notional),
        ),
    )


def held(iid: InstrumentId, notional: Decimal, margin: Decimal = D(0), n: int = 0) -> Exposure:
    return Exposure(
        request_id=f"r-{n}",
        bot_id="bot-02",
        instrument_id=iid,
        underlying=UNDERLYING[iid],
        venue=VENUE[iid],
        side=Side.BUY,
        notional_eur=notional,
        margin_eur=margin,
        stress=stress(notional),
    )


def config(**limits: Decimal) -> PortfolioConfig:
    """Default config with every cap relaxed except the overrides (only they can bind)."""
    cfg = sample_portfolio_config()
    relaxed = {
        "max_gross_per_underlying": D(100),
        "max_gross_per_venue": D(100),
        "max_gross_total": D(100),
        "max_margin_in_use": D(1),
        "max_instrument_concentration": D(1),
        "max_stress_loss": D(1),
    }
    lim = cfg.limits.model_copy(update={**relaxed, **limits})
    return cfg.model_copy(update={"limits": lim})


def failed(
    req: ExposureRequest, exposures: tuple[Exposure, ...], cfg: PortfolioConfig, version: int = 7
) -> set[str]:
    state = PortfolioState(
        equity=PortfolioEquity(equity_eur=EQUITY, start_of_day_eur=EQUITY, peak_eur=EQUITY),
        emergency=False,
        exposures=exposures,
    )
    decision, checks, trigger = review_portfolio(req, state, version, cfg)
    assert trigger is None
    names = {c.name for c in checks if not c.passed}
    assert decision.approved == (not names)
    return names


def test_the_relaxed_baseline_passes_every_check() -> None:
    assert failed(request(CB_BTC, D(1000)), (), config()) == set()


def test_snapshot_version_mismatch_is_refused() -> None:
    assert failed(request(CB_BTC, D(1000)), (), config(), version=8) == {"snapshot"}


class TestGrossCaps:
    # existing: 3000 BTC on Crypto.com + 2000 ETH on Crypto.com; request: 1000 BTC on Coinbase
    EXISTING = (held(CC_BTC, D(3000), n=1), held(CC_ETH, D(2000), n=2))

    @pytest.mark.parametrize(
        ("limit", "boundary"),
        [
            ("max_gross_per_underlying", D("0.40")),  # BTC on BOTH venues: 3000 + 1000
            ("max_gross_per_venue", D("0.10")),  # Coinbase only: 1000
            ("max_gross_total", D("0.60")),  # everything: 3000 + 2000 + 1000
        ],
    )
    def test_each_gross_cap_binds_alone_at_its_boundary(
        self, limit: str, boundary: Decimal
    ) -> None:
        name = {
            "max_gross_per_underlying": "underlying",
            "max_gross_per_venue": "venue",
            "max_gross_total": "total",
        }[limit]
        req = request(CB_BTC, D(1000))
        assert failed(req, self.EXISTING, config(**{limit: boundary})) == set()
        below = boundary - D("0.000001")
        assert failed(req, self.EXISTING, config(**{limit: below})) == {name}

    def test_the_underlying_cap_ignores_other_underlyings(self) -> None:
        other = (held(CC_ETH, D(9000), n=3),)
        cfg = config(max_gross_per_underlying=D("0.10"))
        assert failed(request(CB_BTC, D(1000)), other, cfg) == set()

    def test_the_venue_cap_ignores_other_venues(self) -> None:
        other = (held(CC_BTC, D(9000), n=3),)
        assert failed(request(CB_BTC, D(1000)), other, config(max_gross_per_venue=D("0.10"))) == (
            set()
        )


class TestMarginInUse:
    def test_margin_in_use_sums_every_bot_and_binds_at_its_boundary(self) -> None:
        existing = (held(CC_BTC, D(3000), margin=D(1500), n=1),)
        req = request(CB_BTC, D(1000), margin=D(500))
        assert failed(req, existing, config(max_margin_in_use=D("0.20"))) == set()
        assert failed(req, existing, config(max_margin_in_use=D("0.199"))) == {"margin"}


class TestStress:
    def test_stress_uses_combined_gross_loss_and_binds_at_its_boundary(self) -> None:
        # binding shock 5 %: (3000 + 1000) x 0.05 = 200 EUR of 10000 equity = 2 %
        existing = (held(CC_BTC, D(3000), n=1),)
        req = request(CB_BTC, D(1000))
        assert failed(req, existing, config(max_stress_loss=D("0.02"))) == set()
        assert failed(req, existing, config(max_stress_loss=D("0.0199"))) == {"stress"}


class TestConcentration:
    def test_concentration_applies_only_once_gross_reaches_equity(self) -> None:
        cfg = config(max_instrument_concentration=D("0.40"))
        # gross 9000 < equity: a single-instrument book is allowed (documented policy)
        assert failed(request(CB_BTC, D(9000)), (), cfg) == set()
        # gross 10000 >= equity and CB_BTC is 100 % of it
        assert failed(request(CB_BTC, D(10000)), (), cfg) == {"concentration"}
        # 4000 of 10000 = 40 % passes; 4001 of 10001 does not
        assert failed(request(CB_BTC, D(4000)), (held(CC_BTC, D(6000), n=1),), cfg) == set()
        assert failed(request(CB_BTC, D(4001)), (held(CC_BTC, D(6000), n=1),), cfg) == {
            "concentration"
        }
