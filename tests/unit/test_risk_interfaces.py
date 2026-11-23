"""Contracts for the future (M5) portfolio risk supervisor. No implementation yet."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.instruments.simulation import SimulatedLeverage
from shadow_trading_suite.risk.interfaces import (
    ExposureRequest,
    PortfolioRiskSupervisor,
    SupervisorDecision,
)
from shadow_trading_suite.streaming.state import MarketSnapshot

PERP = InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTCUSD-PERP")


def request(**kw: Any) -> ExposureRequest:
    fields: dict[str, Any] = {
        "request_id": "req-1",
        "bot_id": "cdc-01",
        "instrument_id": PERP,
        "side": Side.BUY,
        "quantity": "0.5",
        "reference_price": "82000",
        "simulated_leverage": SimulatedLeverage(
            value=Decimal(100), venue_published_max=Decimal(100), beyond_venue_published=False
        ),
        "snapshot_version": 42,
        "terms": {
            "underlying": "BTC",
            "venue": "cryptocom",
            "notional_eur": "35260",
            "margin_eur": "352.6",
            "margin_quote": "410",
            "margin_currency": "USD",
            "stress": {
                "side": "buy",
                "entry": "82000",
                "mark": "82000",
                "quantity": "0.5",
                "contract_size": "1",
                "leverage": "100",
                "mmr": "0.005",
                "fee_rate": "0.0006",
                "funding_cost": "0",
                "eur_per_quote": "0.86",
            },
        },
    }
    fields.update(kw)
    return ExposureRequest.model_validate(fields)


def test_request_binds_to_a_snapshot_version_and_hypothetical_leverage() -> None:
    r = request()
    assert r.snapshot_version == 42
    assert r.simulated_leverage.hypothetical is True
    assert r.notional == Decimal(41000)


@pytest.mark.parametrize("field", ["live", "execute", "api_key", "eligibility_override"])
def test_request_has_no_live_or_override_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        request(**{field: True})


def test_rejection_must_explain_itself() -> None:
    with pytest.raises(ValidationError, match="reason"):
        SupervisorDecision(approved=False, reasons=(), snapshot_version=1)
    assert SupervisorDecision(approved=True, reasons=(), snapshot_version=1).approved


class _DenyAll:
    def review(self, request: ExposureRequest, snapshot: MarketSnapshot) -> SupervisorDecision:
        return SupervisorDecision(
            approved=False, reasons=("not implemented until M5",), snapshot_version=snapshot.version
        )


def test_protocol_shape() -> None:
    supervisor: PortfolioRiskSupervisor = _DenyAll()  # static conformance (mypy)
    snap = MarketSnapshot(
        version=7, as_of=datetime(2026, 10, 19, tzinfo=UTC), entries={}, status={}
    )
    decision = supervisor.review(request(snapshot_version=7), snap)
    assert not decision.approved
    assert decision.snapshot_version == 7
