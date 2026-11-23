"""Deterministic replay of execution commands from their audited inputs (§5.5).

For every `exec.cmd` record the plan is recomputed from the state rebuilt from all previous
records, the recorded input and the recorded ledger facts, then compared with the recorded
actions, outputs, state changes and digest. Every simulated fill must exist in the ledger as an
operation whose digest equals the digest of the recomputed fill payload. Journal postings are
covered by `TradingLedger.verify_replay()` and M5 decisions by `risk.pipeline.replay_audit`.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from shadow_trading_suite.execution.engine import fill_spec
from shadow_trading_suite.execution.model import ExecState, apply_effects, state_effects
from shadow_trading_suite.execution.planner import (
    AcceptInput,
    Ctx,
    Facts,
    GapInput,
    MarketInput,
    Plan,
    TickInput,
    plan_accept,
    plan_cancel,
    plan_gap,
    plan_market,
    plan_refusal,
    plan_restart,
    plan_tick,
)
from shadow_trading_suite.execution.recovery import AUDIT_KIND
from shadow_trading_suite.storage.ledger import LedgerStore, canonical_json


def _plan(kind: str, state: ExecState, facts: Facts, inp: dict[str, Any], ctx: Ctx) -> Plan:
    if kind == "accept":
        if "accept" in inp:
            return plan_accept(state, AcceptInput.model_validate(inp["accept"]), ctx)
        rid = inp["proposal"]["request_id"]
        return plan_refusal(state, rid, inp["refusal"])
    if kind == "cancel":
        return plan_cancel(state, inp["order_id"], ctx)
    if kind == "market":
        return plan_market(state, facts, MarketInput.model_validate(inp), ctx)
    if kind == "tick":
        return plan_tick(state, facts, TickInput.model_validate(inp), ctx)
    if kind == "gap":
        return plan_gap(state, GapInput.model_validate(inp))
    if kind == "restart":
        return plan_restart(state, datetime.fromisoformat(inp["now"]))
    raise ValueError(f"unknown command kind {kind!r}")


def _json(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _recorded_ctx(payload: dict[str, Any], ctx: Ctx) -> Ctx:
    """The planner context recorded with the command (M7); records written before it was
    recorded replay with the current context, as before."""
    recorded = payload.get("ctx")
    if recorded is None:
        return ctx
    return Ctx(
        assumptions=ctx.assumptions,
        lots={k: Decimal(v) for k, v in recorded["lots"].items()},
        profiles=dict(recorded["profiles"]),
    )


def replay_execution(store: LedgerStore, ctx: Ctx) -> list[str]:
    mismatches: list[str] = []
    ops = {op.op_id: op for op in store.operations()}
    state = ExecState()
    for rec in store.audit_records():
        if rec.kind != AUDIT_KIND:
            continue
        p = rec.payload
        seq = p["seq"]
        if p["assumptions"] != ctx.assumptions.sha256:
            mismatches.append(f"seq {seq}: recorded under a different assumption set")
        facts = Facts.model_validate(p["facts"])
        try:
            plan = _plan(p["kind"], state, facts, p["input"], _recorded_ctx(p, ctx))
        except Exception as exc:
            mismatches.append(f"seq {seq}: replay raised {type(exc).__name__}: {exc}")
            state = apply_effects(state, p["effects"])
            continue
        after = plan.state.model_copy(update={"seq": seq})
        if _json(plan.actions) != p["actions"]:
            mismatches.append(f"seq {seq}: actions differ")
        if _json(plan.outputs) != p["outputs"]:
            mismatches.append(f"seq {seq}: outputs differ")
        if _json(state_effects(state, after)) != p["effects"] or after.digest() != p["digest"]:
            mismatches.append(f"seq {seq}: execution state differs")
        for action in plan.actions:
            if action["op"] != "fill":
                continue
            spec = fill_spec(_json(action))
            op = ops.get(f"fill:{spec.fill_id}")
            expected = LedgerStore.digest_for("fill", _json({"fill": spec.model_dump()}))
            if op is None or op.digest != expected:
                mismatches.append(f"seq {seq}: fill {spec.fill_id} not booked as planned")
        state = apply_effects(state, p["effects"])
    return mismatches
