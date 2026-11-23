"""Independent accounting reconciliation (M6-A requirement 2).

A deliberately separate reference model recomputes, from the ACCEPTED inputs only (never from
the journal), cash identities, reservations, margin, positions, realised and unrealised PnL,
fees, funding, FX conversion and simulated bad debt. Random operation sequences across two
bots must reconcile exactly with the ledger, the trial balance must be zero per currency,
replay must reproduce every operation and a restart must reproduce the state.
"""

import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from shadow_trading_suite.accounting.journal import bot_account, portfolio_account
from shadow_trading_suite.accounting.reporting import value_bot
from shadow_trading_suite.accounting.trading import (
    CapitalError,
    FillSpec,
    InstrumentTerms,
    InsufficientCapitalError,
    LiquidationExecution,
    LiquidationTrigger,
    OrderSpec,
    OrderState,
    TradingLedger,
)
from shadow_trading_suite.core.instrument import InstrumentId, InstrumentKind, Venue
from shadow_trading_suite.core.orders import Liquidity, Side
from shadow_trading_suite.core.time import ManualClock
from shadow_trading_suite.storage.ledger import LedgerStore
from tests.unit.accounting.legacy_ops import book_legacy_reserve

D = Decimal
Q = D("1e-8")
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
RATE, COST = D("0.86"), D("0.002")
VENUE = Venue.CRYPTOCOM
BOTS = ("bot-03", "bot-04")
SPOT = InstrumentTerms(
    instrument_id=InstrumentId(venue=VENUE, symbol="BTC_USD"),
    kind=InstrumentKind.SPOT,
    base="BTC",
    quote="USD",
)
PERP = InstrumentTerms(
    instrument_id=InstrumentId(venue=VENUE, symbol="BTCUSD-PERP"),
    kind=InstrumentKind.PERPETUAL,
    base="BTC",
    quote="USD",
    settle="USD",
)
FUNDING_MARK = D("82000.37")


def size(seed: int, n: int) -> Decimal:
    """Mostly 1e-4 steps; sometimes 8-decimal sizes so every rounding branch is exercised."""
    if seed % 5 == 0:
        return D("0.00000001") * (1 + seed % 997)
    return D("0.0001") * (1 + seed % n)


def price_of(seed: int) -> Decimal:
    return D(80000 + seed % 4000) + D(seed % 97) / 100


NEVER_REFUSED = frozenset({"cancel", "funding", "liquidate"})
MARKS = {SPOT.instrument_id: D(82500), PERP.instrument_id: D(82500)}


def r_up(x: Decimal) -> Decimal:
    return x.quantize(Q, ROUND_CEILING)


def r_down(x: Decimal) -> Decimal:
    return x.quantize(Q, ROUND_FLOOR)


@dataclass
class RefPosition:
    side: Side
    qty: Decimal
    avg: Decimal
    margin: Decimal


