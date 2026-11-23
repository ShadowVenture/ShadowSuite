"""Paper trading ledger (M6-A): capital, reservations, margin and position accounting.

Every state change is ONE operation with a stable `op_id`:
- its inputs are canonicalised to JSON (the payload) and handed to a PURE planner, which
  derives balanced journal entries, order transitions and the changed projection objects
  (effects) from the payload and the prior state only;
- planner output is validated (per-currency balance, non-negative holdings) and added to the
  store's unit of work; the in-memory projection is swapped in only after COMMIT;
- re-running an operation id with identical content is a no-op returning the stored result,
  different content is refused as a conflict.
Restart rebuilds the projection from the persisted postings and effects; `verify_replay()`
re-plans every stored operation from its payload and reports any difference (determinism and
tamper check).

Accounting model (proposal §4, all simulation conventions are ASSUMPTIONS):
- spot: inventory in base units, cost basis in `fx:{base}-{quote}`, FIFO lots, realised price
  PnL only on sale; fees always separate (`expense:trading_fee`).
- derivatives: isolated margin per position (`margin:{symbol}`), weighted-average entry,
  realised price PnL on reduction (losses from margin first), unrealised PnL never posted.
- funding settles to cash (from margin when cash is short); any remainder that cash and margin
  cannot cover stays an outstanding internal liability `payable:funding:{symbol}` (M6-B, B5)
  until `settle_payable` pays it from the same bot's cash.
- liquidation settlement: losses beyond posted margin are credited to
  `sim:insurance_absorption` (simulated recovery, never income) and memo'd as bad debt. Either
  in one operation for the whole position (`settle_liquidation`, M6-A) or per executed fill
  (`begin_liquidation` -> `book_liquidation_fill`* -> `finish_liquidation`, M6-B, B3); the
  margin stays locked until the position is flat.
Nothing here contacts an exchange or moves real funds.
"""

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import Field, TypeAdapter, model_validator

from shadow_trading_suite.accounting.journal import (
    NON_NEGATIVE_KINDS,
    NON_POSITIVE_KINDS,
    QUANTUM,
    JournalEntry,
    Posting,
    account_kind,
    apply_entry,
    bot_account,
    down,
    portfolio_account,
    up,
)
from shadow_trading_suite.core.decimal import Dec
from shadow_trading_suite.core.instrument import Currency, InstrumentId, InstrumentKind, Venue
from shadow_trading_suite.core.model import DomainModel, LeverageDec, NonNegativeDec, PositiveDec
from shadow_trading_suite.core.orders import Liquidity, Side
from shadow_trading_suite.core.time import Clock, UtcDatetime
from shadow_trading_suite.storage.ledger import (
    LedgerEvent,
    LedgerStore,
    StoredOperation,
    TransitionRow,
    canonical,
    canonical_json,
)

AMOUNT_QUANTUM = QUANTUM
AccountKey = tuple[str, Venue, str]

TREASURY = portfolio_account("treasury")
RESERVE = portfolio_account("reserve")
PAPER_CAPITAL = portfolio_account("equity", "paper_capital")
CONVERSION_COST = portfolio_account("expense", "conversion_cost")


class CapitalError(Exception):
    pass


class InsufficientCapitalError(CapitalError):
    pass


class LedgerError(CapitalError):
    pass


# -- domain values ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    """M5 view of one bot cash account: balance = cash + reserved, committed = reserved."""

    balance: Decimal = Decimal(0)
    committed: Decimal = Decimal(0)

    @property
    def free(self) -> Decimal:
        return self.balance - self.committed


class InstrumentTerms(DomainModel):
    """The contract terms accounting needs (from validated venue metadata)."""

    instrument_id: InstrumentId
    kind: InstrumentKind
    base: Currency
    quote: Currency
    settle: Currency | None = None
    contract_size: PositiveDec = Decimal(1)

    @property
    def is_spot(self) -> bool:
        return self.kind is InstrumentKind.SPOT

    @property
    def settle_currency(self) -> str:
        return self.settle or self.quote

    @property
    def key(self) -> str:
        return f"{self.instrument_id.venue.value}:{self.instrument_id.symbol}"


class OrderState(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    CANCEL_REQUESTED = "cancel_requested"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


OPEN_STATES = frozenset(
    {OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED, OrderState.CANCEL_REQUESTED}
)
TERMINAL_STATES = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED})


class OrderSpec(DomainModel):
    """An accepted PAPER order. `reserve_price`/`reserve_fee_rate` size its reservation
    (limit price, or the protected worst price for market orders; the maximum fee rate)."""

    order_id: str = Field(min_length=1, max_length=160)
    bot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    instrument: InstrumentTerms
    side: Side
    quantity: PositiveDec
    reserve_price: PositiveDec
    reserve_fee_rate: NonNegativeDec
    leverage: LeverageDec = Decimal(1)
    reduce_only: bool = False
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _spot_rules(self) -> Self:
        if self.instrument.is_spot and (self.leverage != 1 or self.reduce_only):
            raise ValueError("spot orders are unleveraged and cannot be reduce-only")
        return self


class OrderRecord(DomainModel):
    spec: OrderSpec
    state: OrderState
    filled_qty: NonNegativeDec
    reserved: NonNegativeDec
    reserved_currency: Currency
    transitions: int = Field(ge=1)

    @property
    def remaining(self) -> Decimal:
        return self.spec.quantity - self.filled_qty


class FillSpec(DomainModel):
    """A simulated fill handed to the ledger (matching itself is M6-B)."""

    fill_id: str = Field(min_length=1, max_length=200)
    order_id: str
    price: PositiveDec
    quantity: PositiveDec
    fee_rate: NonNegativeDec
    liquidity: Liquidity
    ts: UtcDatetime


class Lot(DomainModel):
    quantity: PositiveDec
    cost: NonNegativeDec  # quote currency, excluding fees


class Position(DomainModel):
    bot_id: str
    instrument: InstrumentTerms
    side: Side
    quantity: PositiveDec  # contracts
    avg_entry: PositiveDec  # weighted average
    pending_reduce: NonNegativeDec = Decimal(0)


class Hold(DomainModel):
    reference: str
    bot_id: str
    venue: Venue
    currency: Currency
    amount: PositiveDec
    released: bool = False


class LiquidationExecution(DomainModel):
    price: PositiveDec
    quantity: PositiveDec


class LiquidationTrigger(DomainModel):
    """Stage 1-2 evidence recorded with the settlement (simulated model, ASSUMPTION)."""

    mark: PositiveDec
    maintenance_requirement: NonNegativeDec
    position_equity: Dec
    ts: UtcDatetime


class LiquidationReport(DomainModel):
    model: str = "sim-isolated-v1 (ASSUMPTION)"
    trigger: LiquidationTrigger
    executed_quantity: PositiveDec
    average_price: PositiveDec
    execution_loss: NonNegativeDec  # 0 when the execution realised a gain
    execution_gain: NonNegativeDec
    liquidation_fee: NonNegativeDec
    margin_before: NonNegativeDec
    margin_returned: NonNegativeDec
    insurance_absorbed: NonNegativeDec  # simulated; economic loss = ledger loss + this


