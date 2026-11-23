"""Central Portfolio Risk Supervisor (paper only).

`review_portfolio` is a PURE function of the request and a recorded PortfolioState, so every
decision can be replayed. `PortfolioSupervisor` serialises reviews under one lock (no races
between bots), is idempotent per request_id, atomically commits the approved margin with the
CapitalManager, tracks approved exposure, triggers / requires manual reset of the emergency
shutdown, and writes every decision to the append-only, hash-chained audit trail.

Stress loss uses COMBINED GROSS exposure: every position (any bot, any venue, any underlying)
is shocked adversely at once (correlation 1, no offsetting), in GAP mode, so losses can exceed
posted isolated margin (bad debt) — the most conservative reading of the approved rule.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from pydantic import Field

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.model import DomainModel, NonNegativeDec, PositiveDec
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.risk.capital import CapitalManager, InsufficientCapitalError
from shadow_trading_suite.risk.config import PortfolioConfig
from shadow_trading_suite.risk.engine import CheckResult
from shadow_trading_suite.risk.interfaces import ExposureRequest, StressTerms, SupervisorDecision
from shadow_trading_suite.risk.margin import (
    MarginModelUnavailableError,
    PositionTerms,
    shock_outcome,
)
from shadow_trading_suite.storage.ledger import LedgerStore, UnitOfWork
from shadow_trading_suite.streaming.state import MarketSnapshot


class PortfolioEquity(DomainModel):
    equity_eur: Decimal  # all bots, EUR, excluding any historical (retired) profit reserve
    start_of_day_eur: PositiveDec
    peak_eur: PositiveDec


class Exposure(DomainModel):
    request_id: str
    bot_id: str
    instrument_id: InstrumentId
    underlying: str
    venue: Venue
    side: Side
    notional_eur: PositiveDec
    margin_eur: NonNegativeDec
    stress: StressTerms


class PortfolioState(DomainModel):
    equity: PortfolioEquity
    emergency: bool
    exposures: tuple[Exposure, ...] = Field(default=())


def stress_loss_eur(t: StressTerms, shock: Decimal) -> Decimal:
    if t.mmr is None:  # spot: unleveraged, no liquidation
        size = t.quantity * t.contract_size
        adverse = t.mark * (1 - shock) if t.side is Side.BUY else t.mark * (1 + shock)
        move = t.entry - adverse if t.side is Side.BUY else adverse - t.entry
        return (move * size + adverse * size * t.fee_rate) * t.eur_per_quote
    terms = PositionTerms(
        t.side, t.entry, t.quantity, t.contract_size, t.leverage, t.mmr, t.fee_rate, t.funding_cost
    )
    return shock_outcome(terms, t.mark, shock, Decimal(0), gap=True).loss * t.eur_per_quote


def review_portfolio(
    request: ExposureRequest, portfolio: PortfolioState, snapshot_version: int, cfg: PortfolioConfig
) -> tuple[SupervisorDecision, tuple[CheckResult, ...], str | None]:
    """Pure decision. Returns (decision, checks, emergency_trigger_reason)."""
    lim = cfg.limits
    checks: list[CheckResult] = []
    trigger: str | None = None

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append(CheckResult(name=name, passed=ok, detail=detail))

    eq = portfolio.equity
    daily = (eq.start_of_day_eur - eq.equity_eur) / eq.start_of_day_eur
    dd = (eq.peak_eur - eq.equity_eur) / eq.peak_eur
    if not portfolio.emergency and (
        daily >= lim.emergency_daily_loss or dd >= lim.emergency_drawdown
    ):
        trigger = f"portfolio daily loss {daily:.4f} / drawdown {dd:.4f} breached emergency limits"
    halted = portfolio.emergency or trigger is not None
    reducing = request.reduce_only or request.reduces_exposure
    check(
        "emergency",
        reducing or not halted,
        "emergency shutdown active: reduce-only" if halted else f"daily {daily:.4f}, dd {dd:.4f}",
    )
    check(
        "snapshot",
        request.snapshot_version == snapshot_version,
        f"request v{request.snapshot_version} vs snapshot v{snapshot_version}",
    )
    check("equity", eq.equity_eur > 0, f"portfolio equity {eq.equity_eur}")
    terms = request.terms
    if not reducing and all(c.passed for c in checks):
        opposing = [
            e
            for e in portfolio.exposures
            if e.underlying == terms.underlying and e.side != request.side
        ]
        check(
            "conflict",
            not (lim.reject_opposing_positions and opposing),
            f"opposing exposure on {terms.underlying} by {sorted({e.bot_id for e in opposing})}"
            if opposing
            else "no opposing exposure",
        )
        equity = eq.equity_eur
        existing = portfolio.exposures
        underlying = (
            sum((e.notional_eur for e in existing if e.underlying == terms.underlying), Decimal(0))
            + terms.notional_eur
        )
        venue = (
            sum((e.notional_eur for e in existing if e.venue is terms.venue), Decimal(0))
            + terms.notional_eur
        )
        total = sum((e.notional_eur for e in existing), Decimal(0)) + terms.notional_eur
        margin = sum((e.margin_eur for e in existing), Decimal(0)) + terms.margin_eur
        instrument = (
            sum(
                (e.notional_eur for e in existing if e.instrument_id == request.instrument_id),
                Decimal(0),
            )
            + terms.notional_eur
        )
        check(
            "underlying",
            underlying <= lim.max_gross_per_underlying * equity,
            f"{terms.underlying} gross {underlying:.2f} "
            f"vs {lim.max_gross_per_underlying}x {equity}",
        )
        check(
            "venue",
            venue <= lim.max_gross_per_venue * equity,
            f"{terms.venue.value} gross {venue:.2f} vs {lim.max_gross_per_venue}x",
        )
        check(
            "total",
            total <= lim.max_gross_total * equity,
            f"gross {total:.2f} vs {lim.max_gross_total}x",
        )
        check(
            "margin",
            margin <= lim.max_margin_in_use * equity,
            f"margin {margin:.2f} vs {lim.max_margin_in_use}",
        )
        concentrated = total >= equity and instrument / total > lim.max_instrument_concentration
        check(
            "concentration",
            not concentrated,
            f"{request.instrument_id} share {instrument / total:.3f} of gross {total:.2f}",
        )
        try:
            losses = {
                s: sum((stress_loss_eur(e.stress, s) for e in existing), Decimal(0))
                + stress_loss_eur(terms.stress, s)
                for s in lim.stress_shocks
            }
            binding = losses[lim.stress_shock]
            check(
                "stress",
                binding <= lim.max_stress_loss * equity,
                f"gross adverse gap loss at {lim.stress_shock}: {binding:.2f} vs "
                f"{lim.max_stress_loss * equity:.2f}; all: "
                + ", ".join(f"{s}:{v:.2f}" for s, v in losses.items()),
            )
        except MarginModelUnavailableError as exc:
            check("stress", False, f"cannot stress-test: {exc}")
    reasons = tuple(f"{c.name}: {c.detail}" for c in checks if not c.passed)
    decision = SupervisorDecision(
        approved=not reasons, reasons=reasons, snapshot_version=snapshot_version
    )
    return decision, tuple(checks), trigger


class PortfolioSupervisor:
    """Serialises all reviews under the ledger store's single writer lock.

    Durable: every state change (decision + capital commit, release, emergency trigger/reset)
    is ONE unit of work with its audit record, and in-memory state changes only after COMMIT.
    On construction the state is rebuilt from the audit trail and reconciled with the capital
    holds; any mismatch triggers the emergency shutdown (fail closed).
    """

    def __init__(
        self,
        cfg: PortfolioConfig,
        capital: CapitalManager,
        audit: LedgerStore,
        equity: Callable[[], PortfolioEquity],
    ) -> None:
        if capital.store is not audit:
            raise ValueError("capital and audit must use the same ledger store (atomicity)")
        self._cfg = cfg
        self._capital = capital
        self._audit = audit
        self._equity = equity
        self._lock = audit.lock
        self._exposures: dict[str, Exposure] = {}
        self._backing: dict[str, str] = {}  # request_id -> paper order now holding its capital
        self._decisions: dict[str, tuple[SupervisorDecision, tuple[CheckResult, ...]]] = {}
        self.emergency_shutdown = False
        self.recovery_issues: tuple[str, ...] = ()
        with self._lock:
            self._restore()

    # -- recovery ------------------------------------------------------------------------------

    def _restore(self) -> None:
        for rec in self._audit.audit_records():
            p = rec.payload
            if rec.kind == "supervisor_decision":
                req = ExposureRequest.model_validate(p["request"])
                decision = SupervisorDecision.model_validate(p["decision"])
                checks = tuple(CheckResult.model_validate(c) for c in p["checks"])
                self._decisions[req.request_id] = (decision, checks)
                if decision.approved:
                    self._exposures[req.request_id] = _exposure(req)
            elif rec.kind == "exposure_released" and rec.subject is not None:
                self._exposures.pop(rec.subject, None)
                self._backing.pop(rec.subject, None)
            elif rec.kind == "exposure_order_backed" and rec.subject is not None:
                self._backing[rec.subject] = str(p["order_id"])
            elif rec.kind == "exposure_resized" and rec.subject in self._exposures:
                self._exposures[rec.subject] = _scaled(
                    self._exposures[rec.subject], Decimal(p["fraction"])
                )
            elif rec.kind == "emergency_shutdown":
                self.emergency_shutdown = True
            elif rec.kind == "emergency_reset":
                self.emergency_shutdown = False
        issues = self._reconcile()
        self.recovery_issues = tuple(issues)
        if issues:
            with self._audit.unit_of_work() as uow:
                uow.add_audit("recovery_reconciliation_failed", None, {"issues": issues})
                self._emergency(uow, "recovery reconciliation failed: " + "; ".join(issues))

    def _reconcile(self) -> list[str]:
        """Supervisor exposures must equal the active capital holds it created, or the paper
        orders those holds were transferred to (B1)."""
        issues = []
        orders = self._capital.orders()
        for rid, exposure in sorted(self._exposures.items()):
            hold = self._capital.hold(rid)
            backing = self._backing.get(rid)
            if backing is not None:
                order = orders.get(backing)
                if order is None:
                    issues.append(f"exposure {rid} is backed by unknown order {backing}")
                elif order.spec.bot_id != exposure.bot_id:
                    issues.append(f"exposure {rid} order belongs to {order.spec.bot_id}")
                if hold is not None and not hold.released:
                    issues.append(f"exposure {rid} holds capital twice (hold and order)")
            elif hold is None or hold.released:
                issues.append(f"exposure {rid} has no active capital hold")
            elif hold.bot_id != exposure.bot_id:
                issues.append(f"exposure {rid} hold belongs to {hold.bot_id}")
        for ref, hold in sorted(self._capital.holds().items()):
            approved = ref in self._decisions and self._decisions[ref][0].approved
            if approved and not hold.released and ref not in self._exposures:
                issues.append(f"active hold {ref} has no supervisor exposure")
        return issues

    # -- state ---------------------------------------------------------------------------------

    def exposures(self) -> dict[str, Exposure]:
        with self._lock:
            return dict(self._exposures)

    def backing(self, request_id: str) -> str | None:
        with self._lock:
            return self._backing.get(request_id)

    def state(self) -> PortfolioState:
        return PortfolioState(
            equity=self._equity(),
            emergency=self.emergency_shutdown,
            exposures=tuple(self._exposures.values()),
        )

    def _emergency(self, uow: UnitOfWork, reason: str) -> None:
        if not self.emergency_shutdown:
            uow.add_audit("emergency_shutdown", None, {"reason": reason})
            uow.on_commit(lambda: setattr(self, "emergency_shutdown", True))

    def trigger_emergency(self, reason: str) -> None:
        with self._lock, self._audit.unit_of_work() as uow:
            self._emergency(uow, reason)

    def reset_emergency(self, note: str) -> None:
        """Manual operator action only."""
        with self._lock, self._audit.unit_of_work() as uow:
            uow.add_audit("emergency_reset", None, {"note": note})
            uow.on_commit(lambda: setattr(self, "emergency_shutdown", False))

    def review(self, request: ExposureRequest, snapshot: MarketSnapshot) -> SupervisorDecision:
        return self.review_with_checks(request, snapshot)[0]

    def review_with_checks(
        self, request: ExposureRequest, snapshot: MarketSnapshot
    ) -> tuple[SupervisorDecision, tuple[CheckResult, ...]]:
        rid = request.request_id
        with self._lock:
            if rid in self._decisions:
                return self._decisions[rid]
            with self._audit.unit_of_work() as uow:
                portfolio = self.state()
                decision, checks, trigger = review_portfolio(
                    request, portfolio, snapshot.version, self._cfg
                )
                if trigger:
                    self._emergency(uow, trigger)
                capital_error = None
                if decision.approved and not request.capital_free:
                    bot = self._cfg.bot(request.bot_id)
                    try:
                        if request.terms.margin_currency != bot.cash_currency:
                            raise InsufficientCapitalError(
                                f"margin in {request.terms.margin_currency}, "
                                f"bot cash is {bot.cash_currency}"
                            )
                        self._capital.commit(
                            rid,
                            bot.bot_id,
                            bot.venue,
                            bot.cash_currency,
                            request.terms.margin_quote,
                        )
                    except InsufficientCapitalError as exc:
                        capital_error = str(exc)
                        checks = (
                            *checks,
                            CheckResult(name="capital", passed=False, detail=capital_error),
                        )
                        decision = SupervisorDecision(
                            approved=False,
                            reasons=(f"capital: {capital_error}",),
                            snapshot_version=decision.snapshot_version,
                        )
                payload: dict[str, Any] = {
                    "request": request.model_dump(mode="json"),
                    "portfolio": portfolio.model_dump(mode="json"),
                    "snapshot_version": snapshot.version,
                    "decision": decision.model_dump(mode="json"),
                    "checks": [c.model_dump(mode="json") for c in checks],
                    "capital_error": capital_error,
                }
                uow.add_audit("supervisor_decision", rid, payload)
                result = (decision, checks)
                uow.on_commit(lambda: self._record(request, result))
            return result

    def _record(
        self, request: ExposureRequest, result: tuple[SupervisorDecision, tuple[CheckResult, ...]]
    ) -> None:
        self._decisions[request.request_id] = result
        if result[0].approved:
            self._exposures[request.request_id] = _exposure(request)

    def release(self, request_id: str) -> None:
        """Drop an approved but unfilled exposure and release its committed margin."""
        with self._lock:
            hold = self._capital.hold(request_id)
            pending = hold is not None and not hold.released  # e.g. reviewed in this unit
            if request_id not in self._exposures and not pending:
                return
            with self._audit.unit_of_work() as uow:
                if hold is not None:  # none for a capital-free request
                    self._capital.release(request_id)
                uow.add_audit("exposure_released", request_id, {})
                uow.on_commit(lambda: self._forget(request_id))

    def _forget(self, request_id: str) -> None:
        self._exposures.pop(request_id, None)
        self._backing.pop(request_id, None)

    def abandon(self, request_id: str) -> None:
        """Drop an exposure approved in the CURRENT unit of work whose order could not be
        opened (e.g. its reservation failed): no capital stays held, nothing stays exposed."""
        with self._lock, self._audit.unit_of_work() as uow:
            if self._capital.hold(request_id) is not None:  # none for a capital-free request
                self._capital.release(request_id)  # idempotent when already transferred
            uow.add_audit("exposure_released", request_id, {"reason": "order not opened"})
            uow.on_commit(lambda: self._forget(request_id))

    def back_with_order(
        self, request_id: str, order_id: str, *, capital_free: bool = False
    ) -> None:
        """B1: move an approved exposure's capital hold into its paper order's reservation.

        Must run inside the caller's unit of work, together with `open_order`, so the capital
        is never reserved twice and never free in between (one DuckDB transaction).
        `capital_free` (risk rules v2 spot SELL): the review took no hold - the order reserves
        the sold inventory itself - so there is nothing to transfer."""
        with self._lock:
            if request_id in self._backing:
                return  # idempotent
            hold = self._capital.hold(request_id)  # includes this unit's uncommitted review
            active = hold is not None and not hold.released
            if not active and not capital_free:
                raise ValueError(f"no approved exposure with an active hold {request_id!r}")
            if active and capital_free:
                raise ValueError(f"capital-free exposure {request_id!r} holds capital")
            with self._audit.unit_of_work() as uow:
                if active:
                    self._capital.release(request_id)
                payload: dict[str, Any] = {"order_id": order_id}
                if capital_free:
                    payload["capital_free"] = True
                uow.add_audit("exposure_order_backed", request_id, payload)
                uow.on_commit(lambda: self._backing.__setitem__(request_id, order_id))

    def resize_exposure(self, request_id: str, fraction: Decimal) -> None:
        """Shrink an exposure to `fraction` (0 < fraction < 1) of its size, e.g. to the filled
        part of an order whose remainder was cancelled."""
        if not 0 < fraction < 1:
            raise ValueError("resize fraction must be in (0, 1)")
        with self._lock:
            if request_id not in self._exposures:
                return
            with self._audit.unit_of_work() as uow:
                uow.add_audit("exposure_resized", request_id, {"fraction": fraction})
                uow.on_commit(
                    lambda: self._exposures.__setitem__(
                        request_id, _scaled(self._exposures[request_id], fraction)
                    )
                )


def _scaled(e: Exposure, fraction: Decimal) -> Exposure:
    return e.model_copy(
        update={
            "notional_eur": e.notional_eur * fraction,
            "margin_eur": e.margin_eur * fraction,
            "stress": e.stress.model_copy(update={"quantity": e.stress.quantity * fraction}),
        }
    )


def _exposure(request: ExposureRequest) -> Exposure:
    return Exposure(
        request_id=request.request_id,
        bot_id=request.bot_id,
        instrument_id=request.instrument_id,
        underlying=request.terms.underlying,
        venue=request.terms.venue,
        side=request.side,
        notional_eur=request.terms.notional_eur,
        margin_eur=request.terms.margin_eur,
        stress=request.terms.stress,
    )