@dataclass
class Reference:
    """Independent arithmetic of the accounting rules (no journal, no ledger internals)."""

    allocated_usd: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    fees: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    liq_fees: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    funding_paid: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    funding_received: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    realized: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    absorbed: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    reserve_debits: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    lots: dict[str, list[list[Decimal]]] = field(default_factory=lambda: defaultdict(list))
    positions: dict[str, RefPosition] = field(default_factory=dict)
    fx_eur: Decimal = field(default_factory=Decimal)
    conversion_cost: Decimal = field(default_factory=Decimal)
    reserve_eur: Decimal = field(default_factory=Decimal)

    def allocate(self, bot: str, eur: Decimal) -> None:
        cost = eur * COST
        usd = r_down((eur - cost) / RATE)
        self.allocated_usd[bot] += usd
        self.fx_eur += eur - cost
        self.conversion_cost += cost

    def reserve(self, bot: str, usd: Decimal) -> None:
        gross = r_down(usd * RATE)
        credited = r_down(gross * (1 - COST))
        self.reserve_debits[bot] += usd
        self.fx_eur -= gross
        self.reserve_eur += credited
        self.conversion_cost += gross - credited

    def fill(self, spec: OrderSpec, price: Decimal, qty: Decimal, rate: Decimal) -> None:
        bot = spec.bot_id
        if spec.instrument.is_spot and spec.side is Side.BUY:
            cost = r_up(price * qty)
            self.fees[bot] += r_up(cost * rate)
            self.lots[bot].append([qty, cost])
        elif spec.instrument.is_spot:
            proceeds = r_down(price * qty)
            self.fees[bot] += r_up(proceeds * rate)
            basis, left = D(0), qty
            while left > 0:
                lot = self.lots[bot][0]
                take = min(left, lot[0])
                part = lot[1] if take == lot[0] else r_down(lot[1] * take / lot[0])
                basis, left = basis + part, left - take
                if take == lot[0]:
                    self.lots[bot].pop(0)
                else:
                    lot[0], lot[1] = lot[0] - take, lot[1] - part
            self.realized[bot] += proceeds - basis
        elif not spec.reduce_only:
            notional = price * qty
            self.fees[bot] += r_up(notional * rate)
            margin = r_up(notional / spec.leverage)
            pos = self.positions.get(bot)
            if pos is None:
                self.positions[bot] = RefPosition(spec.side, qty, price, margin)
            else:
                pos.avg = (pos.avg * pos.qty + price * qty) / (pos.qty + qty)
                pos.qty += qty
                pos.margin += margin
        else:
            pos = self.positions[bot]
            pnl = (price - pos.avg) * qty * (1 if pos.side is Side.BUY else -1)
            if pnl < 0:
                pos.margin -= r_up(-pnl)
                self.realized[bot] -= r_up(-pnl)
            else:
                self.realized[bot] += r_down(pnl)
            released = pos.margin if qty == pos.qty else r_down(pos.margin * qty / pos.qty)
            pos.margin -= released
            pos.qty -= qty
            if pos.qty == 0:
                del self.positions[bot]
            self.fees[bot] += r_up(price * qty * rate)

    def funding(self, bot: str, rate: Decimal, mark: Decimal) -> None:
        pos = self.positions[bot]
        raw = pos.qty * mark * abs(rate)
        if (rate > 0) == (pos.side is Side.BUY):
            self.funding_paid[bot] += r_up(raw)
        else:
            self.funding_received[bot] += r_down(raw)

    def liquidate(self, bot: str, price: Decimal, fee_rate: Decimal) -> None:
        pos = self.positions.pop(bot)
        pnl = (price - pos.avg) * pos.qty * (1 if pos.side is Side.BUY else -1)
        fee = r_up(price * pos.qty * fee_rate)
        loss = r_up(-pnl) if pnl < 0 else D(0)
        self.realized[bot] += -loss if pnl < 0 else r_down(pnl)
        remaining = pos.margin - min(loss, pos.margin)
        self.absorbed[bot] += (loss - min(loss, pos.margin)) + (fee - min(fee, remaining))
        self.liq_fees[bot] += fee


