"""Valuation of the paper ledger: ledger vs ECONOMIC equity (R1), unrealised PnL, EUR.

Ledger equity follows the simulated isolated-margin model (a bot never loses more than its
posted margin). Economic equity additionally deducts losses that the simulated insurance fund
absorbed — the conservative figure used for risk reporting and risk-limit enforcement.
Equity per currency = cash + reserved + margin + derivative unrealised PnL + spot inventory at
mark - outstanding liabilities (`payable:*`, e.g. unpaid simulated funding, B5; counted once,
in both views). Cost-basis `fx:*` accounts are trading accounts, not assets, so nothing is
counted twice. Missing marks or EUR rates fail closed.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from decimal import Decimal

from shadow_trading_suite.accounting.journal import bot_account
from shadow_trading_suite.accounting.trading import InstrumentTerms, TradingLedger
from shadow_trading_suite.core.errors import DataQualityError
from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.model import DomainModel
from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.risk.engine import BotState, OpenPosition
from shadow_trading_suite.risk.supervisor import PortfolioEquity

_FIELDS = (
    "cash",
    "reserved",
    "margin",
    "inventory_value",
    "unrealized_pnl",
    "realized_price_pnl",
    "trading_fees",
    "liquidation_fees",
    "funding_paid",
    "funding_received",
    "insurance_absorbed",
    "payables",
)


class CurrencyValuation(DomainModel):
    currency: str
    cash: Decimal
    reserved: Decimal
    margin: Decimal
    inventory_value: Decimal  # spot holdings at mark, in this (quote) currency
    unrealized_pnl: Decimal  # derivatives at mark + spot inventory vs FIFO cost (info only)
    realized_price_pnl: Decimal  # fee- and funding-exclusive
    trading_fees: Decimal
    liquidation_fees: Decimal
    funding_paid: Decimal
    funding_received: Decimal
    insurance_absorbed: Decimal  # SIMULATED recovery; never income
    payables: Decimal = Decimal(0)  # outstanding internal liabilities (positive amount)
    ledger_equity: Decimal
    economic_equity: Decimal


class BotValuation(DomainModel):
    bot_id: str
    venue: Venue
    currencies: tuple[CurrencyValuation, ...]
    ledger_equity_eur: Decimal
    insurance_absorbed_eur: Decimal
    economic_equity_eur: Decimal

    def currency(self, code: str) -> CurrencyValuation:
        for c in self.currencies:
            if c.currency == code:
                return c
        raise KeyError(code)


def _rate(rates: Mapping[str, Decimal], ccy: str) -> Decimal:
    if ccy == "EUR":
        return Decimal(1)
    rate = rates.get(ccy)
    if rate is None or rate <= 0:
        raise DataQualityError(f"no valid EUR rate for {ccy}")
    return rate


def _mark(marks: Mapping[InstrumentId, Decimal], iid: InstrumentId) -> Decimal:
    mark = marks.get(iid)
    if mark is None or mark <= 0:
        raise DataQualityError(f"no valid mark for {iid}")
    return mark


def value_bot(
    ledger: TradingLedger,
    bot_id: str,
    venue: Venue,
    marks: Mapping[InstrumentId, Decimal],
    eur_rates: Mapping[str, Decimal],
) -> BotValuation:
    owner = f"{bot_id}@{venue.value}"
    v: dict[str, dict[str, Decimal]] = defaultdict(lambda: dict.fromkeys(_FIELDS, Decimal(0)))
    units: dict[str, Decimal] = defaultdict(Decimal)
    derivative_upnl: dict[str, Decimal] = defaultdict(Decimal)
    for (account, ccy), bal in ledger.balances().items():
        parts = account.split(":")
        if parts[0] != owner:
            continue
        kind = (
            ":".join(parts[1:3]) if parts[1] in ("pnl", "expense", "funding", "sim") else parts[1]
        )
        if kind in ("cash", "reserved", "margin"):
            v[ccy][kind] += bal
        elif kind in ("spot", "spot_reserved"):
            units[ccy] += bal
        elif kind == "pnl:realized_price":
            v[ccy]["realized_price_pnl"] -= bal
        elif kind == "expense:trading_fee":
            v[ccy]["trading_fees"] += bal
        elif kind == "expense:liquidation_fee":
            v[ccy]["liquidation_fees"] += bal
        elif kind == "funding:paid":
            v[ccy]["funding_paid"] += bal
        elif kind == "funding:received":
            v[ccy]["funding_received"] -= bal
        elif kind == "sim:insurance_absorption":
            v[ccy]["insurance_absorbed"] -= bal
        elif kind == "payable":
            v[ccy]["payables"] -= bal  # credit balance -> positive outstanding amount
    for pos in ledger.positions().values():
        if pos.bot_id != bot_id or pos.instrument.instrument_id.venue is not venue:
            continue
        mark = _mark(marks, pos.instrument.instrument_id)
        sign = 1 if pos.side is Side.BUY else -1
        pnl = (mark - pos.avg_entry) * pos.quantity * pos.instrument.contract_size * sign
        v[pos.instrument.settle_currency]["unrealized_pnl"] += pnl
        derivative_upnl[pos.instrument.settle_currency] += pnl
    terms = _spot_terms(ledger, bot_id)
    for key, lots in ledger.all_lots().items():
        if not key.startswith(f"{bot_id}|{venue.value}:"):
            continue
        inst = terms.get(key)
        if inst is None:
            raise DataQualityError(f"no instrument terms for lots {key}")
        held = units.pop(inst.base, Decimal(0))
        value = held * _mark(marks, inst.instrument_id)
        v[inst.quote]["inventory_value"] += value
        v[inst.quote]["unrealized_pnl"] += value - sum((lot.cost for lot in lots), Decimal(0))
    if any(units.values()):
        raise DataQualityError(f"spot units without lots for {bot_id}: {dict(units)}")
    currencies, ledger_eur, absorbed_eur = [], Decimal(0), Decimal(0)
    for ccy in sorted(v):
        f = v[ccy]
        equity = (
            f["cash"]
            + f["reserved"]
            + f["margin"]
            + derivative_upnl[ccy]
            + f["inventory_value"]
            - f["payables"]
        )
        currencies.append(
            CurrencyValuation(
                currency=ccy,
                **f,
                ledger_equity=equity,
                economic_equity=equity - f["insurance_absorbed"],
            )
        )
        rate = _rate(eur_rates, ccy)
        ledger_eur += equity * rate
        absorbed_eur += f["insurance_absorbed"] * rate
    return BotValuation(
        bot_id=bot_id,
        venue=venue,
        currencies=tuple(currencies),
        ledger_equity_eur=ledger_eur,
        insurance_absorbed_eur=absorbed_eur,
        economic_equity_eur=ledger_eur - absorbed_eur,
    )


def _spot_terms(ledger: TradingLedger, bot_id: str) -> dict[str, InstrumentTerms]:
    terms = {}
    for record in ledger.orders().values():
        inst = record.spec.instrument
        if record.spec.bot_id == bot_id and inst.is_spot:
            terms[f"{bot_id}|{inst.key}"] = inst
    return terms


def portfolio_risk_equity(
    ledger: TradingLedger,
    bots: Iterable[tuple[str, Venue]],
    marks: Mapping[InstrumentId, Decimal],
    eur_rates: Mapping[str, Decimal],
    *,
    start_of_day_eur: Decimal,
    peak_eur: Decimal,
) -> PortfolioEquity:
    """Portfolio equity for risk limits: treasury + ECONOMIC bot equity (reserve excluded)."""
    total = ledger.treasury_eur
    for bot_id, venue in bots:
        total += value_bot(ledger, bot_id, venue, marks, eur_rates).economic_equity_eur
    return PortfolioEquity(equity_eur=total, start_of_day_eur=start_of_day_eur, peak_eur=peak_eur)


def bot_risk_state(
    ledger: TradingLedger,
    bot_id: str,
    venue: Venue,
    marks: Mapping[InstrumentId, Decimal],
    eur_rates: Mapping[str, Decimal],
    *,
    start_of_day_eur: Decimal,
    peak_eur: Decimal,
    orders_last_minute: int,
    halted: bool,
) -> BotState:
    """M5 `BotState` derived from the ledger: ECONOMIC equity (R1) and ledger positions.

    Spot holdings count as fully funded (margin = notional, leverage 1)."""
    valuation = value_bot(ledger, bot_id, venue, marks, eur_rates)
    positions: list[OpenPosition] = []
    for key, pos in sorted(ledger.positions().items()):
        inst = pos.instrument
        if pos.bot_id != bot_id or inst.instrument_id.venue is not venue:
            continue
        rate = _rate(eur_rates, inst.settle_currency)
        notional = pos.quantity * inst.contract_size * _mark(marks, inst.instrument_id) * rate
        margin = (
            ledger.balance(
                bot_account(bot_id, venue, "margin", inst.instrument_id.symbol),
                inst.settle_currency,
            )
            * rate
        )
        positions.append(
            OpenPosition(
                position_id=key,
                bot_id=bot_id,
                instrument_id=inst.instrument_id,
                underlying=inst.base,
                side=pos.side,
                quantity=pos.quantity,
                notional_eur=notional,
                margin_eur=margin,
                leverage=max(Decimal(1), notional / margin) if margin else Decimal(1),
            )
        )
    terms = _spot_terms(ledger, bot_id)
    for key, lots in sorted(ledger.all_lots().items()):
        spot = terms.get(key)
        if spot is None or spot.instrument_id.venue is not venue:
            continue
        qty = sum((lot.quantity for lot in lots), Decimal(0))
        notional = qty * _mark(marks, spot.instrument_id) * _rate(eur_rates, spot.quote)
        positions.append(
            OpenPosition(
                position_id=key,
                bot_id=bot_id,
                instrument_id=spot.instrument_id,
                underlying=spot.base,
                side=Side.BUY,
                quantity=qty,
                notional_eur=notional,
                margin_eur=notional,
                leverage=Decimal(1),
            )
        )
    return BotState(
        bot_id=bot_id,
        equity_eur=valuation.economic_equity_eur,
        start_of_day_equity_eur=start_of_day_eur,
        peak_equity_eur=peak_eur,
        open_positions=tuple(positions),
        orders_last_minute=orders_last_minute,
        halted=halted,
    )
