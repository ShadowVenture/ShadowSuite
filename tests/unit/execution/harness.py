"""Test harness: a real ledger store, CapitalManager, M5 supervisor and pipeline, a real
MarketState and the paper execution engine, driven by deterministic scripted events (E10)."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from shadow_trading_suite.accounting.journal import bot_account
from shadow_trading_suite.accounting.trading import OPEN_STATES
from shadow_trading_suite.config.settings import Settings
from shadow_trading_suite.core.instrument import Instrument, Venue
from shadow_trading_suite.core.market import (
    BookLevel,
    FundingRate,
    MarkPrice,
    OrderBookSnapshot,
    Trade,
)
from shadow_trading_suite.core.orders import OrderType, Side
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.execution.engine import AcceptResult, PaperExecutionEngine
from shadow_trading_suite.execution.model import OrderTerms, TimeInForce
from shadow_trading_suite.execution.planner import Plan
from shadow_trading_suite.execution.replay import replay_execution
from shadow_trading_suite.risk.capital import CapitalManager
from shadow_trading_suite.risk.engine import BotState, TradeProposal
from shadow_trading_suite.risk.pipeline import RiskPipeline, replay_audit
from shadow_trading_suite.risk.supervisor import PortfolioEquity, PortfolioSupervisor
from shadow_trading_suite.storage.ledger import LedgerStore
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind
from shadow_trading_suite.streaming.state import MarketState, StreamKey, StreamStatus
from tests.support.portfolio import sample_portfolio_config
from tests.unit.risk.test_engine import CB_SPOT, CC_SPOT, PERP, cc

__all__ = [
    "CB_SPOT",
    "CC_SPOT",
    "ETH_PERP",
    "PERP",
    "T0",
    "Harness",
    "limit_terms",
    "market_terms",
]

D = Decimal
CFG = sample_portfolio_config()
T0 = datetime(2026, 10, 19, 12, 0, 0, 500000, tzinfo=UTC)
RATE = D("0.86")
EQUITY = PortfolioEquity(equity_eur=D(5000), start_of_day_eur=D(5000), peak_eur=D(5000))
ETH_PERP = cc("ETHUSD-PERP")
INSTRUMENTS: dict[Any, Instrument] = {i.id: i for i in (PERP, CC_SPOT, CB_SPOT, ETH_PERP)}
PERP_FEEDS = (StreamKind.BOOK, StreamKind.TRADES, StreamKind.MARK, StreamKind.FUNDING)
SPOT_FEEDS = (StreamKind.BOOK, StreamKind.TRADES)

Levels = list[tuple[str, str]]


def market_terms() -> OrderTerms:
    return OrderTerms(order_type=OrderType.MARKET, time_in_force=TimeInForce.IOC)


def limit_terms(
    price: str, tif: TimeInForce = TimeInForce.GTC, post_only: bool = False
) -> OrderTerms:
    return OrderTerms(
        order_type=OrderType.LIMIT, time_in_force=tif, limit_price=D(price), post_only=post_only
    )


def bot_state(bot: str) -> BotState:
    eq = CFG.bot(bot).allocation_eur
    return BotState(
        bot_id=bot,
        equity_eur=eq,
        start_of_day_equity_eur=eq,
        peak_equity_eur=eq,
        orders_last_minute=0,
        halted=False,
    )


class Harness:
    def __init__(
        self,
        tmp: Path,
        *,
        fresh: bool = True,
        profiles: dict[str, str] | None = None,
        start: bool = True,
        clock: ManualClock | None = None,
    ) -> None:
        self.tmp = tmp
        self.profiles = profiles
        self.clock = clock or ManualClock(T0)
        self.store = LedgerStore.open(tmp / "data", clock=self.clock)
        self.ledger = CapitalManager(self.store, self.clock, CFG.fees.conversion_cost.value)
        if fresh:
            self.ledger.deposit_paper_capital("seed", CFG.starting_capital_eur)
            for b in CFG.bots:
                self.ledger.allocate(
                    f"alloc:{b.bot_id}", b.bot_id, b.venue, "USD", b.allocation_eur, RATE
                )
        self.supervisor = PortfolioSupervisor(CFG, self.ledger, self.store, lambda: EQUITY)
        self.pipeline = RiskPipeline(CFG, self.supervisor, self.store)
        self.market = MarketState(self.clock, {})
        self.engine = PaperExecutionEngine(
            settings=Settings(),
            cfg=CFG,
            ledger=self.ledger,
            supervisor=self.supervisor,
            pipeline=self.pipeline,
            instruments=INSTRUMENTS,
            clock=self.clock,
            profiles=profiles,
        )
        self._n = 0
        self.event_prefix = "1"
        self.last_mark = "82000"
        if start:
            self.engine.start()

    # -- time and streams -------------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self.clock.now()

    def at(self, ms: int) -> datetime:
        """Set the clock to T0 + ms (never backwards): deterministic across processes."""
        target = T0 + timedelta(milliseconds=ms)
        if target > self.clock.now():
            self.clock.set(target)
        return self.clock.now()

    def advance(self, ms: int) -> datetime:
        self.clock.set(self.clock.now() + timedelta(milliseconds=ms))
        return self.clock.now()

    def live(self, inst: Instrument, kinds: tuple[StreamKind, ...] | None = None) -> None:
        feeds = kinds or (SPOT_FEEDS if inst.kind.value == "spot" else PERP_FEEDS)
        for k in feeds:
            self.market.set_status(StreamKey(inst.id, k), StreamStatus.LIVE)

    def unblock(self) -> Plan:
        return self.engine.tick(self.market.snapshot())

    def _event(self, inst: Instrument, kind: StreamKind, payload: Any, eid: str | None) -> Plan:
        self._n += 1
        key = StreamKey(inst.id, kind)
        event = StreamEvent(
            event_id=eid
            or f"{inst.id.venue.value}:{kind.value}:{inst.id.symbol}:{self.event_prefix}:{self._n}",
            kind=kind,
            instrument_id=inst.id,
            exchange_ts=self.now,
            received_at=self.now,
            epoch=1,
            payload=payload,
        )
        self.market.apply(event)
        return self.engine.on_market(self.market.snapshot(), key)

    def book(self, inst: Instrument, bids: Levels, asks: Levels, eid: str | None = None) -> Plan:
        snap = OrderBookSnapshot(
            instrument_id=inst.id,
            ts=self.now,
            bids=tuple(BookLevel(price=D(p), quantity=D(q)) for p, q in bids),
            asks=tuple(BookLevel(price=D(p), quantity=D(q)) for p, q in asks),
        )
        return self._event(inst, StreamKind.BOOK, snap, eid)

    def trade(
        self, inst: Instrument, price: str, qty: str, aggressor: Side | None, tid: str
    ) -> Plan:
        t = Trade(
            instrument_id=inst.id,
            trade_id=tid,
            ts=self.now,
            price=D(price),
            quantity=D(qty),
            aggressor=aggressor,
        )
        return self._event(inst, StreamKind.TRADES, t, None)

    def mark(self, inst: Instrument, price: str) -> Plan:
        return self._event(
            inst,
            StreamKind.MARK,
            MarkPrice(instrument_id=inst.id, ts=self.now, mark=D(price)),
            None,
        )

    def funding(self, inst: Instrument, rate: str) -> Plan:
        f = FundingRate(
            instrument_id=inst.id, ts=self.now, rate=D(rate), interval=timedelta(hours=1)
        )
        return self._event(inst, StreamKind.FUNDING, f, None)

    def perp_ready(
        self, inst: Instrument = PERP, bids: Levels | None = None, asks: Levels | None = None
    ) -> None:
        """LIVE feeds, a fresh mark and funding rate and a book; venue unblocked."""
        self.live(inst)
        self.mark(inst, "82000")
        self.funding(inst, "0.00001")
        self.book(inst, bids or [("81999", "1")], asks or [("82001", "1")])
        self.trade(inst, "82000", "0.001", Side.BUY, f"warm-{self._n}")
        self.unblock()

    def spot_ready(self, inst: Instrument, bids: Levels, asks: Levels) -> None:
        self.live(inst)
        self.book(inst, bids, asks)
        self.trade(inst, bids[0][0], "0.00001", Side.SELL, f"warm-{self._n}")
        self.unblock()

    # -- orders --------------------------------------------------------------------------------

    def accept(
        self,
        inst: Instrument,
        side: Side,
        qty: str,
        terms: OrderTerms,
        *,
        bot: str = "bot-03",
        rid: str = "r1",
        leverage: str = "5",
        reduce_only: bool = False,
    ) -> AcceptResult:
        spot = inst.kind.value == "spot"
        p = TradeProposal(
            request_id=rid,
            bot_id=bot,
            instrument_id=inst.id,
            side=side,
            quantity=D(qty),
            leverage=D(1) if spot else D(leverage),
            reduce_only=reduce_only,
            created_at=self.now,
        )
        return self.engine.accept(
            p, terms, bot_state(bot), self.market.snapshot(), sigma=D("0.002"), eur_per_quote=RATE
        )

    # -- checks --------------------------------------------------------------------------------

    def cash(self, bot: str = "bot-03", venue: Venue = Venue.CRYPTOCOM) -> Decimal:
        return self.ledger.balance(bot_account(bot, venue, "cash"), "USD")

    def reserved(self, bot: str = "bot-03", venue: Venue = Venue.CRYPTOCOM) -> Decimal:
        return self.ledger.balance(bot_account(bot, venue, "reserved"), "USD")

    def fills(self, plan: Plan) -> list[dict[str, Any]]:
        return [o for o in plan.outputs if o["event"] == "fill"]

    def check_invariants(self) -> None:
        """Invariants 1-9 and 11 (proposal §4.8) after a command."""
        ledger = self.ledger
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for (account, ccy), bal in ledger.balances().items():
            totals[ccy] += bal
            kind = account.split(":")[1]
            if kind in ("cash", "reserved", "spot", "spot_reserved", "margin", "treasury"):
                assert bal >= 0, (account, bal)
            if kind == "payable":
                assert bal <= 0, (account, bal)
        assert all(v == 0 for v in totals.values()), totals  # trial balance per currency
        state = self.engine.state
        orders = ledger.orders()
        open_ids = {oid for oid, r in orders.items() if r.state in OPEN_STATES}
        assert open_ids == set(state.orders)  # execution == ledger (no orphan reservation)
        for oid, order in state.orders.items():
            record = orders[oid]
            assert record.filled_qty == order.filled
            assert record.filled_qty <= record.spec.quantity
        held = sum(
            (
                r.reserved
                for oid, r in orders.items()
                if oid in open_ids
                and r.reserved_currency == "USD"
                and not (r.spec.instrument.is_spot and r.spec.side is Side.SELL)
            ),
            D(0),
        )
        live_holds = sum((h.amount for h in ledger.holds().values() if not h.released), D(0))
        reserved = sum(
            (
                bal
                for (acct, ccy), bal in ledger.balances().items()
                if acct.endswith(":reserved") and ccy == "USD"
            ),
            D(0),
        )
        assert reserved == held + live_holds  # every reservation belongs to an open order
        for rid in self.supervisor.exposures():  # invariant 9: exposures = orders + positions
            backing = self.supervisor.backing(rid)
            assert backing is not None, rid
            record = orders[backing]
            iid = record.spec.instrument.instrument_id
            assert record.state in OPEN_STATES or (
                ledger.position(record.spec.bot_id, iid) is not None
                or ledger.lots(record.spec.bot_id, iid)
            ), rid
        assert set(ledger.liquidations()) == set(state.liquidations)
        bad_debt = sum((v for v in ledger._state.bad_debt.values()), D(0))
        absorbed = -sum(
            (
                bal
                for (acct, _), bal in ledger.balances().items()
                if acct.endswith(":sim:insurance_absorption")
            ),
            D(0),
        )
        assert bad_debt == absorbed  # invariant 8

    def verify_all(self) -> None:
        self.check_invariants()
        assert self.ledger.verify_replay() == []
        assert replay_execution(self.store, self.engine.ctx) == []
        assert replay_audit(self.store.audit_records(), CFG) == []
        self.store.verify_audit_chain()

    def restart(self, *, start: bool = True) -> "Harness":
        self.store.close()
        return Harness(self.tmp, fresh=False, profiles=self.profiles, start=start, clock=self.clock)

    def close(self) -> None:
        self.store.close()
