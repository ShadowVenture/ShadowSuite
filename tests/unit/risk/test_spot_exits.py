"""Risk rules v2 (M7-A): a spot SELL only ever reduces exposure.

Spot is never leveraged or short; the ledger reserves the sold base inventory, so a spot SELL
can never create exposure. Under v2 it is treated like a derivatives reduce-only order by the
halt, loss, drawdown and emergency gates and by the portfolio aggregates, and it is capped at
the bot's held quantity instead of its margin limits. A spot BUY is unchanged. Decisions record
their rules version; v1 records replay with v1 rules (exactly the M5 behaviour).
"""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from shadow_trading_suite.core.orders import Side
from shadow_trading_suite.risk.engine import (
    RULES_VERSION,
    OpenPosition,
    RiskVerdict,
    evaluate_bot,
)
from shadow_trading_suite.risk.supervisor import PortfolioEquity, PortfolioState, review_portfolio
from tests.unit.risk.test_engine import (
    CB_SPOT,
    CC_SPOT,
    CFG,
    PERP,
    failed,
    market,
    proposal,
    state,
)
from tests.unit.risk.test_supervisor_recovery import request

D = Decimal


def holding(qty: str, inst=CC_SPOT, bot: str = "bot-03") -> OpenPosition:  # type: ignore[no-untyped-def]
    return OpenPosition(
        position_id=f"{bot}:{inst.id}",
        bot_id=bot,
        instrument_id=inst.id,
        underlying=inst.base,
        side=Side.BUY,
        quantity=D(qty),
        notional_eur=D(qty) * D(82000) * D("0.86"),
        margin_eur=D(qty) * D(82000) * D("0.86"),
        leverage=D(1),
    )


def sell(qty: str, inst=CC_SPOT, bot: str = "bot-03", **kw):  # type: ignore[no-untyped-def]
    return proposal(inst, bot, side=Side.SELL, quantity=D(qty), leverage=D(1), **kw)


def test_rules_version_is_two() -> None:
    assert RULES_VERSION == 2


class TestSpotSellRulesV2:
    def test_a_spot_sell_is_capped_at_the_held_quantity(self) -> None:
        st_ = state("bot-03", open_positions=(holding("0.003"),))
        d = evaluate_bot(sell("0.005"), CC_SPOT, st_, market(CC_SPOT), CFG)
        assert d.verdict is RiskVerdict.RESIZE
        assert d.approved_quantity == D("0.003")

    def test_a_spot_sell_without_holdings_is_rejected(self) -> None:
        d = evaluate_bot(sell("0.001"), CC_SPOT, state("bot-03"), market(CC_SPOT), CFG)
        assert d.verdict is RiskVerdict.REJECT
        assert "holding" in failed(d)

    def test_other_instruments_holdings_do_not_count(self) -> None:
        st_ = state("bot-01", open_positions=(holding("0.003", CB_SPOT, "bot-01"),))
        d = evaluate_bot(sell("0.001", CC_SPOT, "bot-01"), CC_SPOT, st_, market(CC_SPOT), CFG)
        assert d.verdict is RiskVerdict.REJECT  # permission and/or holding

    def test_a_holding_of_another_spot_instrument_cannot_be_sold_as_this_one(self) -> None:
        eth = CC_SPOT.model_copy(
            update={"id": CC_SPOT.id.model_copy(update={"symbol": "ETH_USD"}), "base": "ETH"}
        )
        st_ = state("bot-03", open_positions=(holding("0.5", eth),))
        d = evaluate_bot(sell("0.001"), CC_SPOT, st_, market(CC_SPOT), CFG)
        assert d.verdict is RiskVerdict.REJECT
        assert "holding" in failed(d)

    def test_halt_daily_loss_and_drawdown_never_block_a_spot_exit(self) -> None:
        eq = CFG.bot("bot-03").allocation_eur
        st_ = state(
            "bot-03",
            open_positions=(holding("0.003"),),
            halted=True,
            equity_eur=eq / 2,  # far beyond the daily loss and drawdown limits
        )
        d = evaluate_bot(sell("0.003"), CC_SPOT, st_, market(CC_SPOT), CFG)
        assert d.verdict is RiskVerdict.APPROVE, d.reasons

    def test_the_same_spot_buy_is_still_blocked(self) -> None:
        eq = CFG.bot("bot-03").allocation_eur
        st_ = state("bot-03", open_positions=(holding("0.003"),), halted=True, equity_eur=eq / 2)
        buy = proposal(CC_SPOT, "bot-03", quantity=D("0.001"), leverage=D(1))
        d = evaluate_bot(buy, CC_SPOT, st_, market(CC_SPOT), CFG)
        assert d.verdict is RiskVerdict.REJECT
        assert {"halt", "daily_loss", "drawdown"} <= failed(d)

    def test_v1_rules_are_the_unchanged_m5_behaviour(self) -> None:
        st_ = state("bot-03", open_positions=(holding("0.003"),), halted=True)
        d = evaluate_bot(sell("0.003"), CC_SPOT, st_, market(CC_SPOT), CFG, rules=1)
        assert d.verdict is RiskVerdict.REJECT
        assert "halt" in failed(d)

    def test_derivatives_are_unchanged(self) -> None:
        p = proposal(PERP, "bot-03", side=Side.SELL)
        assert evaluate_bot(p, PERP, state(), market(PERP), CFG) == evaluate_bot(
            p, PERP, state(), market(PERP), CFG, rules=1
        )


