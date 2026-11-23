"""PaperExecutionEngine: the single writer of simulated execution (M6-B, E1).

Every command (accept, request_cancel, market event, tick, gap, restart) is ONE ledger unit of
work holding the M5 decisions, the journal operations, the supervisor's exposure changes and
one hash-chained `exec.cmd` audit record with the full recorded input, the ledger facts used,
the planned actions, the outputs and the execution-state changes plus digest. In-memory state
changes only after COMMIT. Commands are serialised under the store's writer lock; any failure
rolls the whole command back, is audited and triggers the emergency shutdown (fail closed).

Paper only: the engine refuses to start unless settings are paper-only; nothing here contacts a
venue or represents a real order.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from shadow_trading_suite.accounting.journal import bot_account
from shadow_trading_suite.accounting.trading import (
    CapitalError,
    FillSpec,
    InstrumentTerms,
    LiquidationExecution,
    LiquidationTrigger,
    OrderSpec,
    OrderState,
    TradingLedger,
)
from shadow_trading_suite.config.settings import Settings, enforce_paper_only
from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import Instrument, InstrumentId, InstrumentKind, Venue
from shadow_trading_suite.core.market import FundingRate
from shadow_trading_suite.core.orders import Liquidity, OrderType, Side
from shadow_trading_suite.core.time import Clock
from shadow_trading_suite.execution.assumptions import (
    DEFAULT_PROFILE,
    AssumptionSet,
    default_assumption_set,
)
from shadow_trading_suite.execution.model import (
    STATE_VERSION,
    ExecState,
    OrderTerms,
    instrument_key,
    state_effects,
    stream_key,
    ts_key,
)
from shadow_trading_suite.execution.planner import (
    AcceptInput,
    Ctx,
    Facts,
    GapInput,
    MarketInput,
    Plan,
    PositionFact,
    TickInput,
    plan_accept,
    plan_cancel,
    plan_gap,
    plan_market,
    plan_refusal,
    plan_restart,
    plan_tick,
)
from shadow_trading_suite.execution.recovery import reconcile, restore_state
from shadow_trading_suite.execution.support import (
    ExecutionNotSupportedError,
    require_live_feeds,
    support_for,
)
from shadow_trading_suite.risk.config import PortfolioConfig
from shadow_trading_suite.risk.engine import (
    RULES_VERSION,
    BotState,
    RiskVerdict,
    TradeProposal,
    reduces_exposure,
)
from shadow_trading_suite.risk.market_inputs import market_inputs_from_snapshot
from shadow_trading_suite.risk.pipeline import RiskPipeline
from shadow_trading_suite.risk.supervisor import PortfolioSupervisor
from shadow_trading_suite.storage.ledger import UnitOfWork
from shadow_trading_suite.streaming.events import StreamKind
from shadow_trading_suite.streaming.state import MarketSnapshot, StreamKey

AUDIT_KIND = "exec.cmd"
FAILURE_KIND = "exec.command_failed"


class ExecutionHaltedError(Exception):
    """The engine is read-only (recovery mismatch or a failed command): no commands accepted."""


@dataclass(frozen=True)
class AcceptResult:
    request_id: str
    order_id: str | None
    accepted: bool
    reason: str


def instrument_terms(inst: Instrument) -> InstrumentTerms:
    return InstrumentTerms(
        instrument_id=inst.id,
        kind=inst.kind,
        base=inst.base,
        quote=inst.quote,
        settle=inst.settle,
        contract_size=inst.contract_size,
    )


def fill_spec(action: Mapping[str, Any]) -> FillSpec:
    return FillSpec(
        fill_id=action["fill_id"],
        order_id=action["order_id"],
        price=Decimal(action["price"]),
        quantity=Decimal(action["quantity"]),
        fee_rate=Decimal(action["fee_rate"]),
        liquidity=Liquidity(action["liquidity"]),
        ts=action["ts"],
    )


class PaperExecutionEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        cfg: PortfolioConfig,
        ledger: TradingLedger,
        supervisor: PortfolioSupervisor,
        pipeline: RiskPipeline,
        instruments: Mapping[InstrumentId, Instrument],
        clock: Clock,
        assumptions: AssumptionSet | None = None,
        profiles: Mapping[str, str] | None = None,
    ) -> None:
        enforce_paper_only(settings)  # paper-only start gate (§8.1)
        self._cfg = cfg
        self._ledger = ledger
        self._store = ledger.store
        self._supervisor = supervisor
        self._pipeline = pipeline
        self._instruments = dict(instruments)
        self._clock = clock
        self.assumptions = assumptions or default_assumption_set(cfg)
        bot_profiles = {b.bot_id: DEFAULT_PROFILE for b in cfg.bots} | dict(profiles or {})
        for pid in bot_profiles.values():
            self.assumptions.profile(pid)  # unknown profile -> KeyError (fail closed)
        self.ctx = Ctx(
            assumptions=self.assumptions,
            lots={instrument_key(i.id): i.lot_size for i in self._instruments.values()},
            profiles=bot_profiles,
        )
        self._lock = self._store.lock
        with self._lock:
            self._state = restore_state(self._store)  # tampered chain -> raises: refuse start
            self.recovery_issues = tuple(reconcile(self._state, ledger, supervisor))
        self.read_only = bool(self.recovery_issues)
        if self.read_only:
            supervisor.trigger_emergency(
                "execution recovery reconciliation failed: " + "; ".join(self.recovery_issues)
            )
        self._started = False

    # -- queries ------------------------------------------------------------------------------

    @property
    def state(self) -> ExecState:
        with self._lock:
            return self._state

    def _max_age(self, inst: Instrument) -> timedelta:
        lim = self._cfg.limits
        seconds = (
            lim.max_data_age_spot_s
            if inst.kind is InstrumentKind.SPOT
            else lim.max_data_age_derivatives_s
        )
        return timedelta(seconds=seconds)

    def facts(self) -> Facts:
        positions = {}
        for key, pos in self._ledger.positions().items():
            iid, venue = pos.instrument.instrument_id, pos.instrument.instrument_id.venue
            ccy = pos.instrument.settle_currency
            margin = self._ledger.balance(bot_account(pos.bot_id, venue, "margin", iid.symbol), ccy)
            payable = -self._ledger.balance(
                bot_account(pos.bot_id, venue, "payable", f"funding:{iid.symbol}"), ccy
            )
            positions[key] = PositionFact(
                bot_id=pos.bot_id,
                instrument_id=iid,
                base=pos.instrument.base,
                side=pos.side,
                quantity=pos.quantity,
                avg_entry=pos.avg_entry,
                contract_size=pos.instrument.contract_size,
                margin=margin,
                payable=payable,
                pending_reduce=pos.pending_reduce,
            )
        cash = {}
        for fact in positions.values():
            venue = fact.instrument_id.venue
            owner = f"{fact.bot_id}@{venue.value}"
            ccy = self._cfg.bot(fact.bot_id).cash_currency
            cash[owner] = self._ledger.balance(bot_account(fact.bot_id, venue, "cash"), ccy)
        return Facts(positions=positions, cash=cash)

    # -- command machinery --------------------------------------------------------------------

    def _command(
        self,
        kind: str,
        recorded: dict[str, Any],
        planner: Callable[[ExecState, Facts], Plan],
        before_actions: Callable[[UnitOfWork], None] | None = None,
    ) -> Plan:
        with self._lock:
            if self.read_only:
                raise ExecutionHaltedError("execution engine is read-only")
            try:
                with self._store.unit_of_work() as uow:
                    if before_actions is not None:
                        before_actions(uow)
                    facts = self.facts()
                    plan = planner(self._state, facts)
                    self._execute(plan.actions)
                    seq = self._state.seq + 1
                    follow_ups = self._follow_ups(seq)
                    new_state = plan.state.model_copy(update={"seq": seq})
                    uow.add_audit(
                        AUDIT_KIND,
                        str(seq),
                        {
                            "v": STATE_VERSION,
                            "seq": seq,
                            "kind": kind,
                            "input": recorded,
                            "facts": facts.model_dump(mode="json"),
                            "actions": plan.actions,
                            "outputs": plan.outputs,
                            "follow_ups": follow_ups,
                            "effects": state_effects(self._state, new_state),
                            "digest": new_state.digest(),
                            "assumptions": self.assumptions.sha256,
                            # the planner context in force (venue lot sizes and latency
                            # profiles can change between runs; replay must not depend on that)
                            "ctx": {
                                "lots": {k: str(v) for k, v in sorted(self.ctx.lots.items())},
                                "profiles": dict(sorted(self.ctx.profiles.items())),
                            },
                        },
                    )
                    uow.on_commit(lambda: setattr(self, "_state", new_state))
            except Exception as exc:
                self._fail(kind, exc)
                raise
            return plan

    def _fail(self, kind: str, exc: Exception) -> None:
        """Audit the failure and stop accepting work (fail closed). Best effort only: if the
        store itself is unusable, the next start refuses or reconciles."""
        self.read_only = True
        try:
            with self._store.unit_of_work() as uow:
                uow.add_audit(FAILURE_KIND, kind, {"error": f"{type(exc).__name__}: {exc}"[:500]})
            self._supervisor.trigger_emergency(f"execution command {kind} failed")
        except Exception:  # noqa: S110 - the original exception is re-raised by the caller
            pass

    def _execute(self, actions: list[dict[str, Any]]) -> None:
        ledger = self._ledger
        for a in actions:
            op = a["op"]
            if op == "fill":
                ledger.book_fill(fill_spec(a))
            elif op == "close":
                ledger.close_order(a["order_id"], OrderState(a["final"]), _dt(a["ts"]))
            elif op == "cancel_request":
                ledger.request_cancel(a["order_id"], _dt(a["ts"]))
            elif op == "liq_begin":
                ledger.begin_liquidation(
                    a["op_id"],
                    a["bot_id"],
                    InstrumentId.model_validate(a["instrument_id"]),
                    LiquidationTrigger.model_validate(a["trigger"]),
                )
            elif op == "liq_fill":
                ledger.book_liquidation_fill(
                    a["op_id"],
                    a["bot_id"],
                    InstrumentId.model_validate(a["instrument_id"]),
                    LiquidationExecution(
                        price=Decimal(a["price"]), quantity=Decimal(a["quantity"])
                    ),
                    Decimal(a["fee_rate"]),
                )
            elif op == "liq_finish":
                ledger.finish_liquidation(
                    a["op_id"], a["bot_id"], InstrumentId.model_validate(a["instrument_id"])
                )
            elif op == "funding":
                ledger.book_funding(
                    a["op_id"],
                    a["bot_id"],
                    InstrumentId.model_validate(a["instrument_id"]),
                    Decimal(a["rate"]),
                    Decimal(a["mark"]),
                    _dt(a["ts"]),
                )
            elif op == "emergency":
                self._supervisor.trigger_emergency(a["reason"])
            else:
                raise ValueError(f"unknown execution action {op!r}")

    def _follow_ups(self, seq: int) -> list[dict[str, Any]]:
        """Deterministic follow-ups derived from the ledger after the planned actions:
        (1) settle outstanding liabilities from the SAME bot's free cash (B5);
        (2) keep supervisor exposures equal to open orders + open positions (B1, C7)."""
        done: list[dict[str, Any]] = []
        ledger = self._ledger
        owners: set[tuple[str, Venue]] = set()
        for (account, _), bal in ledger.balances().items():
            owner, _, rest = account.partition(":")
            if rest.startswith("payable:") and bal < 0:
                bot, venue = owner.split("@")
                owners.add((bot, Venue(venue)))
        for bot, venue in sorted(owners):
            ccy = self._cfg.bot(bot).cash_currency
            if ledger.balance(bot_account(bot, venue, "cash"), ccy) > 0:
                op_id = f"payable:{bot}@{venue.value}:{ccy}:{seq}"
                paid = ledger.settle_payable(op_id, bot, venue, ccy)
                done.append({"op": "settle_payable", "op_id": op_id, "paid": paid})
        orders = ledger.orders()
        for rid, exposure in sorted(self._supervisor.exposures().items()):
            oid = self._supervisor.backing(rid)
            record = orders.get(oid) if oid else None
            if record is None or record.state not in (
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.EXPIRED,
            ):
                continue
            spec = record.spec
            reducing = spec.reduce_only or (spec.instrument.is_spot and spec.side is Side.SELL)
            iid = spec.instrument.instrument_id
            flat = (
                ledger.position(spec.bot_id, iid) is None
                and not ledger.lots(spec.bot_id, iid)
                and ledger.liquidation(spec.bot_id, iid) is None
            )
            if reducing or record.filled_qty == 0 or flat:
                self._supervisor.release(rid)
                done.append({"op": "exposure_released", "request_id": rid})
            elif record.filled_qty < exposure.stress.quantity:
                fraction = record.filled_qty / exposure.stress.quantity
                self._supervisor.resize_exposure(rid, fraction)
                done.append({"op": "exposure_resized", "request_id": rid, "fraction": fraction})
        return done

    # -- commands -----------------------------------------------------------------------------

    def start(self) -> Plan:
        """Restart counts as a gap (E4): cancel every order recovered from the ledger."""
        if self._started:
            raise RuntimeError("engine already started")
        now = self._clock.now()
        plan = self._command("restart", {"now": ts_key(now)}, lambda s, f: plan_restart(s, now))
        self._started = True
        return plan

    def _require_started(self) -> None:
        if not self._started:
            raise ExecutionHaltedError("call start() first (restart gap policy)")

    def accept(
        self,
        proposal: TradeProposal,
        terms: OrderTerms,
        bot_state: BotState,
        snapshot: MarketSnapshot,
        *,
        sigma: Decimal,
        eur_per_quote: Decimal,
        funding: FundingRate | None = None,
    ) -> AcceptResult:
        """Atomic accept: M5 engine + supervisor decision, the B1 hold -> order reservation
        transfer, `ACCEPTED` and the audit record in one transaction. Idempotent per request."""
        self._require_started()
        rid = proposal.request_id
        oid = f"{proposal.bot_id}:{rid}"
        with self._lock:
            if rid in self._state.decided:
                done = self._state.decided[rid] or None
                return AcceptResult(rid, done, done is not None, "duplicate request")
        inst = self._instruments.get(proposal.instrument_id)
        recorded: dict[str, Any] = {
            "proposal": proposal.model_dump(mode="json"),
            "terms": terms.model_dump(mode="json"),
            "snapshot_version": snapshot.version,
        }
        outcome: dict[str, Any] = {}

        def decide(uow: UnitOfWork) -> None:
            outcome["result"] = self._decide(
                proposal, terms, inst, bot_state, snapshot, sigma, eur_per_quote, funding, oid
            )

        def planner(state: ExecState, facts: Facts) -> Plan:
            result = outcome["result"]
            if isinstance(result, AcceptInput):
                recorded["accept"] = result.model_dump(mode="json")
                return plan_accept(state, result, self.ctx)
            recorded["refusal"] = result
            return plan_refusal(state, rid, result)

        self._command("accept", recorded, planner, before_actions=decide)
        result = outcome["result"]
        if isinstance(result, AcceptInput):
            return AcceptResult(rid, oid, True, "accepted")
        return AcceptResult(rid, None, False, str(result))

    def _decide(
        self,
        p: TradeProposal,
        terms: OrderTerms,
        inst: Instrument | None,
        bot_state: BotState,
        snapshot: MarketSnapshot,
        sigma: Decimal,
        eur_per_quote: Decimal,
        funding: FundingRate | None,
        oid: str,
    ) -> AcceptInput | str:
        """Runs inside the command's unit of work. Returns the accept input or a refusal."""
        if inst is None:
            return "unknown instrument"
        try:
            support = support_for(inst)
        except ExecutionNotSupportedError as exc:
            return str(exc)
        venue = inst.id.venue.value
        if venue in self._state.blocked:
            return f"venue blocked (E4): {self._state.blocked[venue]}"
        if self._state.funding_unsettled.get(instrument_key(inst.id)):
            return "funding could not be settled for this instrument (fail closed)"
        if self._ledger.liquidation(p.bot_id, inst.id) is not None:
            return "position is being liquidated"
        if inst.kind is InstrumentKind.SPOT and (p.reduce_only or p.leverage != 1):
            return "spot orders are unleveraged and cannot be reduce-only"
        if not reduces_exposure(p, inst, RULES_VERSION) and self._ledger.payables(
            p.bot_id, inst.id.venue
        ):
            return (
                "outstanding funding liability: only exposure-reducing orders until it is "
                "settled from this bot's own cash"
            )
        if terms.limit_price is not None and terms.limit_price % inst.tick_size != 0:
            return f"limit price {terms.limit_price} is not a multiple of tick {inst.tick_size}"
        max_age = self._max_age(inst)
        try:  # before M5: a refused feed must never leave a supervisor hold behind
            require_live_feeds(support, snapshot, max_age, timedelta(hours=2))
        except DataQualityError as exc:
            return f"market_data: {exc}"
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
            # record the fail-closed M5 rejection exactly as the pipeline does
            self._pipeline.submit_from_snapshot(
                p,
                inst,
                bot_state,
                snapshot,
                sigma=sigma,
                eur_per_quote=eur_per_quote,
                funding=funding,
            )
            return f"market_data: {exc}"
        result = self._pipeline.submit(p, inst, bot_state, m, snapshot)
        if result.bot.verdict is RiskVerdict.REJECT:
            return "risk engine: " + "; ".join(result.bot.reasons)
        if result.supervisor is None or not result.supervisor.approved:
            reasons = result.supervisor.reasons if result.supervisor else ("no review",)
            return "supervisor: " + "; ".join(reasons)
        qty = result.bot.approved_quantity
        assert qty is not None  # noqa: S101 - approved decisions carry a quantity
        a = self.assumptions
        band = max(
            a.decimal("price_band.slippage_multiple") * m.slippage, a.decimal("price_band.min")
        )
        mid = (m.bid + m.ask) / 2
        if terms.order_type is OrderType.MARKET:
            reserve = mid * (1 + band)
        elif p.side is Side.BUY or inst.kind is InstrumentKind.SPOT:
            reserve = terms.limit_price or mid
        else:  # a derivative sell may fill above its limit (taker part, within the band)
            reserve = max(terms.limit_price or mid, mid * (1 + band))
        book_ts = snapshot.require(StreamKey(inst.id, StreamKind.BOOK), max_age).exchange_ts
        t = max(self._state.market_time.get(venue, book_ts), book_ts)
        spec = OrderSpec(
            order_id=oid,
            bot_id=p.bot_id,
            instrument=instrument_terms(inst),
            side=p.side,
            quantity=qty,
            reserve_price=reserve,
            reserve_fee_rate=self.ctx.fee(
                spot=inst.kind is InstrumentKind.SPOT, liquidity=Liquidity.TAKER
            ),
            leverage=p.leverage,
            reduce_only=p.reduce_only,
            created_at=t,
        )
        try:
            self._supervisor.back_with_order(  # B1: hold -> reservation
                p.request_id,
                oid,
                capital_free=inst.kind is InstrumentKind.SPOT and p.side is Side.SELL,
            )
            self._ledger.open_order(spec)
        except CapitalError as exc:
            self._supervisor.abandon(p.request_id)
            return f"reservation: {exc}"
        return AcceptInput(
            order_id=oid,
            request_id=p.request_id,
            bot_id=p.bot_id,
            instrument_id=inst.id,
            side=p.side,
            quantity=qty,
            terms=terms,
            reduce_only=p.reduce_only,
            leverage=p.leverage,
            contract_size=inst.contract_size,
            is_spot=inst.kind is InstrumentKind.SPOT,
            decision_mid=mid,
            band=band,
            market_time=t,
        )

    def request_cancel(self, order_id: str) -> Plan:
        self._require_started()
        return self._command(
            "cancel", {"order_id": order_id}, lambda s, f: plan_cancel(s, order_id, self.ctx)
        )

    def on_market(self, snapshot: MarketSnapshot, key: StreamKey) -> Plan:
        """Process the CURRENT value of one stream from one atomic snapshot."""
        self._require_started()
        event = snapshot.entries.get(key)
        if event is None:
            raise DataQualityError(f"{key.instrument_id} {key.kind.value}: no data")
        inst = self._instruments[key.instrument_id]
        age = timedelta(hours=2) if key.kind is StreamKind.FUNDING else self._max_age(inst)
        valid, reason = True, ""
        try:
            snapshot.require(key, age)
        except DataQualityError as exc:
            valid, reason = False, str(exc)
        inp = MarketInput(event=event, valid=valid, reason=reason)
        return self._command(
            "market",
            inp.model_dump(mode="json"),
            lambda s, f: plan_market(s, f, inp, self.ctx),
        )

    def tick(self, snapshot: MarketSnapshot) -> Plan:
        """Wall-clock checks: E4 gap cancellation, missing marks, emergency shutdown."""
        self._require_started()
        status = {
            stream_key(k.instrument_id, k.kind.value): v.value for k, v in snapshot.status.items()
        }
        inp = TickInput(
            now=self._clock.now(),
            status=status,
            emergency=self._supervisor.emergency_shutdown,
        )
        return self._command(
            "tick", inp.model_dump(mode="json"), lambda s, f: plan_tick(s, f, inp, self.ctx)
        )

    def on_gap(self, venue: Venue, reason: str) -> Plan:
        """GapNotice / ConnectionGap / sequence gap: immediate E4 for the venue."""
        self._require_started()
        inp = GapInput(venue=venue, reason=reason, now=self._clock.now())
        return self._command("gap", inp.model_dump(mode="json"), lambda s, f: plan_gap(s, inp))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