class LiquidationProgress(DomainModel):
    """A per-fill liquidation in progress (B3): stage 2 trigger plus what is booked so far.

    Invariant while open: margin - absorbed = margin_before + pnl - fees - funding taken from
    margin since the trigger, with margin * absorbed == 0 (pooled isolated margin)."""

    bot_id: str
    instrument: InstrumentTerms
    side: Side
    avg_entry: PositiveDec
    start_quantity: PositiveDec
    trigger: LiquidationTrigger
    margin_before: NonNegativeDec
    executed_quantity: NonNegativeDec = Decimal(0)
    notional: NonNegativeDec = Decimal(0)
    pnl: Dec = Decimal(0)  # signed, per-fill rounded (losses up, gains down)
    fees: NonNegativeDec = Decimal(0)
    absorbed: NonNegativeDec = Decimal(0)  # net insurance absorption of this liquidation
    fills: int = Field(default=0, ge=0)


# -- projection state ------------------------------------------------------------------------

_COLLECTIONS: dict[str, type[DomainModel] | None] = {
    "holds": Hold,
    "orders": OrderRecord,
    "positions": Position,
    "lots": Lot,
    "bad_debt": None,
    "liquidations": LiquidationProgress,
}


@dataclass
class LedgerState:
    balances: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    holds: dict[str, Hold] = field(default_factory=dict)
    orders: dict[str, OrderRecord] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    lots: dict[str, tuple[Lot, ...]] = field(default_factory=dict)
    bad_debt: dict[str, Decimal] = field(default_factory=dict)
    liquidations: dict[str, LiquidationProgress] = field(default_factory=dict)
    touched: dict[str, set[str]] = field(default_factory=dict)

    def copy(self) -> "LedgerState":
        return LedgerState(
            dict(self.balances),
            dict(self.holds),
            dict(self.orders),
            dict(self.positions),
            dict(self.lots),
            dict(self.bad_debt),
            dict(self.liquidations),
        )

    def put(self, collection: str, key: str, value: Any) -> None:
        target: dict[str, Any] = getattr(self, collection)
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value
        self.touched.setdefault(collection, set()).add(key)

    def effects(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for collection, keys in sorted(self.touched.items()):
            target: dict[str, Any] = getattr(self, collection)
            out[collection] = {k: _dump(target.get(k)) for k in sorted(keys)}
        return out

    def apply_effects(self, effects: Mapping[str, Any]) -> None:
        for collection, changes in effects.items():
            if collection == "result":
                continue
            target: dict[str, Any] = getattr(self, collection)
            for key, value in changes.items():
                if value is None:
                    target.pop(key, None)
                else:
                    target[key] = _load(collection, value)


def _dump(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, tuple):
        return [v.model_dump() for v in value]
    if isinstance(value, DomainModel):
        return value.model_dump()
    return value


def _load(collection: str, value: Any) -> Any:
    model = _COLLECTIONS[collection]
    if collection == "lots":
        return tuple(Lot.model_validate(v) for v in value)
    if model is None:
        return Decimal(value)
    return model.model_validate(value)


def _jsonable(payload: Any) -> Any:
    """The exact JSON form persisted; planners only ever see this form (live == replay)."""
    return json.loads(canonical_json(payload))


def _model_payload(model: DomainModel) -> dict[str, Any]:
    return model.model_dump()


@dataclass
class _Plan:
    entries: list[JournalEntry] = field(default_factory=list)
    transitions: list[TransitionRow] = field(default_factory=list)
    result: Any = None


def _position_key(bot_id: str, instrument_id: InstrumentId) -> str:
    return f"{bot_id}|{instrument_id.venue.value}:{instrument_id.symbol}"


def _entry(memo: str, *postings: tuple[str, str, Decimal]) -> JournalEntry | None:
    rows = [Posting(account=a, currency=c, amount=amt) for a, c, amt in postings if amt != 0]
    return JournalEntry(memo=memo, postings=tuple(rows)) if rows else None


# -- the ledger --------------------------------------------------------------------------------


class TradingLedger:
    def __init__(
        self,
        store: LedgerStore,
        clock: Clock,
        conversion_cost: Decimal,
        *,
        treasury_floor_eur: Decimal = Decimal(0),
    ) -> None:
        if not isinstance(store, LedgerStore):  # LTR-03: never a live ledger store
            raise TypeError("TradingLedger needs a paper LedgerStore, never a live ledger store")
        if conversion_cost <= 0:
            raise ValueError("conversion cost must be modelled explicitly (> 0)")
        if treasury_floor_eur < 0:
            raise ValueError("treasury floor cannot be negative")
        self._store = store
        self._clock = clock
        self._cost = conversion_cost
        self._floor = treasury_floor_eur
        self._lock = store.lock  # ONE lock for ledger + store: no lock-order inversion
        self._state = LedgerState()
        for op in store.operations():
            self._restore(self._state, op)

    # -- queries -----------------------------------------------------------------------------

    @property
    def store(self) -> LedgerStore:
        return self._store

    def _current(self) -> LedgerState:
        """The state including uncommitted changes of the caller's open unit of work."""
        active = self._store._active
        if active is not None and id(self) in active.drafts:
            return active.drafts[id(self)]  # type: ignore[no-any-return]
        return self._state

    def balance(self, account: str, currency: str) -> Decimal:
        with self._lock:
            return self._current().balances.get((account, currency), Decimal(0))

    def balances(self) -> dict[tuple[str, str], Decimal]:
        with self._lock:
            return {k: v for k, v in self._current().balances.items() if v != 0}

    @property
    def treasury_eur(self) -> Decimal:
        return self.balance(TREASURY, "EUR")

    @property
    def reserve_eur(self) -> Decimal:
        return self.balance(RESERVE, "EUR")

    @property
    def conversion_costs_eur(self) -> Decimal:
        return self.balance(CONVERSION_COST, "EUR")

    def account(self, bot_id: str, venue: Venue, currency: str) -> Account:
        with self._lock:
            s = self._current()
            cash = s.balances.get((bot_account(bot_id, venue, "cash"), currency), Decimal(0))
            held = s.balances.get((bot_account(bot_id, venue, "reserved"), currency), Decimal(0))
        return Account(cash + held, held)

    def accounts(self) -> dict[AccountKey, Account]:
        with self._lock:
            keys: set[AccountKey] = set()
            for name, currency in self._current().balances:
                owner, _, rest = name.partition(":")
                if "@" in owner and rest in ("cash", "reserved"):
                    bot_id, venue = owner.split("@")
                    keys.add((bot_id, Venue(venue), currency))
            return {k: self.account(*k) for k in sorted(keys)}

    def hold(self, reference: str) -> Hold | None:
        with self._lock:
            return self._current().holds.get(reference)

    def holds(self) -> dict[str, Hold]:
        with self._lock:
            return dict(self._current().holds)

    def order(self, order_id: str) -> OrderRecord:
        with self._lock:
            record = self._current().orders.get(order_id)
        if record is None:
            raise LedgerError(f"unknown order {order_id!r}")
        return record

    def orders(self) -> dict[str, OrderRecord]:
        with self._lock:
            return dict(self._current().orders)

    def position(self, bot_id: str, instrument_id: InstrumentId) -> Position | None:
        with self._lock:
            return self._current().positions.get(_position_key(bot_id, instrument_id))

    def positions(self) -> dict[str, Position]:
        with self._lock:
            return dict(self._current().positions)

    def lots(self, bot_id: str, instrument_id: InstrumentId) -> tuple[Lot, ...]:
        with self._lock:
            return self._current().lots.get(_position_key(bot_id, instrument_id), ())

    def all_lots(self) -> dict[str, tuple[Lot, ...]]:
        with self._lock:
            return dict(self._current().lots)

    def bad_debt(self, bot_id: str, venue: Venue, currency: str) -> Decimal:
        with self._lock:
            key = f"{bot_id}@{venue.value}|{currency}"
            return self._current().bad_debt.get(key, Decimal(0))

    def liquidation(self, bot_id: str, instrument_id: InstrumentId) -> LiquidationProgress | None:
        with self._lock:
            return self._current().liquidations.get(_position_key(bot_id, instrument_id))

    def liquidations(self) -> dict[str, LiquidationProgress]:
        with self._lock:
            return dict(self._current().liquidations)

    def payables(self, bot_id: str, venue: Venue) -> dict[tuple[str, str], Decimal]:
        """Outstanding liabilities of one bot as POSITIVE amounts, by (account, currency)."""
        owner = f"{bot_id}@{venue.value}:payable"
        with self._lock:
            return {
                k: -v
                for k, v in self._current().balances.items()
                if k[0].startswith(owner + ":") and v != 0
            }

    def events(self) -> list[LedgerEvent]:
        return self._store.events()

    def state_digest(self) -> str:
        with self._lock:
            s = self._current()
            state = {
                "balances": sorted(
                    (a, c, canonical(v)) for (a, c), v in s.balances.items() if v != 0
                ),
                "holds": {k: _dump(v) for k, v in s.holds.items()},
                "orders": {k: _dump(v) for k, v in s.orders.items()},
                "positions": {k: _dump(v) for k, v in s.positions.items()},
                "lots": {k: _dump(v) for k, v in s.lots.items()},
                "bad_debt": dict(s.bad_debt.items()),
            }
            if s.liquidations:  # absent when empty: M6-A digests are unchanged
                state["liquidations"] = {k: _dump(v) for k, v in s.liquidations.items()}
        return hashlib.sha256(canonical_json(state).encode()).hexdigest()

    # -- execution machinery -------------------------------------------------------------------

    def _run(self, op_id: str, kind: str, payload: dict[str, Any]) -> Any:
        if op_id.startswith("live:"):  # LTR-03: the live ledger's namespace
            raise LedgerError(f"operation id {op_id!r} is in the live ledger namespace")
        body = _jsonable(payload)
        with self._lock:
            existing = self._store.operation_digest(op_id)
            if existing is not None:
                if existing != LedgerStore.digest_for(kind, body):
                    raise CapitalError(f"operation id {op_id!r} conflict: different content")
                return (self._store.operation_effects(op_id) or {}).get("result")
            with self._store.unit_of_work() as uow:
                draft = self._current().copy()
                plan = self._planner(kind)(draft, body)
                self._check_non_negative(draft, plan.entries)
                effects = draft.effects()
                effects["result"] = _jsonable(plan.result)
                uow.add_operation(
                    op_id, kind, body, effects, [e.rows() for e in plan.entries], plan.transitions
                )
                if id(self) not in uow.drafts:
                    uow.on_commit(lambda: self._commit(uow.drafts.pop(id(self))))
                uow.drafts[id(self)] = draft
            return effects["result"]

    def _commit(self, draft: LedgerState) -> None:
        draft.touched = {}
        self._state = draft

    @staticmethod
    def _check_non_negative(state: LedgerState, entries: Iterable[JournalEntry]) -> None:
        for entry in entries:
            for p in entry.postings:
                value = state.balances.get((p.account, p.currency), Decimal(0))
                if account_kind(p.account) in NON_NEGATIVE_KINDS and value < 0:
                    raise InsufficientCapitalError(
                        f"{p.account} would be negative ({value} {p.currency})"
                    )
                if account_kind(p.account) in NON_POSITIVE_KINDS and value > 0:
                    raise LedgerError(f"liability {p.account} would be positive ({value})")

    def _planner(self, kind: str) -> Callable[[LedgerState, dict[str, Any]], _Plan]:
        planner = getattr(self, f"_plan_{kind}", None)
        if planner is None:
            raise LedgerError(f"unknown operation kind {kind!r}")
        return planner  # type: ignore[no-any-return]

    @staticmethod
    def _post(state: LedgerState, plan: _Plan, entry: JournalEntry | None) -> None:
        if entry is not None:
            apply_entry(state.balances, entry)
            plan.entries.append(entry)

    def _restore(self, state: LedgerState, op: StoredOperation) -> None:
        for entry in op.entries:
            for account, currency, amount in entry:
                key = (account, currency)
                state.balances[key] = state.balances.get(key, Decimal(0)) + amount
        state.apply_effects(op.effects)

    def verify_replay(self) -> list[str]:
        """Re-plan every stored operation from its payload; return all differences."""
        mismatches: list[str] = []
        state = LedgerState()
        for op in self._store.operations():
            draft = state.copy()
            try:
                plan = self._planner(op.kind)(draft, op.payload)
            except Exception as exc:
                mismatches.append(f"{op.op_id}: replay raised {type(exc).__name__}: {exc}")
                self._restore(state, op)
                continue
            effects = draft.effects()
            effects["result"] = _jsonable(plan.result)
            planned = [tuple((a, c, canonical(v)) for a, c, v in e.rows()) for e in plan.entries]
            stored = [tuple((a, c, canonical(v)) for a, c, v in e) for e in op.entries]
            if planned != stored:
                mismatches.append(f"{op.op_id}: postings differ")
            if _jsonable(effects) != op.effects:
                mismatches.append(f"{op.op_id}: effects differ")
            planned_t = [
                (t.order_id, t.n, t.from_state, t.to_state, canonical(t.filled_qty))
                for t in plan.transitions
            ]
            stored_t = [
                (t.order_id, t.n, t.from_state, t.to_state, canonical(t.filled_qty))
                for t in op.transitions
            ]
            if planned_t != stored_t:
                mismatches.append(f"{op.op_id}: transitions differ")
            self._restore(state, op)  # continue from what was actually committed
        if not mismatches and self.state_digest() != _digest_of(self, state):
            mismatches.append("projection differs from replayed state")
        return mismatches

    # -- capital operations (M5 API) ------------------------------------------------------------

    def deposit_paper_capital(self, op_id: str, eur: Decimal) -> None:
        if eur <= 0:
            raise CapitalError("deposit must be positive")
        self._run(op_id, "deposit", {"eur": eur})

    def _plan_deposit(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        eur = Decimal(p["eur"])
        self._post(
            s, plan, _entry("paper capital", (TREASURY, "EUR", eur), (PAPER_CAPITAL, "EUR", -eur))
        )
        return plan

    def allocate(
        self,
        op_id: str,
        bot_id: str,
        venue: Venue,
        currency: str,
        eur: Decimal,
        eur_per_unit: Decimal,
    ) -> None:
        """Move EUR from the treasury to a bot's cash at a recorded rate, net of cost."""
        if eur <= 0 or eur_per_unit <= 0:
            raise CapitalError("allocation and rate must be positive")
        self._run(
            op_id,
            "allocate",
            {
                "bot": bot_id,
                "venue": venue,
                "ccy": currency,
                "eur": eur,
                "rate": eur_per_unit,
                "cost": self._cost,
            },
        )

    def _plan_allocate(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        eur, rate, ccy = Decimal(p["eur"]), Decimal(p["rate"]), p["ccy"]
        treasury = s.balances.get((TREASURY, "EUR"), Decimal(0))
        if eur > treasury:
            raise InsufficientCapitalError(f"treasury {treasury} < {eur}")
        if treasury - eur < self._floor:
            raise InsufficientCapitalError(
                f"allocation would breach the liquidity reserve floor EUR {self._floor}"
            )
        cost = up(eur * Decimal(p["cost"]))
        cash = bot_account(p["bot"], Venue(p["venue"]), "cash")
        if ccy == "EUR":
            entry = _entry(
                "allocation",
                (cash, "EUR", eur - cost),
                (CONVERSION_COST, "EUR", cost),
                (TREASURY, "EUR", -eur),
            )
        else:
            amount = (eur - cost) / rate
            amount = amount.quantize(QUANTUM, ROUND_FLOOR)
            fx = portfolio_account("fx", f"EUR-{ccy}")
            entry = _entry(
                "allocation",
                (fx, "EUR", eur - cost),
                (CONVERSION_COST, "EUR", cost),
                (TREASURY, "EUR", -eur),
                (cash, ccy, amount),
                (fx, ccy, -amount),
            )
        self._post(s, plan, entry)
        return plan

    def commit(
        self, reference: str, bot_id: str, venue: Venue, currency: str, amount: Decimal
    ) -> None:
        """Hold free cash (e.g. approved initial margin) for one request. Rounded up."""
        if amount <= 0:
            raise CapitalError("commit amount must be positive")
        self._run(
            f"commit:{reference}",
            "commit",
            {
                "ref": reference,
                "bot": bot_id,
                "venue": venue,
                "ccy": currency,
                "amount": up(amount),
            },
        )

    def _plan_commit(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        venue, amount = Venue(p["venue"]), Decimal(p["amount"])
        self._post(
            s,
            plan,
            _entry(
                "hold",
                (bot_account(p["bot"], venue, "reserved"), p["ccy"], amount),
                (bot_account(p["bot"], venue, "cash"), p["ccy"], -amount),
            ),
        )
        s.put(
            "holds",
            p["ref"],
            Hold(
                reference=p["ref"], bot_id=p["bot"], venue=venue, currency=p["ccy"], amount=amount
            ),
        )
        return plan

    def release(self, reference: str) -> None:
        with self._lock:
            hold = self._current().holds.get(reference)
            if hold is None:
                raise CapitalError(f"unknown commitment {reference!r}")
            if hold.released:
                return  # idempotent
            self._run(f"release:{reference}", "release", {"ref": reference})

    def _plan_release(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        hold = s.holds.get(p["ref"])
        if hold is None or hold.released:
            raise CapitalError(f"commitment {p['ref']!r} unknown or already released")
        self._post(
            s,
            plan,
            _entry(
                "release",
                (bot_account(hold.bot_id, hold.venue, "cash"), hold.currency, hold.amount),
                (bot_account(hold.bot_id, hold.venue, "reserved"), hold.currency, -hold.amount),
            ),
        )
        s.put("holds", p["ref"], hold.model_copy(update={"released": True}))
        return plan

    def realize(
        self, op_id: str, bot_id: str, venue: Venue, currency: str, amount: Decimal
    ) -> None:
        """Book a synthetic realised price result (tests / M5 compatibility)."""
        if amount == 0:
            raise CapitalError("realised amount must be non-zero")
        amount = down(amount)  # gains rounded down, losses rounded up (away from the bot)
        self._run(
            op_id, "realize", {"bot": bot_id, "venue": venue, "ccy": currency, "amount": amount}
        )

    def _plan_realize(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        venue, amount = Venue(p["venue"]), Decimal(p["amount"])
        self._post(
            s,
            plan,
            _entry(
                "realised result",
                (bot_account(p["bot"], venue, "cash"), p["ccy"], amount),
                (bot_account(p["bot"], venue, "pnl", "realized_price"), p["ccy"], -amount),
            ),
        )
        return plan

    def transfer_to_reserve(
        self,
        op_id: str,
        debits: dict[AccountKey, Decimal],
        eur_per_unit: Decimal,
        credit_eur: Decimal | None = None,
    ) -> Decimal:
        """REFUSED: the suite has no automatic or discretionary profit reserve for any bot;
        profits stay in each bot's trading equity. Nothing is written. The signature is kept so
        a caller fails loudly instead of silently (safety control)."""
        raise CapitalError(
            f"{op_id}: discretionary profit reserves are not supported (paper-only policy); "
            "profits stay in each bot's trading equity"
        )

    def _plan_reserve(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        # REPLAY ONLY: books historical `reserve` operations of older ledgers, unchanged, so they
        # keep replaying. No production path creates new ones.
        plan = _Plan()
        rate, cost_rate = Decimal(p["rate"]), Decimal(p["cost"])
        postings: list[tuple[str, str, Decimal]] = []
        gross = Decimal(0)
        for bot_id, venue, ccy, amount_s in p["debits"]:
            amount = Decimal(amount_s)
            cash = bot_account(bot_id, Venue(venue), "cash")
            if ccy == "EUR":
                postings.append((cash, "EUR", -amount))
                gross += amount
            else:
                fx = portfolio_account("fx", f"EUR-{ccy}")
                eur = down(amount * rate)
                postings += [(fx, ccy, amount), (cash, ccy, -amount), (fx, "EUR", -eur)]
                gross += eur
        if p["credit"] is None:
            credited = down(gross * (1 - cost_rate))
        else:
            credited = Decimal(p["credit"])
            if gross * (1 - cost_rate) < credited:
                raise InsufficientCapitalError(
                    "debits do not cover the reserve plus conversion cost"
                )
        postings += [(RESERVE, "EUR", credited), (CONVERSION_COST, "EUR", gross - credited)]
        self._post(s, plan, _entry("profit reserve", *postings))
        plan.result = credited
        return plan

    # -- orders ---------------------------------------------------------------------------------

    def open_order(self, spec: OrderSpec) -> OrderRecord:
        self._run(f"ord:{spec.order_id}:open", "open_order", {"spec": _model_payload(spec)})
        return self.order(spec.order_id)

    @staticmethod
    def _requirement(spec: OrderSpec, remaining: Decimal) -> Decimal:
        """Reservation needed for `remaining` (quote/settle units; base units for spot sells)."""
        inst, price, fee = spec.instrument, spec.reserve_price, spec.reserve_fee_rate
        if remaining == 0:
            return Decimal(0)
        if inst.is_spot:
            return remaining if spec.side is Side.SELL else up(remaining * price * (1 + fee))
        notional = remaining * inst.contract_size * price
        if spec.reduce_only:
            return up(notional * fee)
        return up(notional / spec.leverage + notional * fee)

    def _plan_open_order(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        spec = OrderSpec.model_validate(p["spec"])
        inst, venue = spec.instrument, spec.instrument.instrument_id.venue
        if spec.order_id in s.orders:
            raise LedgerError(f"order {spec.order_id!r} already exists")
        pkey = _position_key(spec.bot_id, inst.instrument_id)
        position = s.positions.get(pkey)
        if pkey in s.liquidations:
            raise LedgerError("the position is being liquidated: no new orders")
        if not inst.is_spot:
            if spec.reduce_only:
                if position is None or position.side is spec.side:
                    raise LedgerError("reduce-only order has no opposite position to reduce")
                if spec.quantity > position.quantity - position.pending_reduce:
                    raise LedgerError("reduce-only quantity exceeds the unreduced position")
                s.put(
                    "positions",
                    pkey,
                    position.model_copy(
                        update={"pending_reduce": position.pending_reduce + spec.quantity}
                    ),
                )
            else:
                if position is not None and position.side is not spec.side:
                    raise LedgerError("order is opposite to the open position: use reduce-only")
                for other in s.orders.values():
                    o = other.spec
                    if (
                        other.state in OPEN_STATES
                        and o.bot_id == spec.bot_id
                        and o.instrument.instrument_id == inst.instrument_id
                        and not o.reduce_only
                        and o.side is not spec.side
                    ):
                        raise LedgerError("an opposite increasing order is already open")
        need = self._requirement(spec, spec.quantity)
        if inst.is_spot and spec.side is Side.SELL:
            ccy = inst.base
            entry = _entry(
                "inventory hold",
                (bot_account(spec.bot_id, venue, "spot_reserved"), ccy, need),
                (bot_account(spec.bot_id, venue, "spot"), ccy, -need),
            )
        else:
            ccy = inst.quote if inst.is_spot else inst.settle_currency
            entry = _entry(
                "order hold",
                (bot_account(spec.bot_id, venue, "reserved"), ccy, need),
                (bot_account(spec.bot_id, venue, "cash"), ccy, -need),
            )
        self._post(s, plan, entry)
        s.put(
            "orders",
            spec.order_id,
            OrderRecord(
                spec=spec,
                state=OrderState.ACCEPTED,
                filled_qty=Decimal(0),
                reserved=need,
                reserved_currency=ccy,
                transitions=1,
            ),
        )
        plan.transitions.append(
            TransitionRow(
                spec.order_id,
                1,
                OrderState.NEW.value,
                OrderState.ACCEPTED.value,
                Decimal(0),
                spec.created_at,
            )
        )
        return plan

    def _transition(
        self,
        s: LedgerState,
        plan: _Plan,
        record: OrderRecord,
        to: OrderState,
        ts: datetime,
        **update: Any,
    ) -> OrderRecord:
        n = record.transitions + (1 if to is not record.state else 0)
        new = record.model_copy(update={"state": to, "transitions": n, **update})
        if to is not record.state:
            plan.transitions.append(
                TransitionRow(
                    record.spec.order_id, n, record.state.value, to.value, new.filled_qty, ts
                )
            )
        s.put("orders", record.spec.order_id, new)
        return new

    def request_cancel(self, order_id: str, ts: datetime) -> OrderRecord:
        """Mark an order cancel-requested; fills may still arrive until it is closed."""
        record = self.order(order_id)
        if record.state in TERMINAL_STATES or record.state is OrderState.CANCEL_REQUESTED:
            return record  # no-op
        self._run(f"ord:{order_id}:cancel_request", "cancel_request", {"order": order_id, "ts": ts})
        return self.order(order_id)

    def _plan_cancel_request(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        record = s.orders[p["order"]]
        self._transition(s, plan, record, OrderState.CANCEL_REQUESTED, _ts(p["ts"]))
        return plan

    def close_order(self, order_id: str, final: OrderState, ts: datetime) -> OrderRecord:
        """Cancel or expire an open order and release its whole remaining reservation."""
        if final not in (OrderState.CANCELLED, OrderState.EXPIRED):
            raise LedgerError("orders close as cancelled or expired")
        if self.order(order_id).state in TERMINAL_STATES:
            return self.order(order_id)  # no-op
        self._run(
            f"ord:{order_id}:close", "close_order", {"order": order_id, "final": final, "ts": ts}
        )
        return self.order(order_id)

    def _plan_close_order(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        record = s.orders[p["order"]]
        if record.state in TERMINAL_STATES:
            raise LedgerError(f"order {record.spec.order_id!r} is terminal")
        self._release_reservation(s, plan, record, record.reserved)
        spec = record.spec
        if spec.reduce_only:
            self._unreserve_reduce(s, spec, record.remaining)
        self._transition(s, plan, record, OrderState(p["final"]), _ts(p["ts"]), reserved=Decimal(0))
        return plan

    def _release_reservation(
        self, s: LedgerState, plan: _Plan, record: OrderRecord, amount: Decimal
    ) -> None:
        """Release (amount > 0) or top up from free funds (amount < 0) an order's hold."""
        spec, venue = record.spec, record.spec.instrument.instrument_id.venue
        ccy = record.reserved_currency
        sell_spot = spec.instrument.is_spot and spec.side is Side.SELL
        free_acct = bot_account(spec.bot_id, venue, "spot" if sell_spot else "cash")
        held_acct = bot_account(spec.bot_id, venue, "spot_reserved" if sell_spot else "reserved")
        self._post(
            s,
            plan,
            _entry("reservation adjustment", (free_acct, ccy, amount), (held_acct, ccy, -amount)),
        )

    def _unreserve_reduce(self, s: LedgerState, spec: OrderSpec, quantity: Decimal) -> None:
        pkey = _position_key(spec.bot_id, spec.instrument.instrument_id)
        position = s.positions.get(pkey)
        if position is not None:
            s.put(
                "positions",
                pkey,
                position.model_copy(
                    update={"pending_reduce": max(Decimal(0), position.pending_reduce - quantity)}
                ),
            )

    # -- fills ----------------------------------------------------------------------------------

    def book_fill(self, fill: FillSpec) -> OrderRecord:
        self._run(f"fill:{fill.fill_id}", "fill", {"fill": _model_payload(fill)})
        return self.order(fill.order_id)

    def _plan_fill(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        fill = FillSpec.model_validate(p["fill"])
        record = s.orders.get(fill.order_id)
        if record is None:
            raise LedgerError(f"fill for unknown order {fill.order_id!r}")
        if record.state in TERMINAL_STATES:
            raise LedgerError(f"order {fill.order_id!r} is terminal ({record.state.value})")
        if fill.quantity > record.remaining:
            raise LedgerError(f"fill {fill.quantity} exceeds remaining {record.remaining}")
        spec, inst = record.spec, record.spec.instrument
        if _position_key(spec.bot_id, inst.instrument_id) in s.liquidations:
            raise LedgerError("the position is being liquidated: order fills are refused")
        if inst.is_spot:
            used = (self._spot_buy if spec.side is Side.BUY else self._spot_sell)(
                s, plan, spec, fill
            )
        elif spec.reduce_only:
            used = self._derivative_reduce(s, plan, spec, fill)
        else:
            used = self._derivative_increase(s, plan, spec, fill)
        remaining = record.remaining - fill.quantity
        need = self._requirement(spec, remaining)
        self._release_reservation(s, plan, record, record.reserved - used - need)
        state = (
            OrderState.FILLED
            if remaining == 0
            else OrderState.CANCEL_REQUESTED
            if record.state is OrderState.CANCEL_REQUESTED
            else OrderState.PARTIALLY_FILLED
        )
        self._transition(
            s,
            plan,
            record,
            state,
            fill.ts,
            filled_qty=record.filled_qty + fill.quantity,
            reserved=need,
        )
        return plan

    def _spot_buy(self, s: LedgerState, plan: _Plan, spec: OrderSpec, fill: FillSpec) -> Decimal:
        inst, venue, bot = spec.instrument, spec.instrument.instrument_id.venue, spec.bot_id
        cost = up(fill.price * fill.quantity)
        fee = up(cost * fill.fee_rate)
        fx = bot_account(bot, venue, "fx", f"{inst.base}-{inst.quote}")
        self._post(
            s,
            plan,
            _entry(
                f"spot buy {fill.fill_id}",
                (fx, inst.quote, cost),
                (bot_account(bot, venue, "expense", "trading_fee"), inst.quote, fee),
                (bot_account(bot, venue, "reserved"), inst.quote, -(cost + fee)),
                (bot_account(bot, venue, "spot"), inst.base, fill.quantity),
                (fx, inst.base, -fill.quantity),
            ),
        )
        key = _position_key(bot, inst.instrument_id)
        s.put("lots", key, (*s.lots.get(key, ()), Lot(quantity=fill.quantity, cost=cost)))
        return cost + fee

    def _spot_sell(self, s: LedgerState, plan: _Plan, spec: OrderSpec, fill: FillSpec) -> Decimal:
        inst, venue, bot = spec.instrument, spec.instrument.instrument_id.venue, spec.bot_id
        proceeds = down(fill.price * fill.quantity)
        fee = up(proceeds * fill.fee_rate)
        fx = bot_account(bot, venue, "fx", f"{inst.base}-{inst.quote}")
        cash = bot_account(bot, venue, "cash")
        key = _position_key(bot, inst.instrument_id)
        lots, basis, left = list(s.lots.get(key, ())), Decimal(0), fill.quantity
        while left > 0:
            if not lots:
                raise LedgerError("FIFO lots do not cover the sold quantity")
            lot = lots[0]
            take = min(left, lot.quantity)
            part = lot.cost if take == lot.quantity else down(lot.cost * take / lot.quantity)
            basis += part
            left -= take
            if take == lot.quantity:
                lots.pop(0)
            else:
                lots[0] = Lot(quantity=lot.quantity - take, cost=lot.cost - part)
        s.put("lots", key, tuple(lots) or None)
        realized = proceeds - basis
        pnl = bot_account(bot, venue, "pnl", "realized_price")
        self._post(
            s,
            plan,
            _entry(
                f"spot sell {fill.fill_id}",
                (fx, inst.base, fill.quantity),
                (bot_account(bot, venue, "spot_reserved"), inst.base, -fill.quantity),
                (cash, inst.quote, proceeds - fee),
                (fx, inst.quote, -proceeds),
                (bot_account(bot, venue, "expense", "trading_fee"), inst.quote, fee),
                (fx, inst.quote, realized),
                (pnl, inst.quote, -realized),
            ),
        )
        return fill.quantity

    def _derivative_increase(
        self, s: LedgerState, plan: _Plan, spec: OrderSpec, fill: FillSpec
    ) -> Decimal:
        inst, venue, bot = spec.instrument, spec.instrument.instrument_id.venue, spec.bot_id
        ccy = inst.settle_currency
        notional = fill.price * fill.quantity * inst.contract_size
        margin, fee = up(notional / spec.leverage), up(notional * fill.fee_rate)
        self._post(
            s,
            plan,
            _entry(
                f"open {fill.fill_id}",
                (bot_account(bot, venue, "margin", inst.instrument_id.symbol), ccy, margin),
                (bot_account(bot, venue, "expense", "trading_fee"), ccy, fee),
                (bot_account(bot, venue, "reserved"), ccy, -(margin + fee)),
            ),
        )
        key = _position_key(bot, inst.instrument_id)
        pos = s.positions.get(key)
        if pos is None:
            pos = Position(
                bot_id=bot,
                instrument=inst,
                side=spec.side,
                quantity=fill.quantity,
                avg_entry=fill.price,
            )
        else:
            total = pos.quantity + fill.quantity
            avg = (pos.avg_entry * pos.quantity + fill.price * fill.quantity) / total
            pos = pos.model_copy(update={"quantity": total, "avg_entry": avg})
        s.put("positions", key, pos)
        return margin + fee

    def _realise(
        self,
        s: LedgerState,
        plan: _Plan,
        pos: Position,
        price: Decimal,
        quantity: Decimal,
        memo: str,
    ) -> None:
        """Realise price PnL of `quantity` and return its share of margin (isolated)."""
        inst, venue, bot = pos.instrument, pos.instrument.instrument_id.venue, pos.bot_id
        ccy = inst.settle_currency
        direction = 1 if pos.side is Side.BUY else -1
        pnl = (price - pos.avg_entry) * quantity * inst.contract_size * direction
        margin_acct = bot_account(bot, venue, "margin", inst.instrument_id.symbol)
        pnl_acct = bot_account(bot, venue, "pnl", "realized_price")
        cash = bot_account(bot, venue, "cash")
        margin = s.balances.get((margin_acct, ccy), Decimal(0))
        if pnl < 0:
            loss = up(-pnl)
            if loss > margin:
                raise LedgerError("loss exceeds the isolated margin: settle as a liquidation")
            self._post(
                s, plan, _entry(f"{memo} loss", (pnl_acct, ccy, loss), (margin_acct, ccy, -loss))
            )
            margin -= loss
        released = margin if quantity == pos.quantity else down(margin * quantity / pos.quantity)
        gain = down(pnl) if pnl > 0 else Decimal(0)
        self._post(
            s,
            plan,
            _entry(
                f"{memo} settle",
                (cash, ccy, released + gain),
                (margin_acct, ccy, -released),
                (pnl_acct, ccy, -gain),
            ),
        )
        key = _position_key(bot, inst.instrument_id)
        left = pos.quantity - quantity
        s.put("positions", key, None if left == 0 else pos.model_copy(update={"quantity": left}))

    def _derivative_reduce(
        self, s: LedgerState, plan: _Plan, spec: OrderSpec, fill: FillSpec
    ) -> Decimal:
        inst, venue, bot = spec.instrument, spec.instrument.instrument_id.venue, spec.bot_id
        key = _position_key(bot, inst.instrument_id)
        pos = s.positions.get(key)
        if pos is None or pos.side is spec.side or fill.quantity > pos.quantity:
            raise LedgerError("reduce fill exceeds the open position")
        pos = pos.model_copy(
            update={"pending_reduce": max(Decimal(0), pos.pending_reduce - fill.quantity)}
        )
        s.put("positions", key, pos)
        self._realise(s, plan, pos, fill.price, fill.quantity, f"reduce {fill.fill_id}")
        ccy = inst.settle_currency
        fee = up(fill.price * fill.quantity * inst.contract_size * fill.fee_rate)
        self._post(
            s,
            plan,
            _entry(
                f"fee {fill.fill_id}",
                (bot_account(bot, venue, "expense", "trading_fee"), ccy, fee),
                (bot_account(bot, venue, "reserved"), ccy, -fee),
            ),
        )
        return fee

    # -- funding and liquidation ----------------------------------------------------------------

    def book_funding(
        self,
        op_id: str,
        bot_id: str,
        instrument_id: InstrumentId,
        rate: Decimal,
        mark: Decimal,
        ts: datetime,
    ) -> None:
        """Settle one funding interval for a position (long pays when rate > 0)."""
        self._run(
            op_id,
            "funding",
            {
                "bot": bot_id,
                "instrument": instrument_id.model_dump(),
                "rate": rate,
                "mark": mark,
                "ts": ts,
            },
        )

    def _plan_funding(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        iid = InstrumentId.model_validate(p["instrument"])
        pos = s.positions.get(_position_key(p["bot"], iid))
        if pos is None:
            raise LedgerError("funding for a position that does not exist")
        rate, mark = Decimal(p["rate"]), Decimal(p["mark"])
        raw = pos.quantity * pos.instrument.contract_size * mark * abs(rate)
        ccy, venue = pos.instrument.settle_currency, iid.venue
        cash = bot_account(pos.bot_id, venue, "cash")
        pays = (rate > 0) == (pos.side is Side.BUY)
        if rate == 0 or raw == 0:
            return plan
        if pays:
            amount = up(raw)
            free = s.balances.get((cash, ccy), Decimal(0))
            from_cash = min(amount, max(free, Decimal(0)))
            margin_acct = bot_account(pos.bot_id, venue, "margin", iid.symbol)
            margin = max(s.balances.get((margin_acct, ccy), Decimal(0)), Decimal(0))
            from_margin = min(amount - from_cash, margin)
            unpaid = amount - from_cash - from_margin  # B5: stays an outstanding liability
            self._post(
                s,
                plan,
                _entry(
                    "funding paid",
                    (bot_account(pos.bot_id, venue, "funding", "paid"), ccy, amount),
                    (cash, ccy, -from_cash),
                    (margin_acct, ccy, -from_margin),
                    (_payable(pos.bot_id, venue, iid.symbol), ccy, -unpaid),
                ),
            )
            if unpaid:  # result only on the B5 path: M6-A funding effects are unchanged
                plan.result = {"unpaid": unpaid}
        else:
            amount = down(raw)
            self._post(
                s,
                plan,
                _entry(
                    "funding received",
                    (cash, ccy, amount),
                    (bot_account(pos.bot_id, venue, "funding", "received"), ccy, -amount),
                ),
            )
        return plan

    def settle_liquidation(
        self,
        op_id: str,
        bot_id: str,
        instrument_id: InstrumentId,
        executions: list[LiquidationExecution],
        fee_rate: Decimal,
        trigger: LiquidationTrigger,
    ) -> LiquidationReport:
        """Stages 4-5: book the simulated close-out of a WHOLE position with fee and settlement."""
        result = self._run(
            op_id,
            "liquidation",
            {
                "bot": bot_id,
                "instrument": instrument_id.model_dump(),
                "executions": [_model_payload(e) for e in executions],
                "fee_rate": fee_rate,
                "trigger": _model_payload(trigger),
            },
        )
        return LiquidationReport.model_validate(result)

    def _plan_liquidation(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        iid = InstrumentId.model_validate(p["instrument"])
        key = _position_key(p["bot"], iid)
        pos = s.positions.get(key)
        if pos is None:
            raise LedgerError("liquidation of a position that does not exist")
        if pos.pending_reduce > 0:
            raise LedgerError("close open reduce-only orders before settling a liquidation")
        if key in s.liquidations:
            raise LedgerError("a per-fill liquidation is in progress for this position")
        execs = [LiquidationExecution.model_validate(e) for e in p["executions"]]
        qty = sum((e.quantity for e in execs), Decimal(0))
        if qty != pos.quantity:
            raise LedgerError("liquidation executions must close the whole position")
        inst, venue, bot, ccy = (
            pos.instrument,
            iid.venue,
            pos.bot_id,
            pos.instrument.settle_currency,
        )
        direction = 1 if pos.side is Side.BUY else -1
        pnl = sum(
            (
                (e.price - pos.avg_entry) * e.quantity * inst.contract_size * direction
                for e in execs
            ),
            Decimal(0),
        )
        notional = sum((e.price * e.quantity * inst.contract_size for e in execs), Decimal(0))
        fee = up(notional * Decimal(p["fee_rate"]))
        margin_acct = bot_account(bot, venue, "margin", iid.symbol)
        insurance = bot_account(bot, venue, "sim", "insurance_absorption")
        pnl_acct = bot_account(bot, venue, "pnl", "realized_price")
        cash = bot_account(bot, venue, "cash")
        margin_before = margin = s.balances.get((margin_acct, ccy), Decimal(0))
        loss = up(-pnl) if pnl < 0 else Decimal(0)
        gain = down(pnl) if pnl > 0 else Decimal(0)
        from_margin = min(loss, margin)
        self._post(
            s,
            plan,
            _entry(
                "liquidation execution loss",
                (pnl_acct, ccy, loss),
                (margin_acct, ccy, -from_margin),
                (insurance, ccy, -(loss - from_margin)),
            ),
        )
        self._post(
            s, plan, _entry("liquidation execution gain", (cash, ccy, gain), (pnl_acct, ccy, -gain))
        )
        margin -= from_margin
        fee_from_margin = min(fee, margin)
        self._post(
            s,
            plan,
            _entry(
                "liquidation fee",
                (bot_account(bot, venue, "expense", "liquidation_fee"), ccy, fee),
                (margin_acct, ccy, -fee_from_margin),
                (insurance, ccy, -(fee - fee_from_margin)),
            ),
        )
        margin -= fee_from_margin
        self._post(
            s,
            plan,
            _entry("liquidation settlement", (cash, ccy, margin), (margin_acct, ccy, -margin)),
        )
        absorbed = (loss - from_margin) + (fee - fee_from_margin)
        debt_key = f"{bot}@{venue.value}|{ccy}"
        if absorbed:
            s.put("bad_debt", debt_key, s.bad_debt.get(debt_key, Decimal(0)) + absorbed)
        s.put("positions", key, None)
        plan.result = LiquidationReport(
            trigger=LiquidationTrigger.model_validate(p["trigger"]),
            executed_quantity=qty,
            average_price=notional / (qty * inst.contract_size),
            execution_loss=loss,
            execution_gain=gain,
            liquidation_fee=fee,
            margin_before=margin_before,
            margin_returned=margin,
            insurance_absorbed=absorbed,
        ).model_dump()
        return plan

    # -- outstanding liabilities (B5) -----------------------------------------------------------

    def settle_payable(self, op_id: str, bot_id: str, venue: Venue, currency: str) -> Decimal:
        """Pay outstanding liabilities of ONE bot from that bot's own free cash (never another
        bot's, never the treasury). Returns the amount paid."""
        return Decimal(
            self._run(op_id, "settle_payable", {"bot": bot_id, "venue": venue, "ccy": currency})
        )

    def _plan_settle_payable(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        venue, ccy = Venue(p["venue"]), p["ccy"]
        cash = bot_account(p["bot"], venue, "cash")
        owner = bot_account(p["bot"], venue, "payable") + ":"
        left = s.balances.get((cash, ccy), Decimal(0))
        due = sorted(
            (acct, -bal)
            for (acct, c), bal in s.balances.items()
            if c == ccy and acct.startswith(owner) and bal < 0
        )
        if not due or left <= 0:
            raise LedgerError("nothing to settle: no outstanding liability or no free cash")
        paid = Decimal(0)
        for acct, outstanding in due:
            amount = min(outstanding, left)
            if amount <= 0:
                break
            self._post(
                s, plan, _entry("payable settled", (acct, ccy, amount), (cash, ccy, -amount))
            )
            left -= amount
            paid += amount
        plan.result = paid
        return plan

    # -- per-fill liquidation (B3) ----------------------------------------------------------------

    def begin_liquidation(
        self, op_id: str, bot_id: str, instrument_id: InstrumentId, trigger: LiquidationTrigger
    ) -> LiquidationProgress:
        """Stage 2: lock the position for liquidation (no postings; margin stays locked)."""
        self._run(
            op_id,
            "liquidation_begin",
            {
                "bot": bot_id,
                "instrument": instrument_id.model_dump(),
                "trigger": _model_payload(trigger),
            },
        )
        progress = self.liquidation(bot_id, instrument_id)
        if progress is None:
            raise LedgerError("liquidation was not recorded")
        return progress

    def _plan_liquidation_begin(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        iid = InstrumentId.model_validate(p["instrument"])
        key = _position_key(p["bot"], iid)
        pos = s.positions.get(key)
        if pos is None or pos.instrument.is_spot:
            raise LedgerError("liquidation of a derivative position that does not exist")
        if key in s.liquidations:
            raise LedgerError("the position is already being liquidated")
        if pos.pending_reduce > 0:
            raise LedgerError("close open reduce-only orders before liquidating")
        margin_acct = bot_account(pos.bot_id, iid.venue, "margin", iid.symbol)
        s.put(
            "liquidations",
            key,
            LiquidationProgress(
                bot_id=pos.bot_id,
                instrument=pos.instrument,
                side=pos.side,
                avg_entry=pos.avg_entry,
                start_quantity=pos.quantity,
                trigger=LiquidationTrigger.model_validate(p["trigger"]),
                margin_before=s.balances.get(
                    (margin_acct, pos.instrument.settle_currency), Decimal(0)
                ),
            ),
        )
        return _Plan()

    def book_liquidation_fill(
        self,
        op_id: str,
        bot_id: str,
        instrument_id: InstrumentId,
        execution: LiquidationExecution,
        fee_rate: Decimal,
    ) -> LiquidationProgress | None:
        """Stage 3-5 for ONE executed fill: realised PnL, liquidation fee and any insurance
        absorption, booked exactly once (idempotent by `op_id`). Returns the progress, or None
        once the position is flat (then `finish_liquidation` must follow)."""
        self._run(
            op_id,
            "liquidation_fill",
            {
                "bot": bot_id,
                "instrument": instrument_id.model_dump(),
                "execution": _model_payload(execution),
                "fee_rate": fee_rate,
            },
        )
        progress = self.liquidation(bot_id, instrument_id)
        return progress if self.position(bot_id, instrument_id) is not None else None

    def _charge_pooled(
        self,
        s: LedgerState,
        plan: _Plan,
        prog: LiquidationProgress,
        debit: str,
        amount: Decimal,
        memo: str,
    ) -> Decimal:
        """Charge `amount` to the locked margin; any excess is absorbed (simulated). Returns
        the absorbed part."""
        inst = prog.instrument
        venue, ccy = inst.instrument_id.venue, inst.settle_currency
        margin_acct = bot_account(prog.bot_id, venue, "margin", inst.instrument_id.symbol)
        insurance = bot_account(prog.bot_id, venue, "sim", "insurance_absorption")
        from_margin = min(amount, max(s.balances.get((margin_acct, ccy), Decimal(0)), Decimal(0)))
        self._post(
            s,
            plan,
            _entry(
                memo,
                (debit, ccy, amount),
                (margin_acct, ccy, -from_margin),
                (insurance, ccy, -(amount - from_margin)),
            ),
        )
        return amount - from_margin

    def _plan_liquidation_fill(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        iid = InstrumentId.model_validate(p["instrument"])
        key = _position_key(p["bot"], iid)
        prog, pos = s.liquidations.get(key), s.positions.get(key)
        if prog is None or pos is None:
            raise LedgerError("liquidation fill without a liquidation in progress")
        ex = LiquidationExecution.model_validate(p["execution"])
        if ex.quantity > pos.quantity:
            raise LedgerError(f"liquidation fill {ex.quantity} exceeds position {pos.quantity}")
        inst, venue, bot = prog.instrument, iid.venue, prog.bot_id
        ccy = inst.settle_currency
        direction = 1 if prog.side is Side.BUY else -1
        raw = (ex.price - prog.avg_entry) * ex.quantity * inst.contract_size * direction
        pnl_acct = bot_account(bot, venue, "pnl", "realized_price")
        margin_acct = bot_account(bot, venue, "margin", iid.symbol)
        insurance = bot_account(bot, venue, "sim", "insurance_absorption")
        absorbed = prog.absorbed
        if raw < 0:
            loss = up(-raw)
            absorbed += self._charge_pooled(s, plan, prog, pnl_acct, loss, "liquidation fill loss")
            pnl = -loss
        else:
            gain = down(raw)
            repay = min(gain, absorbed)  # a gain first reverses this liquidation's absorption
            self._post(
                s,
                plan,
                _entry(
                    "liquidation fill gain",
                    (insurance, ccy, repay),
                    (margin_acct, ccy, gain - repay),  # stays locked until flat
                    (pnl_acct, ccy, -gain),
                ),
            )
            absorbed -= repay
            pnl = gain
        notional = ex.price * ex.quantity * inst.contract_size
        fee = up(notional * Decimal(p["fee_rate"]))
        absorbed += self._charge_pooled(
            s,
            plan,
            prog,
            bot_account(bot, venue, "expense", "liquidation_fee"),
            fee,
            "liquidation fill fee",
        )
        debt_key = f"{bot}@{venue.value}|{ccy}"
        debt = s.bad_debt.get(debt_key, Decimal(0)) + absorbed - prog.absorbed
        if debt != s.bad_debt.get(debt_key, Decimal(0)):
            s.put("bad_debt", debt_key, debt if debt else None)
        left = pos.quantity - ex.quantity
        s.put("positions", key, None if left == 0 else pos.model_copy(update={"quantity": left}))
        s.put(
            "liquidations",
            key,
            prog.model_copy(
                update={
                    "executed_quantity": prog.executed_quantity + ex.quantity,
                    "notional": prog.notional + notional,
                    "pnl": prog.pnl + pnl,
                    "fees": prog.fees + fee,
                    "absorbed": absorbed,
                    "fills": prog.fills + 1,
                }
            ),
        )
        return plan

    def finish_liquidation(
        self, op_id: str, bot_id: str, instrument_id: InstrumentId
    ) -> LiquidationReport:
        """Final stage 5 once flat: return the remaining locked margin to cash. Books no PnL,
        fee or absorption (all were booked per fill), so nothing is counted twice."""
        result = self._run(
            op_id, "liquidation_finish", {"bot": bot_id, "instrument": instrument_id.model_dump()}
        )
        return LiquidationReport.model_validate(result)

    def _plan_liquidation_finish(self, s: LedgerState, p: dict[str, Any]) -> _Plan:
        plan = _Plan()
        iid = InstrumentId.model_validate(p["instrument"])
        key = _position_key(p["bot"], iid)
        prog = s.liquidations.get(key)
        if prog is None:
            raise LedgerError("no liquidation in progress")
        if key in s.positions:
            raise LedgerError("the position is not flat yet")
        inst, ccy = prog.instrument, prog.instrument.settle_currency
        margin_acct = bot_account(prog.bot_id, iid.venue, "margin", iid.symbol)
        margin = s.balances.get((margin_acct, ccy), Decimal(0))
        self._post(
            s,
            plan,
            _entry(
                "liquidation settlement",
                (bot_account(prog.bot_id, iid.venue, "cash"), ccy, margin),
                (margin_acct, ccy, -margin),
            ),
        )
        s.put("liquidations", key, None)
        plan.result = LiquidationReport(
            trigger=prog.trigger,
            executed_quantity=prog.executed_quantity,
            average_price=prog.notional / (prog.executed_quantity * inst.contract_size),
            execution_loss=max(-prog.pnl, Decimal(0)),
            execution_gain=max(prog.pnl, Decimal(0)),
            liquidation_fee=prog.fees,
            margin_before=prog.margin_before,
            margin_returned=margin,
            insurance_absorbed=prog.absorbed,
        ).model_dump()
        return plan


def _payable(bot_id: str, venue: Venue, symbol: str) -> str:
    return bot_account(bot_id, venue, "payable", f"funding:{symbol}")


_UTC = TypeAdapter(UtcDatetime)


def _ts(value: str) -> datetime:
    return _UTC.validate_python(value)


def _digest_of(ledger: TradingLedger, state: LedgerState) -> str:
    original = ledger._state
    try:
        ledger._state = state
        return ledger.state_digest()
    finally:
        ledger._state = original
