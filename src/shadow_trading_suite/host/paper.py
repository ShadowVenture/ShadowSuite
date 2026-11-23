"""The paper host: runs external bot plugins behind the central risk and execution controls.

One host = one single-writer process over one paper ledger. The host owns the market state, the
ledger, the risk engine / portfolio supervisor and the paper execution engine; plugins see only
a read-only `MarketView` of their own instruments, holdings and orders.

Order path (the only one): plugin `OrderRequest` -> host checks -> ledger-derived risk inputs
(economic equity, durable day-start / peak equity, live triangulated EUR rate, minute-close
volatility) -> `PaperExecutionEngine.accept` (risk engine + portfolio supervisor + paper order
reservation, one atomic unit of work). Anything missing or stale refuses the request (fail
closed). Fills are simulated by the engine on later market events.

With no plugin installed the host starts, keeps the ledger and reports an empty state.

Scope of this preview (0.1): plugins trade SPOT instruments only; derivatives are supported by
the engine but not yet admitted for plugins (docs/roadmap.md). There is no real order path.
"""

import contextlib
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from shadow_trading_suite import __version__
from shadow_trading_suite.accounting.reporting import bot_risk_state, value_bot
from shadow_trading_suite.config.settings import Settings, enforce_paper_only
from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import Instrument, InstrumentId, InstrumentKind
from shadow_trading_suite.core.market import MarkPrice, OrderBookSnapshot, Trade
from shadow_trading_suite.core.orders import OrderType, Side
from shadow_trading_suite.core.time import Clock
from shadow_trading_suite.execution.engine import PaperExecutionEngine
from shadow_trading_suite.execution.model import OrderTerms, TimeInForce
from shadow_trading_suite.observability.status import write_status
from shadow_trading_suite.paper.equity import PORTFOLIO, EquityMarks, EquitySnapshots
from shadow_trading_suite.paper.fx import FX_SUBSCRIPTIONS, FxPolicy, eur_rates, usd_eur_rate
from shadow_trading_suite.paper.volatility import VolatilityPolicy, VolatilityTracker
from shadow_trading_suite.plugins.contract import (
    Decision,
    Holding,
    MarketView,
    OpenOrder,
    OrderRequest,
    OrderStatus,
    OrderUpdate,
    PluginConfig,
    Quote,
    Signal,
    StopReason,
)
from shadow_trading_suite.plugins.registry import PluginEntry, PluginRegistry, PluginState
from shadow_trading_suite.risk.capital import CapitalManager
from shadow_trading_suite.risk.config import PortfolioConfig, simulation_permission
from shadow_trading_suite.risk.engine import TradeProposal
from shadow_trading_suite.risk.pipeline import RiskPipeline
from shadow_trading_suite.risk.supervisor import PortfolioEquity, PortfolioSupervisor
from shadow_trading_suite.storage.ledger import LedgerStore
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind, Subscription
from shadow_trading_suite.streaming.state import ApplyOutcome, MarketState, StreamKey, StreamStatus

REFUSED = "host.request_refused"
BOOTSTRAP = "host.bootstrap"
PLUGIN_EVENT = "host.plugin"
RESERVED_IDS = frozenset({PORTFOLIO})
TICK_EVERY = timedelta(seconds=1)  # engine wall-clock checks, driven by market events
PLUGIN_STREAMS = frozenset({StreamKind.BOOK, StreamKind.TRADES})  # what wakes a plugin
_LEDGER_STATUS = {
    "accepted": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "cancel_requested": OrderStatus.CANCEL_REQUESTED,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
}


class HostError(Exception):
    """A host operation was refused (the host state is unchanged)."""


def emergency_breach(
    equity: Decimal, marks: EquityMarks, daily_limit: Decimal, drawdown_limit: Decimal
) -> str | None:
    """The supervisor's emergency rule on ledger-derived equity."""
    daily = (marks.start_of_day - equity) / marks.start_of_day
    dd = (marks.peak - equity) / marks.peak
    if daily >= daily_limit or dd >= drawdown_limit:
        return f"portfolio daily loss {daily:.4f} / drawdown {dd:.4f} breached emergency limits"
    return None