@given(
    held=st.decimals(min_value=D("0.00001"), max_value=D(1), places=5),
    asked=st.decimals(min_value=D("0.00001"), max_value=D(2), places=5),
    halted=st.booleans(),
)
def test_a_spot_sell_never_exceeds_holdings(held: Decimal, asked: Decimal, halted: bool) -> None:
    st_ = state("bot-03", open_positions=(holding(str(held)),), halted=halted)
    d = evaluate_bot(sell(str(asked)), CC_SPOT, st_, market(CC_SPOT), CFG)
    if d.approved_quantity is not None:
        assert d.approved_quantity <= held
        assert d.approved_quantity <= asked
        assert d.approved_quantity % CC_SPOT.lot_size == 0


class TestSupervisor:
    def _state(self, emergency: bool, equity: str = "5000") -> PortfolioState:
        return PortfolioState(
            equity=PortfolioEquity(
                equity_eur=D(equity), start_of_day_eur=D(5000), peak_eur=D(5000)
            ),
            emergency=emergency,
            exposures=(),
        )

    def test_an_exposure_reducing_request_passes_the_emergency(self) -> None:
        r = request("x", "bot-03", Side.SELL).model_copy(update={"reduces_exposure": True})
        decision, _, _ = review_portfolio(r, self._state(True), 0, CFG)
        assert decision.approved, decision.reasons

    def test_an_increasing_request_is_still_refused_in_an_emergency(self) -> None:
        r = request("x", "bot-03", Side.SELL)
        decision, _, _ = review_portfolio(r, self._state(True), 0, CFG)
        assert not decision.approved
        assert any(x.startswith("emergency") for x in decision.reasons)

    def test_an_exposure_reducing_request_skips_the_opposing_rule(self) -> None:
        own = request("own", "bot-01", Side.BUY)
        from shadow_trading_suite.risk.supervisor import Exposure

        exposure = Exposure(
            request_id="own",
            bot_id="bot-01",
            instrument_id=own.instrument_id,
            underlying=own.terms.underlying,
            venue=own.terms.venue,
            side=Side.BUY,
            notional_eur=own.terms.notional_eur,
            margin_eur=own.terms.margin_eur,
            stress=own.terms.stress,
        )
        state_ = self._state(False).model_copy(update={"exposures": (exposure,)})
        r = request("x", "bot-01", Side.SELL)
        refused, _, _ = review_portfolio(r, state_, 0, CFG)
        assert not refused.approved  # unchanged v1 semantics for a non-reducing request
        reducing = r.model_copy(update={"reduces_exposure": True})
        ok, _, _ = review_portfolio(reducing, state_, 0, CFG)
        assert ok.approved, ok.reasons


class TestCapitalFree:
    def test_only_an_exposure_reducing_request_may_need_no_capital(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="exposure-reducing"):
            request("x", "bot-03", Side.BUY, margin="0")
        free = request("x", "bot-03", Side.SELL).model_copy(update={"reduces_exposure": True})
        data = free.model_dump()
        data["terms"]["margin_quote"] = D(0)
        data["terms"]["margin_eur"] = D(0)
        assert type(free).model_validate(data).capital_free
        data["reduces_exposure"] = False
        with pytest.raises(ValidationError, match="exposure-reducing"):
            type(free).model_validate(data)
