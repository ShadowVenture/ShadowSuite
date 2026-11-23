"""Execution uncertainty report (proposal §R requirement 1, §3.1).

Deterministic simulation correctness (replay, accounting invariants) is reported SEPARATELY
from real-world accuracy, which this simulation cannot establish: queue position, hidden
liquidity, latency and other participants' reactions are unknown. Simulated maker fills come
from the conservative queue model (ASSUMPTION) and are never reported as verified executions.
"""

from collections import Counter
from decimal import Decimal
from typing import Any

from shadow_trading_suite.core.model import DomainModel
from shadow_trading_suite.execution.assumptions import AssumptionSet, Source
from shadow_trading_suite.execution.recovery import AUDIT_KIND
from shadow_trading_suite.storage.ledger import AuditRecord

BP = Decimal(10_000)
DISCLAIMER = (
    "Simulated paper fills. Deterministic and replayable, but NOT evidence of real-world "
    "execution quality: queue position, hidden/iceberg liquidity, latency and market impact "
    "are modelled by ASSUMPTIONS."
)


class FillMetrics(DomainModel):
    fill_id: str
    basis: str
    shortfall_bp: Decimal  # vs decision mid, positive = worse for the bot
    drift_bp: Decimal | None  # decision mid -> best price at the fill book
    slippage_bp: Decimal | None  # fill price vs best price at the fill book


class ExecutionReport(DomainModel):
    assumption_set: str
    assumption_sha256: str
    profiles: dict[str, str]
    fills: tuple[FillMetrics, ...]
    orders: int
    filled_orders: int
    fill_ratio: Decimal | None
    band_rejections: int
    depth_exhaustions: int
    queue_fills: int
    taker_fills: int
    cancels: int
    assumption_dependent_share: Decimal | None
    verified_parameters: tuple[str, ...]
    assumption_parameters: tuple[str, ...]
    derived_parameters: tuple[str, ...]
    disclaimer: str = DISCLAIMER


def _metrics(fill: dict[str, Any]) -> FillMetrics:
    sign = 1 if fill["side"] == "buy" else -1
    price, mid = Decimal(fill["price"]), Decimal(fill["decision_mid"])
    best = Decimal(fill["best_price"]) if fill.get("best_price") is not None else None
    return FillMetrics(
        fill_id=fill["fill_id"],
        basis=fill["basis"],
        shortfall_bp=sign * (price - mid) / mid * BP,
        drift_bp=sign * (best - mid) / mid * BP if best is not None else None,
        slippage_bp=sign * (price - best) / best * BP if best is not None else None,
    )


def execution_report(
    records: list[AuditRecord], assumptions: AssumptionSet, profiles: dict[str, str]
) -> ExecutionReport:
    fills: list[FillMetrics] = []
    counts: Counter[str] = Counter()
    ordered: dict[str, Decimal] = {}
    filled: dict[str, Decimal] = {}
    for rec in records:
        if rec.kind != AUDIT_KIND:
            continue
        for out in rec.payload["outputs"]:
            event = out["event"]
            counts[event] += 1
            if event == "accepted":
                ordered[out["order_id"]] = Decimal(0)
            elif event == "fill":
                fills.append(_metrics(out))
                filled[out["order_id"]] = filled.get(out["order_id"], Decimal(0)) + Decimal(
                    out["quantity"]
                )
            elif event == "order_closed":
                ordered[out["order_id"]] = Decimal(out["quantity"])
                counts["closed_" + out["reason"].split(":")[0]] += 1
    taker = sum(1 for f in fills if f.basis == "book_walk")
    queue = len(fills) - taker
    total_qty = sum(ordered.values(), Decimal(0))
    return ExecutionReport(
        assumption_set=f"{assumptions.set_id}@{assumptions.version}",
        assumption_sha256=assumptions.sha256,
        profiles=profiles,
        fills=tuple(fills),
        orders=counts["accepted"],
        filled_orders=counts["order_filled"],
        fill_ratio=(sum(filled.values(), Decimal(0)) / total_qty) if total_qty else None,
        band_rejections=counts["price_band"],
        depth_exhaustions=counts["depth"] + counts["liq_depth_exhausted"],
        queue_fills=queue,
        taker_fills=taker,
        cancels=counts["closed_cancel_request"],
        # every fill depends on latency/fee ASSUMPTIONS unless the ideal profile is used;
        # queue-model fills additionally depend on the queue ASSUMPTION
        assumption_dependent_share=Decimal(1) if fills else None,
        verified_parameters=tuple(assumptions.by_source(Source.VERIFIED)),
        assumption_parameters=tuple(assumptions.by_source(Source.ASSUMPTION)),
        derived_parameters=tuple(assumptions.by_source(Source.DERIVED)),
    )