class PaperHost:
    def __init__(
        self,
        *,
        portfolio: PortfolioConfig,
        data_dir: Path,
        instruments: Mapping[InstrumentId, Instrument],
        clock: Clock,
        settings: Settings | None = None,
        status_dir: Path | None = None,
        fx_policy: FxPolicy | None = None,
        volatility_policy: VolatilityPolicy | None = None,
    ) -> None:
        self.settings = settings or Settings()
        enforce_paper_only(self.settings)  # paper-only start gate
        self.cfg = portfolio
        self._clock = clock
        self.instruments = dict(instruments)
        self.status_dir = status_dir
        self.fx_policy = fx_policy or FxPolicy()
        self.store = LedgerStore.open(data_dir, clock=clock)
        self.ledger = CapitalManager(self.store, clock, portfolio.fees.conversion_cost.value)
        self._equity_now: PortfolioEquity | None = None
        self.supervisor = PortfolioSupervisor(portfolio, self.ledger, self.store, self._equity)
        self.pipeline = RiskPipeline(portfolio, self.supervisor, self.store)
        self.market = MarketState(clock, {})
        self.engine = PaperExecutionEngine(
            settings=self.settings,
            cfg=portfolio,
            ledger=self.ledger,
            supervisor=self.supervisor,
            pipeline=self.pipeline,
            instruments=self.instruments,
            clock=clock,
        )
        self.volatility = VolatilityTracker(volatility_policy)
        self.snapshots = EquitySnapshots(self.store)
        self.registry = PluginRegistry()
        self.notes: list[str] = []
        self.started = False
        self.stopped_at: datetime | None = None
        self.final_status: dict[str, Any] | None = None
        self._orders: dict[str, deque[datetime]] = {}
        self._seen: dict[str, tuple[str, Decimal]] = {}
        self._last_tick: datetime | None = None

    def now(self) -> datetime:
        """The host's injected clock."""
        return self._clock.now()

    # -- plugins ------------------------------------------------------------------------------

    def add_plugin(self, plugin: object, config: PluginConfig | None = None) -> PluginEntry:
        """Register and configure one plugin. Refused (nothing registered) when the portfolio has
        no risk profile for it, or it names an instrument the host does not simulate for it."""
        entry = self.registry.register(plugin)
        m = entry.manifest
        problem = self._admission_problem(entry)
        if problem is not None:
            self.registry.discard(m.plugin_id)
            raise HostError(f"plugin {m.plugin_id!r} refused: {problem}")
        self._orders[m.plugin_id] = deque()
        self._audit(m.plugin_id, "registered", {"version": m.version})
        try:
            entry.plugin.configure(dict(config or {}))
        except Exception as exc:  # the plugin's own error: FAILED, never raised into the host
            self._fail(entry, "configure", exc, call_stop=False)
            return entry
        entry.state = PluginState.CONFIGURED
        if self.started:
            self._start_plugin(entry)
        return entry

    def _admission_problem(self, entry: PluginEntry) -> str | None:
        m = entry.manifest
        if m.plugin_id in RESERVED_IDS:
            return f"the id {m.plugin_id!r} is reserved"
        try:
            profile = self.cfg.bot(m.plugin_id)
        except KeyError:
            return "the portfolio has no [[bots]] risk profile with this bot_id"
        if profile.venue is not m.venue:
            return f"its risk profile is for venue {profile.venue.value}"
        permission = simulation_permission(profile)
        for iid in m.instruments:
            inst = self.instruments.get(iid)
            if inst is None:
                return f"instrument {iid} is not available in this host"
            if inst.kind is not InstrumentKind.SPOT:
                return f"instrument {iid}: only spot instruments are admitted in this version"
            if inst.kind not in permission.instrument_kinds:
                return f"instrument {iid}: {inst.kind.value} is not simulated for this bot"
        return None

    def stop_plugin(self, plugin_id: str, reason: StopReason = StopReason.REQUESTED) -> None:
        """Stop one plugin: its resting paper orders are cancel-requested, `on_stop` is called
        once, and it never receives market data or submits again. Idempotent."""
        entry = self.registry.get(plugin_id)
        if entry.state in (PluginState.STOPPED, PluginState.FAILED):
            return
        entry.state = PluginState.STOPPED
        self._cancel_open(plugin_id)
        self._audit(plugin_id, "stopped", {"reason": reason.value})
        try:
            entry.plugin.on_stop(reason)
        except Exception as exc:
            entry.error = f"on_stop: {type(exc).__name__}: {exc}"[:300]
            entry.counters["errors"] += 1
        self.export_status()

    def emergency_stop(self, reason: str) -> None:
        """Portfolio emergency: the supervisor refuses every exposure increase (persistent in
        the ledger audit) and every plugin is stopped."""
        self.supervisor.trigger_emergency(reason)
        for entry in list(self.registry):
            self.stop_plugin(entry.manifest.plugin_id, StopReason.SHUTDOWN)

    # -- lifecycle ----------------------------------------------------------------------------

    def subscriptions(self) -> tuple[Subscription, ...]:
        """Streams the host needs: the EUR-rate tickers plus book and trades per plugin
        instrument (none for plugins when none is installed)."""
        subs = list(FX_SUBSCRIPTIONS)
        for entry in self.registry:
            for iid in entry.manifest.instruments:
                subs += [Subscription(iid, kind) for kind in sorted(PLUGIN_STREAMS)]
        return tuple(dict.fromkeys(subs))

    def start(self) -> None:
        if self.started:
            return
        self.engine.start()
        self.started = True
        for entry in list(self.registry):
            if entry.state is PluginState.CONFIGURED:
                self._start_plugin(entry)
        self.export_status()

    def _start_plugin(self, entry: PluginEntry) -> None:
        if self._call(entry, "on_start", self._clock.now()) is _FAILED:
            return
        entry.state = PluginState.RUNNING
        self._audit(entry.manifest.plugin_id, "running", {})

    def shutdown(self) -> None:
        """Stop every plugin, write the final status and close the ledger."""
        if self.stopped_at is not None:
            return
        for entry in list(self.registry):
            self.stop_plugin(entry.manifest.plugin_id, StopReason.SHUTDOWN)
        self.stopped_at = self._clock.now()
        self.final_status = self.status()
        self.export_status()
        self.store.close()

    # -- market data --------------------------------------------------------------------------

    def set_stream_status(self, key: StreamKey, status: StreamStatus) -> None:
        self.market.set_status(key, status)

    def on_event(self, event: StreamEvent) -> None:
        self._require_running()
        outcome = self.market.apply(event)
        if outcome is not ApplyOutcome.ACCEPTED:
            return
        self.volatility.observe(event)
        if event.instrument_id in self.instruments:  # FX tickers only feed the EUR rate
            key = StreamKey(event.instrument_id, event.kind)
            self.engine.on_market(self.market.snapshot(), key)
            self._sync_orders()
        self.bootstrap()
        self._monitor()
        self._maybe_tick()
        for entry in list(self.registry):
            if (
                entry.state is PluginState.RUNNING
                and event.kind in PLUGIN_STREAMS
                and event.instrument_id in entry.manifest.instruments
            ):
                self._run_plugin(entry)

    def tick(self) -> None:
        """Wall-clock checks of the engine: stream gaps block a venue and cancel its resting
        orders (E4); a venue blocked after a restart or gap is released only once every stream
        of it is LIVE again; expiries; missing marks."""
        self._require_running()
        self._last_tick = self._clock.now()
        self.engine.tick(self.market.snapshot())
        self._sync_orders()

    def _maybe_tick(self) -> None:
        now = self._clock.now()
        if self._last_tick is None or now - self._last_tick >= TICK_EVERY:
            self.tick()

    def _require_running(self) -> None:
        if not self.started or self.stopped_at is not None:
            raise HostError("the host is not running")

    # -- capital ------------------------------------------------------------------------------

    def bootstrapped(self) -> bool:
        return all(
            self.store.operation_digest(f"alloc:{b.bot_id}") is not None for b in self.cfg.bots
        )

    def bootstrap(self) -> bool:
        """Seed the fictitious paper capital and the configured allocations once, atomically,
        at the live EUR rate. False while no rate exists (nothing is booked)."""
        if self.bootstrapped():
            return True
        try:
            fx = usd_eur_rate(self.market.snapshot(), self.fx_policy)
        except DataQualityError as exc:
            self._note(f"bootstrap waiting for EUR rate: {exc}")
            return False
        with self.store.unit_of_work() as uow:
            self.ledger.deposit_paper_capital("seed", self.cfg.starting_capital_eur)
            for b in self.cfg.bots:
                self.ledger.allocate(
                    f"alloc:{b.bot_id}",
                    b.bot_id,
                    b.venue,
                    b.cash_currency,
                    b.allocation_eur,
                    fx.eur_per_unit,
                )
            uow.add_audit(BOOTSTRAP, None, {"fx": fx.model_dump(mode="json")})
        return True

    def _equity(self) -> PortfolioEquity:
        if self._equity_now is None:
            raise DataQualityError("portfolio equity is only available for a gated request")
        return self._equity_now

    def _marks(self) -> dict[InstrumentId, Decimal]:
        snapshot = self.market.snapshot()
        age = timedelta(seconds=self.cfg.limits.max_data_age_spot_s)
        out: dict[InstrumentId, Decimal] = {}
        for iid, inst in self.instruments.items():
            try:
                if inst.kind is InstrumentKind.SPOT:
                    book = snapshot.require(StreamKey(iid, StreamKind.BOOK), age).payload
                    mid = book.mid if isinstance(book, OrderBookSnapshot) else None
                    if mid is not None:
                        out[iid] = mid
                else:
                    dage = timedelta(seconds=self.cfg.limits.max_data_age_derivatives_s)
                    mark = snapshot.require(StreamKey(iid, StreamKind.MARK), dage).payload
                    if isinstance(mark, MarkPrice):
                        out[iid] = mark.mark
            except DataQualityError:
                continue  # absent: valuing a holding without a mark fails closed later
        return out

    def _valuation(
        self,
    ) -> tuple[dict[InstrumentId, Decimal], dict[str, Decimal], dict[str, EquityMarks], Decimal]:
        fx = usd_eur_rate(self.market.snapshot(), self.fx_policy)
        rates = eur_rates(fx)
        marks = self._marks()
        bots = {
            b.bot_id: value_bot(self.ledger, b.bot_id, b.venue, marks, rates).economic_equity_eur
            for b in self.cfg.bots
        }
        total = self.ledger.treasury_eur + sum(bots.values(), Decimal(0))
        eq = self.snapshots.observe(self._clock.now(), {PORTFOLIO: total} | bots)
        return marks, rates, eq, total

    def _monitor(self) -> None:
        if not self.cfg.bots or not self.bootstrapped():
            return
        try:
            _, _, eq, total = self._valuation()
        except (DataQualityError, ValueError) as exc:
            self._note(f"valuation unavailable: {exc}")
            return
        reason = emergency_breach(
            total,
            eq[PORTFOLIO],
            self.cfg.limits.emergency_daily_loss,
            self.cfg.limits.emergency_drawdown,
        )
        if reason and not self.supervisor.emergency_shutdown:
            self.emergency_stop(reason)

    # -- plugin calls -------------------------------------------------------------------------

    def _call(self, entry: PluginEntry, name: str, *args: Any) -> Any:
        try:
            return getattr(entry.plugin, name)(*args)
        except Exception as exc:
            self._fail(entry, name, exc)
            return _FAILED

    def _fail(self, entry: PluginEntry, where: str, exc: Exception, call_stop: bool = True) -> None:
        entry.state = PluginState.FAILED
        entry.error = f"{where}: {type(exc).__name__}: {exc}"[:300]
        entry.counters["errors"] += 1
        pid = entry.manifest.plugin_id
        self._audit(pid, "failed", {"error": entry.error})
        if self.started:
            self._cancel_open(pid)
        if call_stop:
            with contextlib.suppress(Exception):  # already FAILED: a second error changes nothing
                entry.plugin.on_stop(StopReason.ERROR)
        self.export_status()

    def _view(self, entry: PluginEntry) -> MarketView:
        m = entry.manifest
        snapshot = self.market.snapshot()
        age = timedelta(seconds=self.cfg.limits.max_data_age_spot_s)
        quotes: dict[str, Quote] = {}
        last: dict[str, Decimal] = {}
        for iid in m.instruments:
            try:
                ev = snapshot.require(StreamKey(iid, StreamKind.BOOK), age)
                book = ev.payload
                if (
                    isinstance(book, OrderBookSnapshot)
                    and book.best_bid is not None
                    and book.best_ask is not None
                ):
                    quotes[str(iid)] = Quote(
                        instrument_id=iid,
                        bid=book.best_bid.price,
                        ask=book.best_ask.price,
                        as_of=ev.exchange_ts,
                    )
            except DataQualityError:
                pass  # no fresh two-sided book: no quote (never a stale value)
            trade = snapshot.entries.get(StreamKey(iid, StreamKind.TRADES))
            if trade is not None and isinstance(trade.payload, Trade):
                last[str(iid)] = trade.payload.price
        holdings = tuple(
            Holding(
                instrument_id=iid,
                quantity=sum(
                    (lot.quantity for lot in self.ledger.lots(m.plugin_id, iid)), Decimal(0)
                ),
            )
            for iid in m.instruments
        )
        state = self.engine.state
        records = self.ledger.orders()
        open_orders = tuple(
            OpenOrder(
                order_id=o.order_id,
                request_id=o.request_id.split(".", 1)[1],
                instrument_id=o.instrument_id,
                side=o.side,
                quantity=o.quantity,
                filled=o.filled,
                state=records[o.order_id].state.value if o.order_id in records else "pending",
            )
            for o in sorted(state.orders.values(), key=lambda o: o.order_id)
            if o.bot_id == m.plugin_id
        )
        return MarketView(
            plugin_id=m.plugin_id,
            now=self._clock.now(),
            quotes=quotes,
            holdings=holdings,
            open_orders=open_orders,
            last_trade_price=last,
        )

    def _run_plugin(self, entry: PluginEntry) -> None:
        decision = self._call(entry, "on_market", self._view(entry))
        if decision is _FAILED:
            return
        if not isinstance(decision, Decision):
            self._fail(entry, "on_market", TypeError("on_market must return a Decision"))
            return
        pid = entry.manifest.plugin_id
        for signal in decision.signals:
            self._record_signal(entry, signal)
        for oid in decision.cancel_order_ids:
            self._cancel_own(entry, oid)
        for request in decision.orders:
            if entry.state is not PluginState.RUNNING:
                break  # a failure in an earlier callback stops the rest
            self.submit(pid, request)

    def _record_signal(self, entry: PluginEntry, signal: Signal) -> None:
        if signal.instrument_id not in entry.manifest.instruments:
            entry.counters["errors"] += 1
            return
        entry.signals.append({"at": self._clock.now().isoformat()} | signal.model_dump(mode="json"))
        del entry.signals[:-50]

    # -- orders -------------------------------------------------------------------------------

    def submit(self, plugin_id: str, request: OrderRequest) -> OrderUpdate:
        """The single order path for plugins. Returns the first update (ACCEPTED or REJECTED);
        the same update is delivered to the plugin."""
        self._require_running()
        entry = self.registry.get(plugin_id)
        entry.counters["requests"] += 1
        reason = self._host_check(entry, request)
        if reason is not None:
            return self._reject(entry, request, reason)
        now = self._clock.now()
        inst = self.instruments[request.instrument_id]
        bot = self.cfg.bot(plugin_id)
        snapshot = self.market.snapshot()
        spot_sell = request.side is Side.SELL
        try:
            tif = request.time_in_force or (
                TimeInForce.IOC if request.order_type is OrderType.MARKET else TimeInForce.GTC
            )
            terms = OrderTerms(
                order_type=request.order_type,
                time_in_force=tif,
                limit_price=request.limit_price,
                post_only=request.post_only,
            )
        except ValidationError as exc:
            return self._reject(entry, request, f"invalid order terms: {exc.errors()[0]['msg']}")
        try:
            marks, rates, eq, total = self._valuation()
            sigma = self._sigma(inst.id, now, spot_sell)
        except (DataQualityError, ValueError) as exc:
            return self._reject(entry, request, f"risk inputs unavailable: {exc}")
        state = bot_risk_state(
            self.ledger,
            plugin_id,
            bot.venue,
            marks,
            rates,
            start_of_day_eur=eq[plugin_id].start_of_day,
            peak_eur=eq[plugin_id].peak,
            orders_last_minute=self._rate(plugin_id, now),
            halted=entry.state is not PluginState.RUNNING,
        )
        proposal = TradeProposal(
            request_id=f"{plugin_id}.{request.request_id}",
            bot_id=plugin_id,
            instrument_id=inst.id,
            side=request.side,
            quantity=request.quantity,
            leverage=Decimal(1),
            reduce_only=request.reduce_only,
            created_at=now,
        )
        self._equity_now = PortfolioEquity(
            equity_eur=total,
            start_of_day_eur=eq[PORTFOLIO].start_of_day,
            peak_eur=eq[PORTFOLIO].peak,
        )
        try:
            result = self.engine.accept(
                proposal,
                terms,
                state,
                snapshot,
                sigma=sigma,
                eur_per_quote=rates.get(inst.quote, Decimal(0)),
            )
        finally:
            self._equity_now = None
        if result.reason != "duplicate request":
            self._orders[plugin_id].append(now)
        if result.reason == "duplicate request":  # idempotent in the engine: no second order
            return self._reject(
                entry, request, f"duplicate request_id (already order {result.order_id})"
            )
        if not result.accepted:
            return self._reject(entry, request, result.reason or "refused by risk or execution")
        entry.counters["accepted"] += 1
        accepted = self.engine.state.orders.get(result.order_id or "")
        update = OrderUpdate(
            plugin_id=plugin_id,
            request_id=request.request_id,
            order_id=result.order_id,
            status=OrderStatus.ACCEPTED,
            quantity=accepted.quantity if accepted is not None else None,
            reason=result.reason,
            at=now,
        )
        if result.order_id is not None:
            self._seen[result.order_id] = ("accepted", Decimal(0))
        self._deliver(entry, update)
        self._sync_orders()
        return update

    def _host_check(self, entry: PluginEntry, request: OrderRequest) -> str | None:
        if entry.state is not PluginState.RUNNING:
            return f"plugin is {entry.state.value}"
        if request.instrument_id not in entry.manifest.instruments:
            return "instrument is not in the plugin's manifest"
        if self.supervisor.emergency_shutdown and request.side is Side.BUY:
            return "portfolio emergency shutdown: no new exposure"  # the supervisor agrees
        if not self.bootstrapped():
            return "paper capital not bootstrapped (no live EUR rate yet)"
        return None

    def _sigma(self, iid: InstrumentId, now: datetime, spot_sell: bool) -> Decimal:
        """Sigma enters only the derivatives liquidation buffer. A spot SELL (an exit) does
        not wait for the volatility warm-up (0 when none exists); everything else fails closed."""
        try:
            return self.volatility.sigma(iid, now)
        except DataQualityError:
            if spot_sell:
                return Decimal(0)
            raise

    def _rate(self, plugin_id: str, now: datetime) -> int:
        q = self._orders[plugin_id]
        while q and q[0] <= now - timedelta(minutes=1):
            q.popleft()
        return len(q)

    def _reject(self, entry: PluginEntry, request: OrderRequest, reason: str) -> OrderUpdate:
        pid = entry.manifest.plugin_id
        entry.counters["rejected"] += 1
        self.store.append_audit(
            REFUSED,
            f"{pid}:{request.request_id}",
            {"plugin_id": pid, "request": request.model_dump(mode="json"), "reason": reason},
        )
        update = OrderUpdate(
            plugin_id=pid,
            request_id=request.request_id,
            order_id=None,
            status=OrderStatus.REJECTED,
            reason=reason,
            at=self._clock.now(),
        )
        self._deliver(entry, update)
        return update

    def _deliver(self, entry: PluginEntry, update: OrderUpdate) -> None:
        if entry.state is PluginState.RUNNING:
            self._call(entry, "on_order_update", update)

    def _cancel_own(self, entry: PluginEntry, order_id: str) -> None:
        pid = entry.manifest.plugin_id
        order = self.engine.state.orders.get(order_id)
        if order is None or order.bot_id != pid:
            entry.counters["errors"] += 1  # never another plugin's order
            return
        self.engine.request_cancel(order_id)
        self._sync_orders()

    def _cancel_open(self, plugin_id: str) -> None:
        for order in list(self.engine.state.orders.values()):
            if order.bot_id == plugin_id:
                self.engine.request_cancel(order.order_id)
        self._sync_orders()

    def _sync_orders(self) -> None:
        """Deliver every state or fill change of plugin orders (from the ledger)."""
        state = self.engine.state
        for oid, rec in sorted(self.ledger.orders().items()):
            pid = rec.spec.bot_id
            if pid not in self.registry:
                continue
            current = (rec.state.value, rec.filled_qty)
            if self._seen.get(oid) == current:
                continue
            self._seen[oid] = current
            order = state.orders.get(oid)
            rid = order.request_id if order is not None else oid.split(":", 1)[-1]
            update = OrderUpdate(
                plugin_id=pid,
                request_id=rid.split(".", 1)[-1],
                order_id=oid,
                status=_LEDGER_STATUS.get(rec.state.value, OrderStatus.ACCEPTED),
                quantity=rec.spec.quantity,
                filled=rec.filled_qty,
                at=self._clock.now(),
            )
            self._deliver(self.registry.get(pid), update)

    # -- status -------------------------------------------------------------------------------

    def _audit(self, plugin_id: str, event: str, payload: dict[str, Any]) -> None:
        self.store.append_audit(PLUGIN_EVENT, plugin_id, {"event": event} | payload)

    def _note(self, text: str) -> None:
        if not self.notes or self.notes[-1] != text:
            self.notes.append(text)
            del self.notes[:-20]

    def status(self) -> dict[str, Any]:
        state = self.engine.state
        return {
            "version": __version__,
            "paper_only": True,
            "live_trading": "not implemented",
            "started": self.started,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "portfolio_id": self.cfg.portfolio_id,
            "bootstrapped": self.bootstrapped(),
            "emergency_shutdown": self.supervisor.emergency_shutdown,
            "plugins": self.registry.describe(),
            "open_orders": sum(1 for _ in state.orders.values()),
            "streams": len(self.subscriptions()),
            "notes": list(self.notes[-5:]),
        }

    def export_status(self) -> None:
        if self.status_dir is None:
            return
        now = self._clock.now()
        write_status(self.status_dir, "runtime", self.final_status or self.status(), now)
        write_status(self.status_dir, "plugins", {"plugins": self.registry.describe()}, now)


_FAILED = object()
