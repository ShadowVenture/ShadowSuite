"""Risk pipeline: validated snapshot -> per-bot engine -> portfolio supervisor -> audit.

Every decision (including fail-closed rejections caused by stale or missing data) is written
to the append-only audit trail together with the exact inputs, so `replay_audit` can
recompute each decision and prove determinism. Nothing here executes or contacts a venue.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import Instrument, InstrumentKind, VenueDetails
from shadow_trading_suite.core.market import FundingRate
from shadow_trading_suite.exchanges.coinbase.details import CoinbaseDetails
from shadow_trading_suite.exchanges.cryptocom.details import CryptoComDetails
from shadow_trading_suite.risk.config import PortfolioConfig
from shadow_trading_suite.risk.engine import (
    RULES_VERSION,
    BotRiskDecision,
    BotState,
    CheckResult,
    MarketInputs,
    RiskVerdict,
    TradeProposal,
    _fee,
    _funding_cost,
    evaluate_bot,
    reduces_exposure,
)
from shadow_trading_suite.risk.interfaces import (
    ExposureRequest,
    ExposureTerms,
    StressTerms,
    SupervisorDecision,
)
from shadow_trading_suite.risk.market_inputs import market_inputs_from_snapshot
from shadow_trading_suite.risk.supervisor import (
    PortfolioState,
    PortfolioSupervisor,
    review_portfolio,
)
from shadow_trading_suite.storage.ledger import AuditRecord, LedgerStore
from shadow_trading_suite.streaming.state import MarketSnapshot

_DETAILS: dict[str, type[VenueDetails]] = {
    "CoinbaseDetails": CoinbaseDetails,
    "CryptoComDetails": CryptoComDetails,
}


def instrument_to_json(inst: Instrument) -> dict[str, Any]:
    data = inst.model_dump(mode="json", exclude={"venue_details"})
    details = inst.venue_details
    data["venue_details"] = details.model_dump(mode="json") if details else None
    data["venue_details_type"] = type(details).__name__ if details else None
    return data


def instrument_from_json(data: dict[str, Any]) -> Instrument:
    data = dict(data)
    kind, details = data.pop("venue_details_type"), data.pop("venue_details")
    inst = Instrument.model_validate(data)
    if kind is not None:
        inst = inst.model_copy(update={"venue_details": _DETAILS[kind].model_validate(details)})
    return inst


@dataclass(frozen=True)
class PipelineResult:
    bot: BotRiskDecision
    supervisor: SupervisorDecision | None
    supervisor_checks: tuple[CheckResult, ...] = ()


def _req[T](value: T | None, name: str) -> T:
    if value is None:
        raise ValueError(f"approved decision lacks {name}")
    return value


def _exposure_request(
    p: TradeProposal,
    inst: Instrument,
    d: BotRiskDecision,
    m: MarketInputs,
    cfg: PortfolioConfig,
    rules: int = RULES_VERSION,
) -> ExposureRequest:
    stress = StressTerms(
        side=p.side,
        entry=_req(d.entry_price, "entry_price"),
        mark=m.mark,
        quantity=_req(d.approved_quantity, "approved_quantity"),
        contract_size=inst.contract_size,
        leverage=p.leverage,
        mmr=d.maintenance_margin.value if d.maintenance_margin else None,
        fee_rate=_fee(inst, cfg),
        funding_cost=_funding_cost(p, m),
        eur_per_quote=m.eur_per_quote,
    )
    return ExposureRequest(
        request_id=p.request_id,
        bot_id=p.bot_id,
        instrument_id=p.instrument_id,
        side=p.side,
        quantity=_req(d.approved_quantity, "approved_quantity"),
        reference_price=_req(d.entry_price, "entry_price"),
        simulated_leverage=_req(d.leverage, "leverage"),
        snapshot_version=m.snapshot_version,
        reduce_only=p.reduce_only,
        reduces_exposure=reduces_exposure(p, inst, rules) and not p.reduce_only,
        terms=ExposureTerms(
            underlying=inst.base,
            venue=inst.id.venue,
            notional_eur=_req(d.notional_eur, "notional_eur"),
            margin_eur=_req(d.margin_required_eur, "margin_required_eur"),
            margin_quote=_req(d.margin_required_quote, "margin_required_quote"),
            margin_currency=inst.settle or inst.quote,
            stress=stress,
        ),
    )


# Public name for offline consumers (e.g. a backtester): the exact request the live pipeline
# sends to the supervisor, so backtests gate through the unchanged M5 code.
exposure_request = _exposure_request


class RiskPipeline:
    def __init__(
        self, cfg: PortfolioConfig, supervisor: PortfolioSupervisor, audit: LedgerStore
    ) -> None:
        self._cfg = cfg
        self._supervisor = supervisor
        self._audit = audit

    def submit_from_snapshot(
        self,
        p: TradeProposal,
        inst: Instrument,
        state: BotState,
        snapshot: MarketSnapshot,
        *,
        sigma: Decimal,
        eur_per_quote: Decimal,
        funding: FundingRate | None = None,
    ) -> PipelineResult:
        lim = self._cfg.limits
        max_age = timedelta(
            seconds=lim.max_data_age_spot_s
            if inst.kind is InstrumentKind.SPOT
            else lim.max_data_age_derivatives_s
        )
        try:
            m = market_inputs_from_snapshot(
                snapshot,
                inst,
                quantity=p.quantity,
                sigma=sigma,
                eur_per_quote=eur_per_quote,
                max_age=max_age,
                min_slippage=self._cfg.fees.min_slippage.value,
                funding=funding,
            )
        except DataQualityError as exc:
            check = CheckResult(name="market_data", passed=False, detail=str(exc))
            decision = BotRiskDecision(
                request_id=p.request_id,
                bot_id=p.bot_id,
                verdict=RiskVerdict.REJECT,
                approved_quantity=None,
                checks=(check,),
                reasons=(f"market_data: {exc}",),
                snapshot_version=snapshot.version,
            )
            self._record_bot(p, inst, state, None, decision)
            return PipelineResult(decision, None)
        return self.submit(p, inst, state, m, snapshot)

    def submit(
        self,
        p: TradeProposal,
        inst: Instrument,
        state: BotState,
        m: MarketInputs,
        snapshot: MarketSnapshot,
    ) -> PipelineResult:
        decision = evaluate_bot(p, inst, state, m, self._cfg)
        self._record_bot(p, inst, state, m, decision)
        if decision.verdict is RiskVerdict.REJECT:
            return PipelineResult(decision, None)
        request = _exposure_request(p, inst, decision, m, self._cfg)
        sup, checks = self._supervisor.review_with_checks(request, snapshot)
        return PipelineResult(decision, sup, checks)

    def _record_bot(
        self,
        p: TradeProposal,
        inst: Instrument,
        state: BotState,
        m: MarketInputs | None,
        decision: BotRiskDecision,
    ) -> None:
        self._audit.append_audit(
            "bot_decision",
            p.request_id,
            {
                "proposal": p.model_dump(mode="json"),
                "instrument": instrument_to_json(inst),
                "state": state.model_dump(mode="json"),
                "market": m.model_dump(mode="json") if m else None,
                "decision": decision.model_dump(mode="json"),
                "rules": RULES_VERSION,
            },
        )


def replay_audit(records: list[AuditRecord], cfg: PortfolioConfig) -> list[str]:
    """Recompute every recorded decision from its recorded inputs; return mismatches."""
    mismatches: list[str] = []
    for rec in records:
        payload = rec.payload
        if rec.kind == "bot_decision":
            recorded = BotRiskDecision.model_validate(payload["decision"])
            if payload["market"] is None:
                ok = (
                    recorded.verdict is RiskVerdict.REJECT
                    and recorded.checks[0].name == "market_data"
                )
            else:
                recomputed = evaluate_bot(
                    TradeProposal.model_validate(payload["proposal"]),
                    instrument_from_json(payload["instrument"]),
                    BotState.model_validate(payload["state"]),
                    MarketInputs.model_validate(payload["market"]),
                    cfg,
                    rules=int(payload.get("rules", 1)),  # records before v2 carry no version
                )
                ok = recomputed == recorded
        elif rec.kind == "supervisor_decision":
            recorded_sup = SupervisorDecision.model_validate(payload["decision"])
            pure, _, _ = review_portfolio(
                ExposureRequest.model_validate(payload["request"]),
                PortfolioState.model_validate(payload["portfolio"]),
                int(payload["snapshot_version"]),
                cfg,
            )
            if payload["capital_error"]:
                ok = pure.approved and not recorded_sup.approved  # approved, then capital refused
            else:
                ok = pure == recorded_sup
        else:
            continue
        if not ok:
            mismatches.append(f"seq {rec.seq} {rec.kind} {rec.subject}")
    return mismatches