class Runner:
    def __init__(self, ledger: TradingLedger) -> None:
        self.ledger = ledger
        self.ref = Reference()
        self.specs: dict[str, OrderSpec] = {}
        self.n = 0

    def attempt(self, op: tuple[str, int, int]) -> None:
        kind, b, seed = op
        bot = BOTS[b]
        self.n += 1
        try:
            getattr(self, f"_{kind}")(bot, seed)
        except InsufficientCapitalError:
            if kind in NEVER_REFUSED:
                raise
        except CapitalError:
            if kind in NEVER_REFUSED or kind == "fill":
                raise  # preconditions hold: a refusal here is a ledger bug

    def _spec(
        self,
        bot: str,
        inst: InstrumentTerms,
        side: Side,
        qty: Decimal,
        price: Decimal,
        fee: str,
        lev: str = "1",
        reduce_only: bool = False,
    ) -> OrderSpec:
        return OrderSpec(
            order_id=f"o{self.n}",
            bot_id=bot,
            instrument=inst,
            side=side,
            quantity=qty,
            reserve_price=price,
            reserve_fee_rate=D(fee),
            leverage=D(lev),
            reduce_only=reduce_only,
            created_at=NOW,
        )

    def _open(self, spec: OrderSpec, seed: int) -> None:
        self.ledger.open_order(spec)
        self.specs[spec.order_id] = spec
        if seed % 4:  # most orders trade at once (fully or partially)
            self._fill_order(spec.order_id, seed // 4)

    def _fill_order(self, oid: str, seed: int) -> None:
        spec, record = self.specs[oid], self.ledger.order(oid)
        qty = record.remaining if seed % 2 else min(record.remaining, size(seed, 7))
        price = spec.reserve_price + D(seed % 600 - 300)
        rate = D("0.004") if spec.instrument.is_spot else D("0.0002")
        if seed % 3 == 0:
            rate = D("0.006") if spec.instrument.is_spot else D("0.0006")
        self.ledger.book_fill(
            FillSpec(
                fill_id=f"f{self.n}:{oid}",
                order_id=oid,
                price=price,
                quantity=qty,
                fee_rate=rate,
                liquidity=Liquidity.TAKER,
                ts=NOW,
            )
        )
        self.ref.fill(spec, price, qty, rate)

    def _spot_buy(self, bot: str, seed: int) -> None:
        qty = size(seed, 20)
        self._open(self._spec(bot, SPOT, Side.BUY, qty, price_of(seed), "0.006"), seed)

    def _spot_sell(self, bot: str, seed: int) -> None:
        held = sum((lot[0] for lot in self.ref.lots[bot]), D(0))
        pending = sum(
            (
                s.quantity
                for oid, s in self.specs.items()
                if s.bot_id == bot
                and s.instrument.is_spot
                and s.side is Side.SELL
                and self.ledger.order(oid).state not in (OrderState.FILLED, OrderState.CANCELLED)
            ),
            D(0),
        )
        free = held - pending
        if free > 0:
            qty = min(free, size(seed, 20))
            self._open(self._spec(bot, SPOT, Side.SELL, qty, price_of(seed), "0.006"), seed)

    def _perp_open(self, bot: str, seed: int) -> None:
        pos = self.ref.positions.get(bot)
        side = pos.side if pos else (Side.BUY if seed % 2 else Side.SELL)
        qty = size(seed, 30)
        self._open(
            self._spec(bot, PERP, side, qty, price_of(seed), "0.0006", lev=str(1 + seed % 20)), seed
        )

    def _perp_reduce(self, bot: str, seed: int) -> None:
        pos = self.ledger.position(bot, PERP.instrument_id)
        if pos is not None and pos.quantity - pos.pending_reduce > 0:
            side = Side.SELL if pos.side is Side.BUY else Side.BUY
            qty = min(pos.quantity - pos.pending_reduce, size(seed, 30))
            self._open(
                self._spec(bot, PERP, side, qty, price_of(seed), "0.0006", reduce_only=True), seed
            )

    def _fill(self, bot: str, seed: int) -> None:
        open_ids = [
            oid
            for oid, s in sorted(self.specs.items())
            if s.bot_id == bot
            and self.ledger.order(oid).state not in (OrderState.FILLED, OrderState.CANCELLED)
        ]
        if not open_ids:
            return
        self._fill_order(open_ids[seed % len(open_ids)], seed)

    def _cancel(self, bot: str, seed: int) -> None:
        open_ids = [
            oid
            for oid, s in sorted(self.specs.items())
            if s.bot_id == bot
            and self.ledger.order(oid).state not in (OrderState.FILLED, OrderState.CANCELLED)
        ]
        if open_ids:
            self.ledger.close_order(open_ids[seed % len(open_ids)], OrderState.CANCELLED, NOW)

    def _funding(self, bot: str, seed: int) -> None:
        if bot in self.ref.positions:
            rate = D(seed % 21 - 10) * D("0.0000013")  # odd rates and a cent mark: inexact
            self.ledger.book_funding(
                f"fund{self.n}", bot, PERP.instrument_id, rate, FUNDING_MARK, NOW
            )
            if rate:
                self.ref.funding(bot, rate, FUNDING_MARK)

    def _liquidate(self, bot: str, seed: int) -> None:
        pos = self.ledger.position(bot, PERP.instrument_id)
        if pos is None or pos.pending_reduce > 0:
            return
        move = D(1) - D(seed % 25) / 100 if pos.side is Side.BUY else D(1) + D(seed % 25) / 100
        price = (pos.avg_entry * move).quantize(D("0.1"))
        self.ledger.settle_liquidation(
            f"liq{self.n}",
            bot,
            PERP.instrument_id,
            [LiquidationExecution(price=price, quantity=pos.quantity)],
            D("0.005"),
            LiquidationTrigger(
                mark=price, maintenance_requirement=D(0), position_equity=D(0), ts=NOW
            ),
        )
        self.ref.liquidate(bot, price, D("0.005"))

    def _reserve(self, bot: str, seed: int) -> None:
        usd = D(1 + seed % 50)
        # historical op kind (new reserves are refused); must still reconcile
        book_legacy_reserve(self.ledger, f"res{self.n}", {(bot, VENUE, "USD"): usd}, RATE)
        self.ref.reserve(bot, usd)


def reconcile(ledger: TradingLedger, ref: Reference) -> None:
    bal = ledger.balances()

    def b(account: str, ccy: str = "USD") -> Decimal:
        return bal.get((account, ccy), D(0))

    totals: dict[str, Decimal] = defaultdict(Decimal)
    for (account, ccy), amount in bal.items():
        totals[ccy] += amount
        kind = account.split(":")[1]
        if kind in ("cash", "reserved", "spot", "spot_reserved", "margin", "treasury", "reserve"):
            assert amount >= 0, (account, ccy, amount)
    assert all(v == 0 for v in totals.values()), f"trial balance {dict(totals)}"
    # FX conversion and the (inactive) profit reserve
    assert b(portfolio_account("fx", "EUR-USD"), "EUR") == ref.fx_eur
    assert b(portfolio_account("expense", "conversion_cost"), "EUR") == ref.conversion_cost
    assert ledger.reserve_eur == ref.reserve_eur
    orders, holds = ledger.orders(), ledger.holds()
    for bot in BOTS:

        def a(kind: str, detail: str | None = None, _bot: str = bot) -> str:
            return bot_account(_bot, VENUE, kind, detail)

        # reservations equal what open orders and active holds still need
        open_cash = sum(
            (
                o.reserved
                for o in orders.values()
                if o.spec.bot_id == bot
                and o.state not in (OrderState.FILLED, OrderState.CANCELLED)
                and o.reserved_currency == "USD"
            ),
            D(0),
        )
        open_holds = sum(
            (h.amount for h in holds.values() if h.bot_id == bot and not h.released), D(0)
        )
        assert b(a("reserved")) == open_cash + open_holds
        open_btc = sum(
            (
                o.reserved
                for o in orders.values()
                if o.spec.bot_id == bot
                and o.state not in (OrderState.FILLED, OrderState.CANCELLED)
                and o.reserved_currency == "BTC"
            ),
            D(0),
        )
        assert b(a("spot_reserved"), "BTC") == open_btc
        # spot inventory and cost basis = FIFO lots (independent reference)
        lot_qty = sum((lot[0] for lot in ref.lots[bot]), D(0))
        lot_cost = sum((lot[1] for lot in ref.lots[bot]), D(0))
        assert b(a("spot"), "BTC") + b(a("spot_reserved"), "BTC") == lot_qty
        assert b(a("fx", "BTC-USD"), "BTC") == -lot_qty
        assert b(a("fx", "BTC-USD")) == lot_cost
        # positions and isolated margin
        pos, rpos = ledger.position(bot, PERP.instrument_id), ref.positions.get(bot)
        assert (pos is None) == (rpos is None)
        if pos is not None and rpos is not None:
            assert (pos.side, pos.quantity, pos.avg_entry) == (rpos.side, rpos.qty, rpos.avg)
            assert b(a("margin", "BTCUSD-PERP")) == rpos.margin
        else:
            assert b(a("margin", "BTCUSD-PERP")) == 0
        # income statement accounts
        assert -b(a("pnl", "realized_price")) == ref.realized[bot]
        assert b(a("expense", "trading_fee")) == ref.fees[bot]
        assert b(a("expense", "liquidation_fee")) == ref.liq_fees[bot]
        assert b(a("funding", "paid")) == ref.funding_paid[bot]
        assert -b(a("funding", "received")) == ref.funding_received[bot]
        assert -b(a("sim", "insurance_absorption")) == ref.absorbed[bot]
        assert ledger.bad_debt(bot, VENUE, "USD") == ref.absorbed[bot]
        # cash identity: holdings = funding sources - uses (each term from one source only)
        held = b(a("cash")) + b(a("reserved")) + b(a("margin", "BTCUSD-PERP")) + lot_cost
        expected = (
            ref.allocated_usd[bot]
            - ref.reserve_debits[bot]
            + ref.realized[bot]
            - ref.fees[bot]
            - ref.liq_fees[bot]
            - ref.funding_paid[bot]
            + ref.funding_received[bot]
            + ref.absorbed[bot]
        )
        assert held == expected, (bot, held, expected)
        # unrealised PnL and equity at marks (reference arithmetic)
        usd = (
            value_bot(ledger, bot, VENUE, MARKS, {"USD": RATE}).currency("USD")
            if (b(a("cash")) or lot_qty or rpos)
            else None
        )
        if usd is not None:
            upnl = lot_qty * MARKS[SPOT.instrument_id] - lot_cost
            if rpos is not None:
                upnl += (
                    (MARKS[PERP.instrument_id] - rpos.avg)
                    * rpos.qty
                    * (1 if rpos.side is Side.BUY else -1)
                )
            assert usd.unrealized_pnl == upnl
            assert usd.economic_equity == usd.ledger_equity - ref.absorbed[bot]


KINDS = (
    "spot_buy",
    "spot_buy",
    "spot_sell",
    "spot_sell",
    "perp_open",
    "perp_open",
    "perp_open",
    "perp_reduce",
    "fill",
    "fill",
    "cancel",
    "funding",
    "funding",
    "funding",
    "liquidate",
    "liquidate",
    "reserve",
)
ops = st.lists(
    st.tuples(st.sampled_from(KINDS), st.integers(0, 1), st.integers(0, 10**6)),
    min_size=5,
    max_size=50,
)


@settings(max_examples=60, deadline=None)
@given(sequence=ops)
def test_random_sequences_reconcile_independently(sequence: list[tuple[str, int, int]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LedgerStore.open(Path(tmp) / "d", clock=ManualClock(NOW))
        try:
            ledger = TradingLedger(store, ManualClock(NOW), COST)
            runner = Runner(ledger)
            ledger.deposit_paper_capital("seed", D(5000))
            for bot, eur in zip(BOTS, (D(1500), D(1500)), strict=True):
                ledger.allocate(f"alloc:{bot}", bot, VENUE, "USD", eur, RATE)
                runner.ref.allocate(bot, eur)
            for op in sequence:
                runner.attempt(op)
                reconcile(ledger, runner.ref)
            assert ledger.verify_replay() == []
            digest = ledger.state_digest()
            restarted = TradingLedger(store, ManualClock(NOW), COST)
            assert restarted.state_digest() == digest
            reconcile(restarted, runner.ref)
        finally:
            store.close()
