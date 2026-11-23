"""PURE command planners: (execution state, recorded ledger facts, recorded input) -> plan.

A plan is the new execution state, the ordered ledger/supervisor actions and the audited
outputs. Planners never read a clock, the ledger or market state directly: everything they use
is part of the recorded command, so `replay_execution` recomputes every plan byte-for-byte.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import Field

from shadow_trading_suite.accounting.journal import down, up
from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.market import FundingRate, MarkPrice, OrderBookSnapshot, Trade
from shadow_trading_suite.core.model import DomainModel
from shadow_trading_suite.core.orders import Liquidity, OrderType, Side
from shadow_trading_suite.execution.assumptions import AssumptionSet, LatencyAction, latency
from shadow_trading_suite.execution.matching import (
    band_bounds,
    consumption_key,
    crosses,
    displayed_at,
    floor_to,
    live_consumption,
    queue_step,
    two_sided_uncrossed,
    walk,
)
from shadow_trading_suite.execution.model import (
    Consumption,
    ExecLiquidation,
    ExecOrder,
    ExecState,
    FillRecord,
    FundingQuote,
    LiquidationStage,
    MarkQuote,
    OrderTerms,
    Phase,
    TimeInForce,
    instrument_key,
    stream_key,
    ts_key,
)
from shadow_trading_suite.streaming.events import StreamEvent, StreamKind

TRADE_ID_WINDOW = 2000
LIQUIDATION_MODEL = "sim-isolated-v1 (ASSUMPTION)"


class PositionFact(DomainModel):
    bot_id: str
    instrument_id: InstrumentId
    base: str
    side: Side
    quantity: Decimal
    avg_entry: Decimal
    contract_size: Decimal
    margin: Decimal
    payable: Decimal  # outstanding funding liability of this position (positive)
    pending_reduce: Decimal


class Facts(DomainModel):
    """Ledger values a plan depends on, recorded with the command."""

    positions: dict[str, PositionFact]
    cash: dict[str, Decimal] = Field(default_factory=dict)  # "bot@venue" -> free cash


@dataclass(frozen=True)
class Ctx:
    assumptions: AssumptionSet
    lots: Mapping[str, Decimal]  # instrument key -> lot size
    profiles: Mapping[str, str]  # bot -> latency profile id

    def seconds(self, name: str) -> timedelta:
        return timedelta(seconds=float(self.assumptions.decimal(name)))

    @property
    def replenish(self) -> timedelta:
        return timedelta(milliseconds=float(self.assumptions.decimal("liquidity.replenish_ms")))

    def delay(self, bot_id: str, subject: str, action: LatencyAction) -> timedelta:
        profile = self.assumptions.profile(self.profiles[bot_id])
        return latency(profile, self.assumptions.text("latency.seed"), subject, action)

    def fee(self, *, spot: bool, liquidity: Liquidity) -> Decimal:
        cls = "spot" if spot else "derivatives"
        role = "taker" if liquidity is Liquidity.TAKER else "maker"
        return self.assumptions.decimal(f"fee.{cls}_{role}")

    def mmr(self, base: str) -> Decimal:
        major = base in self.assumptions.text("mmr.cryptocom_major_bases").split(",")
        return self.assumptions.decimal("mmr.cryptocom_major" if major else "mmr.cryptocom_other")


@dataclass
class Plan:
    state: ExecState
    actions: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)


def _short(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _venue_orders(s: ExecState, venue: Venue) -> list[ExecOrder]:
    return sorted(
        (o for o in s.orders.values() if o.instrument_id.venue is venue),
        key=lambda o: (o.accepted_at, o.bot_id, o.order_id),
    )


def _close(plan: Plan, order: ExecOrder, final: str, ts: datetime, reason: str) -> None:
    plan.actions.append(
        {"op": "close", "order_id": order.order_id, "final": final, "ts": ts_key(ts)}
    )
    plan.outputs.append(
        {
            "event": "order_closed",
            "order_id": order.order_id,
            "final": final,
            "reason": reason,
            "filled": order.filled,
            "quantity": order.quantity,
        }
    )
    plan.state.orders.pop(order.order_id, None)


def _fill(
    plan: Plan,
    ctx: Ctx,
    order: ExecOrder,
    *,
    price: Decimal,
    quantity: Decimal,
    liquidity: Liquidity,
    ts: datetime,
    source_id: str,
    best: Decimal | None,
    basis: str,
    book_event_id: str | None = None,
    trade_id: str | None = None,
) -> ExecOrder:
    fee_rate = ctx.fee(spot=order.is_spot, liquidity=liquidity)
    record = FillRecord(
        fill_id=f"{order.order_id}:{_short(source_id)}:{order.fills}",
        order_id=order.order_id,
        bot_id=order.bot_id,
        instrument_id=order.instrument_id,
        side=order.side,
        price=price,
        quantity=quantity,
        liquidity=liquidity,
        fee_rate=fee_rate,
        ts=ts,
        book_event_id=book_event_id,
        trade_id=trade_id,
        decision_mid=order.decision_mid,
        best_price=best,
        basis=basis,
    )
    plan.actions.append({"op": "fill", **record.model_dump(mode="json")})
    plan.outputs.append({"event": "fill", **record.model_dump(mode="json")})
    updated = order.model_copy(update={"filled": order.filled + quantity, "fills": order.fills + 1})
    if updated.remaining == 0:
        plan.state.orders.pop(order.order_id, None)
        plan.outputs.append({"event": "order_filled", "order_id": order.order_id})
    else:
        plan.state.orders[order.order_id] = updated
    return updated


def _consume(
    s: ExecState,
    book: OrderBookSnapshot,
    side: str,
    price: Decimal,
    base: Decimal,
    ts: datetime,
    event_id: str,
    replenish: timedelta,
) -> None:
    """Record consumed displayed size; the replenish window restarts at the latest use."""
    key = consumption_key(book.instrument_id, side, price)
    level = next(lv for lv in (book.bids if side == "bid" else book.asks) if lv.price == price)
    used = live_consumption(s.consumed.get(key), level, ts, replenish)
    s.consumed[key] = Consumption(
        instrument_id=book.instrument_id,
        book_side=side,
        price=price,
        level_quantity=level.quantity,
        consumed=used + base,
        ts=ts,
        book_event_id=event_id,
    )


def _prune(s: ExecState, book: OrderBookSnapshot, t: datetime, replenish: timedelta) -> None:
    prefix = instrument_key(book.instrument_id) + ":"
    for key, c in list(s.consumed.items()):
        if not key.startswith(prefix):
            continue
        levels = book.bids if c.book_side == "bid" else book.asks
        level = next((lv for lv in levels if lv.price == c.price), None)
        if level is None or live_consumption(c, level, t, replenish) == 0:
            del s.consumed[key]


# -- accept / cancel ---------------------------------------------------------------------------


class AcceptInput(DomainModel):
    order_id: str
    request_id: str
    bot_id: str
    instrument_id: InstrumentId
    side: Side
    quantity: Decimal
    terms: OrderTerms
    reduce_only: bool
    leverage: Decimal
    contract_size: Decimal
    is_spot: bool
    decision_mid: Decimal
    band: Decimal
    market_time: datetime


def plan_accept(state: ExecState, inp: AcceptInput, ctx: Ctx) -> Plan:
    s = state.model_copy(deep=True)
    plan = Plan(s)
    t = inp.market_time
    gtc = inp.terms.order_type is OrderType.LIMIT and inp.terms.time_in_force is TimeInForce.GTC
    order = ExecOrder(
        order_id=inp.order_id,
        request_id=inp.request_id,
        bot_id=inp.bot_id,
        instrument_id=inp.instrument_id,
        side=inp.side,
        quantity=inp.quantity,
        terms=inp.terms,
        reduce_only=inp.reduce_only,
        leverage=inp.leverage,
        contract_size=inp.contract_size,
        lot_size=ctx.lots[instrument_key(inp.instrument_id)],
        is_spot=inp.is_spot,
        decision_mid=inp.decision_mid,
        band=inp.band,
        profile_id=ctx.profiles[inp.bot_id],
        accepted_at=t,
        effective_at=t + ctx.delay(inp.bot_id, inp.order_id, LatencyAction.SUBMIT),
        expires_at=t + ctx.seconds("order.gtc_lifetime_s") if gtc else None,
    )
    s.orders[order.order_id] = order
    s.decided[inp.request_id] = order.order_id
    vk = inp.instrument_id.venue.value
    s.market_time[vk] = max(s.market_time.get(vk, t), t)
    plan.outputs.append(
        {
            "event": "accepted",
            "order_id": order.order_id,
            "effective_at": ts_key(order.effective_at),
            "profile": order.profile_id,
        }
    )
    return plan


def plan_refusal(state: ExecState, request_id: str, reason: str) -> Plan:
    s = state.model_copy(deep=True)
    s.decided[request_id] = ""
    return Plan(s, [], [{"event": "refused", "request_id": request_id, "reason": reason}])


def plan_cancel(state: ExecState, order_id: str, ctx: Ctx) -> Plan:
    s = state.model_copy(deep=True)
    plan = Plan(s)
    order = s.orders.get(order_id)
    if order is None or order.cancel_effective_at is not None:
        plan.outputs.append({"event": "cancel_noop", "order_id": order_id})
        return plan
    t = s.market_time.get(order.instrument_id.venue.value, order.accepted_at)
    effective = t + ctx.delay(order.bot_id, order_id, LatencyAction.CANCEL)
    s.orders[order_id] = order.model_copy(update={"cancel_effective_at": effective})
    plan.actions.append({"op": "cancel_request", "order_id": order_id, "ts": ts_key(t)})
    plan.outputs.append(
        {"event": "cancel_requested", "order_id": order_id, "effective_at": ts_key(effective)}
    )
    return plan


# -- market events -----------------------------------------------------------------------------


class MarketInput(DomainModel):
    event: StreamEvent
    valid: bool
    reason: str = ""


def plan_market(state: ExecState, facts: Facts, inp: MarketInput, ctx: Ctx) -> Plan:
    s = state.model_copy(deep=True)
    plan = Plan(s)
    ev = inp.event
    skey = stream_key(ev.instrument_id, ev.kind.value)
    if s.checkpoints.get(skey) == ev.event_id:
        plan.outputs.append({"event": "duplicate", "event_id": ev.event_id})
        return plan
    s.checkpoints[skey] = ev.event_id
    venue = ev.instrument_id.venue
    t = max(s.market_time.get(venue.value, ev.exchange_ts), ev.exchange_ts)
    s.market_time[venue.value] = t
    positions = dict(facts.positions)
    cash = dict(facts.cash)
    payload = ev.payload
    ikey = instrument_key(ev.instrument_id)
    if inp.valid and ev.kind is StreamKind.MARK and isinstance(payload, MarkPrice):
        s.marks[ikey] = MarkQuote(mark=payload.mark, ts=ev.exchange_ts, event_id=ev.event_id)
        s.mark_missing_since.pop(ikey, None)
    if inp.valid and ev.kind is StreamKind.FUNDING and isinstance(payload, FundingRate):
        s.funding[ikey] = FundingQuote(
            rate=payload.rate,
            ts=payload.ts,
            interval_s=int(payload.interval.total_seconds()),
            event_id=ev.event_id,
        )
        if ikey not in s.funding_next:
            s.funding_next[ikey] = _next_boundary(t, payload.interval)
    _timers(plan, venue, t)
    _funding_boundaries(plan, ctx, positions, cash, venue, t)
    if not inp.valid:
        plan.outputs.append({"event": "data_quality", "stream": skey, "reason": inp.reason})
        return plan
    if ev.kind is StreamKind.BOOK and isinstance(payload, OrderBookSnapshot):
        _on_book(plan, ctx, positions, ev, payload, t)
    elif ev.kind is StreamKind.TRADES and isinstance(payload, Trade):
        _on_trade(plan, ctx, ev, payload, t)
    elif ev.kind is StreamKind.MARK and isinstance(payload, MarkPrice):
        _on_mark(plan, ctx, positions, ev, payload, t)
    return plan


def _next_boundary(t: datetime, interval: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=t.tzinfo)
    n = (t - epoch) // interval + 1
    return epoch + n * interval


def _timers(plan: Plan, venue: Venue, t: datetime) -> None:
    for order in _venue_orders(plan.state, venue):
        if order.cancel_effective_at is not None and order.cancel_effective_at <= t:
            _close(plan, order, "cancelled", t, "cancel_request")
        elif order.expires_at is not None and order.expires_at <= t:
            _close(plan, order, "expired", t, "gtc_lifetime")


def _apply_funding(
    positions: dict[str, PositionFact],
    cash: dict[str, Decimal],
    pkey: str,
    rate: Decimal,
    mark: Decimal,
) -> None:
    """Mirror of the ledger's funding planner on the recorded facts, so that the stage-1
    check later in the SAME command sees the charge (cash, then margin, then payable)."""
    pos = positions[pkey]
    owner = f"{pos.bot_id}@{pos.instrument_id.venue.value}"
    raw = pos.quantity * pos.contract_size * mark * abs(rate)
    if rate == 0 or raw == 0:
        return
    free = cash.get(owner, Decimal(0))
    if (rate > 0) == (pos.side is Side.BUY):
        amount = up(raw)
        from_cash = min(amount, max(free, Decimal(0)))
        from_margin = min(amount - from_cash, max(pos.margin, Decimal(0)))
        cash[owner] = free - from_cash
        positions[pkey] = pos.model_copy(
            update={
                "margin": pos.margin - from_margin,
                "payable": pos.payable + amount - from_cash - from_margin,
            }
        )
    else:
        cash[owner] = free + down(raw)


def _funding_boundaries(
    plan: Plan,
    ctx: Ctx,
    positions: dict[str, PositionFact],
    cash: dict[str, Decimal],
    venue: Venue,
    t: datetime,
) -> None:
    s = plan.state
    max_mark_age = ctx.seconds("data.max_age_derivatives_s")
    for ikey, boundary in sorted(s.funding_next.items()):
        if not ikey.startswith(venue.value + ":"):
            continue
        quote = s.funding.get(ikey)
        interval = timedelta(seconds=quote.interval_s if quote else 3600)
        while boundary <= t:
            holders = sorted(
                k for k, p in positions.items() if instrument_key(p.instrument_id) == ikey
            )
            mark = s.marks.get(ikey)
            rate_ok = quote is not None and boundary - 2 * interval <= quote.ts <= t
            mark_ok = mark is not None and t - mark.ts <= max_mark_age
            for pkey in holders:
                pos = positions[pkey]
                if rate_ok and mark_ok and quote is not None and mark is not None:
                    plan.actions.append(
                        {
                            "op": "funding",
                            "op_id": f"fund:{ikey}:{ts_key(boundary)}:{pkey}",
                            "bot_id": pos.bot_id,
                            "instrument_id": pos.instrument_id.model_dump(mode="json"),
                            "rate": quote.rate,
                            "mark": mark.mark,
                            "ts": ts_key(boundary),
                        }
                    )
                    _apply_funding(positions, cash, pkey, quote.rate, mark.mark)
                    plan.outputs.append(
                        {
                            "event": "funding",
                            "position": pkey,
                            "boundary": ts_key(boundary),
                            "rate": quote.rate,
                            "mark": mark.mark,
                            "rate_event": quote.event_id,
                        }
                    )
                else:
                    pending = s.funding_unsettled.get(ikey, ())
                    s.funding_unsettled[ikey] = (*pending, f"{ts_key(boundary)}|{pkey}")
                    plan.outputs.append(
                        {
                            "event": "funding_unsettled",
                            "position": pkey,
                            "boundary": ts_key(boundary),
                            "reason": "no fresh rate" if not rate_ok else "no fresh mark",
                        }
                    )
            boundary += interval
        s.funding_next[ikey] = boundary


def _on_book(
    plan: Plan,
    ctx: Ctx,
    positions: dict[str, PositionFact],
    ev: StreamEvent,
    book: OrderBookSnapshot,
    t: datetime,
) -> None:
    s = plan.state
    if not two_sided_uncrossed(book):
        plan.outputs.append(
            {
                "event": "data_quality",
                "stream": ev.event_id,
                "reason": "book not two-sided or crossed/locked",
            }
        )
        return
    _prune(s, book, t, ctx.replenish)
    ikey = instrument_key(book.instrument_id)
    for pkey, liq in sorted(s.liquidations.items()):
        if instrument_key(liq.instrument_id) != ikey or ev.exchange_ts < liq.effective_at:
            continue
        _liquidate(plan, ctx, positions, pkey, liq, ev, book, t)
    pending = sorted(
        (
            o
            for o in s.orders.values()
            if o.phase is Phase.PENDING
            and instrument_key(o.instrument_id) == ikey
            and o.effective_at <= ev.exchange_ts
        ),
        key=lambda o: (o.effective_at, o.bot_id, o.order_id),
    )
    for order in pending:
        _activate(plan, ctx, order, ev, book, t)


def _taker(
    plan: Plan,
    ctx: Ctx,
    order: ExecOrder,
    ev: StreamEvent,
    book: OrderBookSnapshot,
    t: datetime,
    *,
    limit: Decimal | None,
) -> tuple[ExecOrder, str]:
    s = plan.state
    result = walk(
        book,
        s.consumed,
        buy=order.buy,
        quantity=order.remaining,
        contract_size=order.contract_size,
        lot_size=order.lot_size,
        limit=limit,
        band=band_bounds(order.decision_mid, order.band),
        now=t,
        replenish=ctx.replenish,
    )
    best = book.best_ask if order.buy else book.best_bid
    for take in result.takes:
        _consume(
            s,
            book,
            "ask" if order.buy else "bid",
            take.price,
            take.base,
            t,
            ev.event_id,
            ctx.replenish,
        )
        order = _fill(
            plan,
            ctx,
            order,
            price=take.price,
            quantity=take.quantity,
            liquidity=Liquidity.TAKER,
            ts=t,
            source_id=ev.event_id,
            best=best.price if best else None,
            basis="book_walk",
            book_event_id=ev.event_id,
        )
    if result.stop in ("price_band", "depth"):
        plan.outputs.append(
            {"event": result.stop, "order_id": order.order_id, "unfilled": order.remaining}
        )
    return order, result.stop


def _activate(
    plan: Plan, ctx: Ctx, order: ExecOrder, ev: StreamEvent, book: OrderBookSnapshot, t: datetime
) -> None:
    terms = order.terms
    if terms.order_type is OrderType.MARKET or terms.time_in_force is TimeInForce.IOC:
        order, stop = _taker(plan, ctx, order, ev, book, t, limit=terms.limit_price)
        if order.remaining > 0:
            _close(plan, order, "cancelled", t, f"ioc_remainder:{stop}")
        return
    limit = terms.limit_price
    assert limit is not None  # noqa: S101 - guaranteed by OrderTerms
    if terms.time_in_force is TimeInForce.FOK:
        probe = walk(
            book,
            plan.state.consumed,
            buy=order.buy,
            quantity=order.remaining,
            contract_size=order.contract_size,
            lot_size=order.lot_size,
            limit=limit,
            band=band_bounds(order.decision_mid, order.band),
            now=t,
            replenish=ctx.replenish,
        )
        if probe.quantity < order.remaining:
            _close(plan, order, "cancelled", t, f"fok_unfillable:{probe.stop}")
            return
        order, _ = _taker(plan, ctx, order, ev, book, t, limit=limit)
        return
    if crosses(book, buy=order.buy, limit=limit):
        if terms.post_only:
            _close(plan, order, "cancelled", t, "post_only_would_cross")
            return
        order, stop = _taker(plan, ctx, order, ev, book, t, limit=limit)
        if order.remaining == 0:
            return
        if stop == "price_band":
            _close(plan, order, "cancelled", t, "price_band")
            return
    _rest(plan, order, book, t)


def _rest(plan: Plan, order: ExecOrder, book: OrderBookSnapshot, t: datetime) -> None:
    s = plan.state
    limit = order.terms.limit_price
    assert limit is not None  # noqa: S101
    ahead = displayed_at(book, buy=order.buy, price=limit)
    for other in s.orders.values():  # earlier paper orders at the same price are ahead (E8)
        if (
            other.phase is Phase.RESTING
            and other.instrument_id == order.instrument_id
            and other.side is order.side
            and other.terms.limit_price == limit
        ):
            ahead += other.remaining * other.contract_size
    s.counters["rest"] = s.counters.get("rest", 0) + 1
    s.orders[order.order_id] = order.model_copy(
        update={
            "phase": Phase.RESTING,
            "queue_ahead": ahead,
            "rested_at": t,
            "rest_seq": s.counters["rest"],
        }
    )
    plan.outputs.append(
        {"event": "rested", "order_id": order.order_id, "price": limit, "queue_ahead": ahead}
    )


def _on_trade(plan: Plan, ctx: Ctx, ev: StreamEvent, trade: Trade, t: datetime) -> None:
    s = plan.state
    ikey = instrument_key(trade.instrument_id)
    seen = s.trades_seen.get(ikey, ())
    if trade.trade_id in seen:
        plan.outputs.append({"event": "duplicate_trade", "trade_id": trade.trade_id})
        return
    s.trades_seen[ikey] = (*seen, trade.trade_id)[-TRADE_ID_WINDOW:]
    available = trade.quantity
    resting = sorted(
        (
            o
            for o in s.orders.values()
            if o.phase is Phase.RESTING and instrument_key(o.instrument_id) == ikey
        ),
        key=lambda o: o.rest_seq or 0,
    )
    for order in resting:
        r = queue_step(order, trade, available, trade.instrument_id.venue)
        available = r.trade_left
        order = order.model_copy(update={"queue_ahead": r.queue_ahead})
        s.orders[order.order_id] = order
        if r.fill > 0:
            limit = order.terms.limit_price
            assert limit is not None  # noqa: S101
            _fill(
                plan,
                ctx,
                order,
                price=limit,
                quantity=r.fill,
                liquidity=Liquidity.MAKER,
                ts=t,
                source_id=f"{ev.event_id}|{trade.trade_id}",
                best=None,
                basis="queue_model",
                trade_id=trade.trade_id,
            )


def _on_mark(
    plan: Plan,
    ctx: Ctx,
    positions: dict[str, PositionFact],
    ev: StreamEvent,
    mark: MarkPrice,
    t: datetime,
) -> None:
    s = plan.state
    ikey = instrument_key(ev.instrument_id)
    for pkey, pos in sorted(positions.items()):
        if instrument_key(pos.instrument_id) != ikey or pkey in s.liquidations:
            continue
        check = maintenance_check(pos, mark.mark, ctx)
        if check["breach"]:
            _trigger(plan, ctx, pkey, pos, mark.mark, check, t)


def maintenance_check(pos: PositionFact, mark: Decimal, ctx: Ctx) -> dict[str, Any]:
    """Stage 1 (ASSUMPTION model): position equity at the mark vs maintenance requirement plus
    the estimated close fee. Outstanding funding of the position reduces its equity (B5)."""
    direction = 1 if pos.side is Side.BUY else -1
    notional = mark * pos.quantity * pos.contract_size
    upnl = (mark - pos.avg_entry) * pos.quantity * pos.contract_size * direction
    equity = pos.margin + upnl - pos.payable
    requirement = notional * (ctx.mmr(pos.base) + ctx.fee(spot=False, liquidity=Liquidity.TAKER))
    return {
        "breach": equity < requirement,
        "equity": equity,
        "requirement": requirement,
        "deficit": max(requirement - equity, Decimal(0)),
    }


def _trigger(
    plan: Plan,
    ctx: Ctx,
    pkey: str,
    pos: PositionFact,
    mark: Decimal,
    check: dict[str, Any],
    t: datetime,
) -> None:
    s = plan.state
    for order in _venue_orders(s, pos.instrument_id.venue):  # stage 2: the position is locked
        if order.bot_id == pos.bot_id and order.instrument_id == pos.instrument_id:
            _close(plan, order, "cancelled", t, "liquidation")
    trigger = {
        "mark": mark,
        "maintenance_requirement": check["requirement"],
        "position_equity": check["equity"],
        "ts": ts_key(t),
    }
    plan.outputs.append(
        {
            "event": "mm_breach",
            "position": pkey,
            **trigger,
            "deficit": check["deficit"],
            "model": LIQUIDATION_MODEL,
        }
    )
    plan.actions.append(
        {
            "op": "liq_begin",
            "op_id": f"liq:{pkey}:{ts_key(t)}:begin",
            "bot_id": pos.bot_id,
            "instrument_id": pos.instrument_id.model_dump(mode="json"),
            "trigger": trigger,
        }
    )
    effective = t + ctx.delay(pos.bot_id, f"{pkey}:{ts_key(t)}", LatencyAction.LIQUIDATION)
    s.liquidations[pkey] = ExecLiquidation(
        bot_id=pos.bot_id,
        instrument_id=pos.instrument_id,
        stage=LiquidationStage.TRIGGERED,
        triggered_at=t,
        effective_at=effective,
    )
    plan.outputs.append(
        {
            "event": "liq_trigger",
            "position": pkey,
            "policy": ctx.assumptions.text("liquidation.trigger_policy"),
            "effective_at": ts_key(effective),
            "model": LIQUIDATION_MODEL,
        }
    )


def _liquidate(
    plan: Plan,
    ctx: Ctx,
    positions: dict[str, PositionFact],
    pkey: str,
    liq: ExecLiquidation,
    ev: StreamEvent,
    book: OrderBookSnapshot,
    t: datetime,
) -> None:
    """Stage 3: taker walk of the remaining shared liquidity of THIS fresh LIVE book (no price
    band, B6); any remainder continues on later books, never at an invented price."""
    s = plan.state
    pos = positions.get(pkey)
    if pos is None:
        return
    lot = ctx.lots[instrument_key(pos.instrument_id)]
    sell = pos.side is Side.BUY
    result = walk(
        book,
        s.consumed,
        buy=not sell,
        quantity=pos.quantity,
        contract_size=pos.contract_size,
        lot_size=lot,
        limit=None,
        band=None,
        now=t,
        replenish=ctx.replenish,
    )
    liq = liq.model_copy(update={"stage": LiquidationStage.LIQUIDATING})
    fee_rate = ctx.assumptions.decimal("liquidation.fee_rate")
    base_id = f"liq:{pkey}:{ts_key(liq.triggered_at)}"
    left = pos.quantity
    for take in result.takes:
        _consume(
            s, book, "bid" if sell else "ask", take.price, take.base, t, ev.event_id, ctx.replenish
        )
        plan.actions.append(
            {
                "op": "liq_fill",
                "op_id": f"{base_id}:fill:{liq.fills}",
                "bot_id": pos.bot_id,
                "instrument_id": pos.instrument_id.model_dump(mode="json"),
                "price": take.price,
                "quantity": take.quantity,
                "fee_rate": fee_rate,
            }
        )
        plan.outputs.append(
            {
                "event": "liq_fill",
                "position": pkey,
                "price": take.price,
                "quantity": take.quantity,
                "book_event_id": ev.event_id,
                "n": liq.fills,
                "model": LIQUIDATION_MODEL,
            }
        )
        liq = liq.model_copy(update={"fills": liq.fills + 1})
        left -= take.quantity
    if left == 0:
        plan.actions.append(
            {
                "op": "liq_finish",
                "op_id": f"{base_id}:finish",
                "bot_id": pos.bot_id,
                "instrument_id": pos.instrument_id.model_dump(mode="json"),
            }
        )
        plan.outputs.append(
            {"event": "liq_settlement", "position": pkey, "model": LIQUIDATION_MODEL}
        )
        del s.liquidations[pkey]
        positions.pop(pkey)
    else:
        s.liquidations[pkey] = liq
        positions[pkey] = pos.model_copy(update={"quantity": left})
        if result.stop == "depth":
            plan.outputs.append(
                {"event": "liq_depth_exhausted", "position": pkey, "remaining": left}
            )


# -- data-integrity commands (E4) --------------------------------------------------------------


class TickInput(DomainModel):
    now: datetime  # wall clock (injected)
    status: dict[str, str]  # stream key -> StreamStatus value
    emergency: bool


def _venues_in_use(s: ExecState, facts: Facts) -> set[str]:
    used = {o.instrument_id.venue.value for o in s.orders.values()}
    used |= {p.instrument_id.venue.value for p in facts.positions.values()}
    return used | set(s.blocked)


def _block(plan: Plan, venue: Venue, reason: str, t: datetime) -> None:
    s = plan.state
    for order in _venue_orders(s, venue):
        _close(plan, order, "cancelled", t, f"gap:{reason}")
    if venue.value not in s.blocked:
        s.blocked[venue.value] = reason
        plan.outputs.append({"event": "venue_blocked", "venue": venue.value, "reason": reason})


def reduces(order: ExecOrder) -> bool:
    """Exposure-reducing orders survive the emergency shutdown: derivatives reduce-only and,
    since risk rules v2, every spot SELL (spot is unleveraged; the sold inventory is held)."""
    return order.reduce_only or (order.is_spot and order.side is Side.SELL)


def plan_tick(state: ExecState, facts: Facts, inp: TickInput, ctx: Ctx) -> Plan:
    s = state.model_copy(deep=True)
    plan = Plan(s)
    gap_after = ctx.seconds("gap.cancel_after_s")
    for vk in sorted(_venues_in_use(s, facts) | {k.split(":")[0] for k in inp.status}):
        venue = Venue(vk)
        statuses = [v for k, v in inp.status.items() if k.startswith(vk + ":")]
        live = bool(statuses) and all(v == "live" for v in statuses)
        t = s.market_time.get(vk, inp.now)
        if live:
            s.down_since.pop(vk, None)
            if vk in s.blocked:
                del s.blocked[vk]
                plan.outputs.append({"event": "venue_unblocked", "venue": vk})
            continue
        since = s.down_since.setdefault(vk, inp.now)
        if inp.now - since > gap_after:
            _block(plan, venue, "data not LIVE > gap threshold", t)
    missing_after = ctx.seconds("mark.missing_emergency_s")
    for pos in sorted(facts.positions.values(), key=lambda p: (p.bot_id, p.instrument_id.symbol)):
        if pos.instrument_id.venue is not Venue.CRYPTOCOM:
            continue
        ikey = instrument_key(pos.instrument_id)
        if inp.status.get(stream_key(pos.instrument_id, "mark")) == "live":
            s.mark_missing_since.pop(ikey, None)
            continue
        since = s.mark_missing_since.setdefault(ikey, inp.now)
        if inp.now - since > missing_after and not inp.emergency:
            plan.actions.append({"op": "emergency", "reason": f"{ikey}: mark unavailable > 5 s"})
            plan.outputs.append({"event": "mark_unavailable", "instrument": ikey})
            inp = inp.model_copy(update={"emergency": True})
    if inp.emergency:
        for order in sorted(s.orders.values(), key=lambda o: o.order_id):
            if not reduces(order):
                t = s.market_time.get(order.instrument_id.venue.value, inp.now)
                _close(plan, order, "cancelled", t, "emergency_shutdown")
    return plan


class GapInput(DomainModel):
    venue: Venue
    reason: str
    now: datetime


def plan_gap(state: ExecState, inp: GapInput) -> Plan:
    s = state.model_copy(deep=True)
    plan = Plan(s)
    s.down_since.setdefault(inp.venue.value, inp.now)
    _block(plan, inp.venue, inp.reason, s.market_time.get(inp.venue.value, inp.now))
    return plan


def plan_restart(state: ExecState, now: datetime) -> Plan:
    """A restart counts as a gap (E4): every resting/pending order is cancelled from the
    recovered state; positions and in-progress liquidations are kept."""
    s = state.model_copy(deep=True)
    plan = Plan(s)
    for venue in Venue:
        s.down_since.setdefault(venue.value, now)
        _block(plan, venue, "restart", s.market_time.get(venue.value, now))
    return plan


def lot_floor(quantity: Decimal, lot: Decimal) -> Decimal:
    return floor_to(quantity, lot)
