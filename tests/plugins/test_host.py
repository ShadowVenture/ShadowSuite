"""The paper host with external (test-only, scripted) plugins: empty start, registration,
signals, simulated execution, risk refusals, errors and stopping. No network, no real order."""

from decimal import Decimal
from pathlib import Path

import pytest

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.orders import OrderType, Side
from shadow_trading_suite.host.paper import HostError
from shadow_trading_suite.observability.status import read_all_status
from shadow_trading_suite.plugins.contract import (
    Decision,
    MarketView,
    OrderRequest,
    OrderStatus,
)
from shadow_trading_suite.plugins.registry import PluginError, PluginState
from tests.fixtures.plugins.scripted import BTC_USD, RaisingPlugin, ScriptedPlugin, buy_once
from tests.plugins.support import make_host

D = Decimal


def _statuses(plugin: ScriptedPlugin) -> list[str]:
    return [u.status.value for u in plugin.updates]


# -- empty state ----------------------------------------------------------------------------


def test_the_host_starts_with_no_plugin_and_reports_an_empty_state(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path)
    host.start()
    feed.fx()
    status = host.status()
    assert status["plugins"] == []
    assert status["paper_only"] is True
    assert status["live_trading"] == "not implemented"
    assert status["open_orders"] == 0
    assert len(host.registry) == 0
    docs = read_all_status(tmp_path / "data" / "status")
    assert docs["plugins"].payload == {"plugins": []}  # type: ignore[union-attr]
    host.shutdown()
    assert read_all_status(tmp_path / "data" / "status")["runtime"].payload["stopped_at"]  # type: ignore[union-attr]


def test_no_plugin_is_loaded_automatically(tmp_path: Path) -> None:
    from shadow_trading_suite.plugins.registry import installed_entry_points

    host, _ = make_host(tmp_path, "test-bot")  # a risk profile alone loads nothing
    host.start()
    assert len(host.registry) == 0
    assert installed_entry_points() == []
    host.shutdown()


# -- registration -----------------------------------------------------------------------------


def test_a_plugin_without_a_risk_profile_is_refused_and_not_registered(tmp_path: Path) -> None:
    host, _ = make_host(tmp_path)  # portfolio without bots
    with pytest.raises(HostError, match="no \\[\\[bots\\]\\] risk profile"):
        host.add_plugin(ScriptedPlugin())
    assert len(host.registry) == 0


def test_unknown_or_unadmitted_instruments_are_refused(tmp_path: Path) -> None:
    host, _ = make_host(tmp_path, "test-bot")
    eth = InstrumentId(venue=Venue.COINBASE, symbol="ETH-USD")
    with pytest.raises(HostError, match="not available in this host"):
        host.add_plugin(ScriptedPlugin(instruments=(eth,)))
    assert len(host.registry) == 0


def test_an_object_that_is_not_a_plugin_and_duplicate_ids_are_refused(tmp_path: Path) -> None:
    host, _ = make_host(tmp_path, "test-bot")
    with pytest.raises(PluginError, match="does not implement"):
        host.add_plugin(object())
    host.add_plugin(ScriptedPlugin())
    with pytest.raises(PluginError, match="already registered"):
        host.add_plugin(ScriptedPlugin())


def test_the_reserved_portfolio_id_is_refused(tmp_path: Path) -> None:
    host, _ = make_host(tmp_path, "portfolio")
    with pytest.raises(HostError, match="reserved"):
        host.add_plugin(ScriptedPlugin(plugin_id="portfolio"))


def test_a_refused_configuration_fails_the_plugin_and_it_never_starts(tmp_path: Path) -> None:
    host, _ = make_host(tmp_path, "test-bot")
    plugin = ScriptedPlugin()
    entry = host.add_plugin(plugin, {"refuse": True})
    host.start()
    assert entry.state is PluginState.FAILED
    assert "refused by test configuration" in str(entry.error)
    assert [c[0] for c in plugin.calls] == []  # neither started nor stopped


def test_configuration_reaches_the_plugin_and_it_starts(tmp_path: Path) -> None:
    host, _ = make_host(tmp_path, "test-bot")
    plugin = ScriptedPlugin()
    entry = host.add_plugin(plugin, {"threshold": "5", "enabled": True})
    host.start()
    assert plugin.config == {"threshold": "5", "enabled": True}
    assert entry.state is PluginState.RUNNING
    assert [c[0] for c in plugin.calls] == ["configure", "on_start"]


# -- signals and simulated execution ----------------------------------------------------------


def test_a_signal_and_a_market_buy_are_simulated_through_risk_and_execution(
    tmp_path: Path,
) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    feed.ready()
    feed.warm()
    plugin = ScriptedPlugin(buy_once("0.001"))
    entry = host.add_plugin(plugin)
    feed.ready()  # first on_market: the scripted BUY
    assert _statuses(plugin)[0] == "accepted", plugin.updates
    assert entry.signals
    assert entry.signals[0]["kind"] == "bullish"
    for _ in range(3):  # the fill happens on a later book after the simulated latency
        feed.advance(2)
        feed.ready()
    assert "filled" in _statuses(plugin), plugin.updates
    held = sum(lot.quantity for lot in host.ledger.lots("test-bot", BTC_USD))
    assert held == D("0.001")
    view = plugin.views[-1]
    assert view.holdings[0].quantity == D("0.001")
    assert entry.counters["accepted"] == 1
    host.shutdown()


def test_the_view_shows_only_fresh_quotes(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    plugin = ScriptedPlugin()
    host.add_plugin(plugin)
    feed.book("81999", "82001")
    assert plugin.views[-1].quotes[str(BTC_USD)].ask == D(82001)
    feed.advance(60)  # the book is now older than the spot freshness bound
    feed.trade(D(82000))
    assert str(BTC_USD) not in plugin.views[-1].quotes  # never a stale quote
    host.shutdown()


# -- refusals by the central controls ----------------------------------------------------------


def test_an_oversized_order_is_reduced_by_the_risk_engine_never_increased(
    tmp_path: Path,
) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    feed.ready()
    feed.warm()
    plugin = ScriptedPlugin(buy_once("1"))  # ~70k EUR against a 1000 EUR paper allocation
    host.add_plugin(plugin)
    feed.ready()
    update = plugin.updates[0]
    assert update.status is OrderStatus.ACCEPTED
    assert update.quantity is not None
    assert D(0) < update.quantity < D(1)  # capped to the bot's max position margin
    (record,) = host.ledger.orders().values()
    assert record.spec.quantity == update.quantity
    host.shutdown()


def test_a_spot_sell_without_holdings_is_rejected_by_the_risk_engine(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    feed.ready()
    feed.warm()
    plugin = ScriptedPlugin()
    host.add_plugin(plugin)
    feed.ready()
    update = host.submit(
        "test-bot",
        OrderRequest(
            request_id="s1",
            instrument_id=BTC_USD,
            side=Side.SELL,
            quantity=D("0.001"),
            order_type=OrderType.MARKET,
        ),
    )
    assert update.status is OrderStatus.REJECTED
    assert update.reason.startswith("risk engine:")
    assert plugin.updates[-1] == update  # the plugin is told
    assert host.ledger.orders() == {}
    assert host.registry.get("test-bot").counters["rejected"] == 1
    host.shutdown()


def test_inconsistent_order_terms_are_rejected_before_risk(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    feed.ready()
    feed.warm()
    host.add_plugin(ScriptedPlugin())
    feed.ready()
    update = host.submit(
        "test-bot",
        OrderRequest(
            request_id="m1",
            instrument_id=BTC_USD,
            side=Side.BUY,
            quantity=D("0.001"),
            order_type=OrderType.MARKET,
            limit_price=D(80000),
        ),
    )
    assert update.status is OrderStatus.REJECTED
    assert "invalid order terms" in update.reason
    host.shutdown()


def test_without_a_volatility_estimate_a_buy_fails_closed(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    feed.ready()
    plugin = ScriptedPlugin(buy_once("0.001"))
    host.add_plugin(plugin)
    feed.ready()
    assert plugin.updates[0].status is OrderStatus.REJECTED
    assert "risk inputs unavailable" in plugin.updates[0].reason
    host.shutdown()


def test_without_a_live_eur_rate_nothing_is_bootstrapped_and_buys_are_refused(
    tmp_path: Path,
) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    plugin = ScriptedPlugin(buy_once("0.001"))
    host.add_plugin(plugin)
    feed.book()
    assert not host.bootstrapped()
    assert plugin.updates[0].status is OrderStatus.REJECTED
    assert "not bootstrapped" in plugin.updates[0].reason
    host.shutdown()


def test_an_instrument_outside_the_manifest_is_refused_by_the_host(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    feed.ready()
    eth = InstrumentId(venue=Venue.COINBASE, symbol="ETH-USD")

    def script(n: int, view: MarketView) -> Decision:
        return Decision(
            orders=(
                OrderRequest(
                    request_id=f"x{n}",
                    instrument_id=eth,
                    side=Side.BUY,
                    quantity=D("0.01"),
                    order_type=OrderType.LIMIT,
                    limit_price=D(100),
                ),
            )
        )

    plugin = ScriptedPlugin(script)
    host.add_plugin(plugin)
    feed.book()
    assert plugin.updates[0].status is OrderStatus.REJECTED
    assert "not in the plugin's manifest" in plugin.updates[0].reason
    host.shutdown()


def test_after_an_emergency_stop_buys_are_refused_and_plugins_are_stopped(
    tmp_path: Path,
) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    feed.ready()
    feed.warm()
    plugin = ScriptedPlugin()
    entry = host.add_plugin(plugin)
    host.emergency_stop("operator test")
    assert entry.state is PluginState.STOPPED
    assert host.supervisor.emergency_shutdown
    update = host.submit(
        "test-bot",
        OrderRequest(
            request_id="late",
            instrument_id=BTC_USD,
            side=Side.BUY,
            quantity=D("0.001"),
            order_type=OrderType.MARKET,
        ),
    )
    assert update.status is OrderStatus.REJECTED
    host.shutdown()


# -- errors and stopping -------------------------------------------------------------------------


def test_a_plugin_error_fails_only_that_plugin_and_cancels_its_resting_orders(
    tmp_path: Path,
) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    feed.ready()
    feed.warm()
    good = ScriptedPlugin(buy_once("0.001", OrderType.LIMIT, limit="80000"))
    entry = host.add_plugin(good)
    feed.ready()
    feed.advance(2)
    feed.ready()
    assert _statuses(good)[0] == "accepted"

    # the same plugin then raises: replace its script with a failing one
    def boom(n: int, view: MarketView) -> Decision:
        raise RuntimeError("simulated plugin bug")

    good.script = boom
    feed.advance(1)
    feed.ready()
    assert entry.state is PluginState.FAILED
    assert "simulated plugin bug" in str(entry.error)
    assert ("on_stop", "error") in good.calls
    calls = len(good.calls)
    feed.advance(1)
    feed.ready()
    assert len(good.calls) == calls  # never called again
    for _ in range(3):
        feed.advance(2)
        feed.ready()
    states = {r.state.value for r in host.ledger.orders().values()}
    assert states <= {"cancelled", "cancel_requested"}, states
    host.shutdown()


def test_a_raising_plugin_is_isolated_from_other_plugins(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path, "test-bot", "other-bot")
    host.start()
    bad = RaisingPlugin()
    ok = ScriptedPlugin(plugin_id="other-bot")
    host.add_plugin(bad)
    host.add_plugin(ok)
    feed.book()
    feed.book()
    assert host.registry.get("test-bot").state is PluginState.FAILED
    assert host.registry.get("other-bot").state is PluginState.RUNNING
    assert len(ok.views) == 2
    host.shutdown()


def test_a_wrong_return_type_fails_the_plugin(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    plugin = ScriptedPlugin(lambda n, v: {"orders": []})  # type: ignore[arg-type,return-value]
    entry = host.add_plugin(plugin)
    feed.book()
    assert entry.state is PluginState.FAILED
    assert "must return a Decision" in str(entry.error)
    host.shutdown()


def test_stopping_a_plugin_cancels_its_orders_and_it_never_submits_again(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    feed.ready()
    feed.warm()
    plugin = ScriptedPlugin(buy_once("0.001", OrderType.LIMIT, limit="80000"))
    entry = host.add_plugin(plugin)
    feed.ready()
    assert _statuses(plugin)[0] == "accepted"
    host.stop_plugin("test-bot")
    assert entry.state is PluginState.STOPPED
    assert plugin.calls[-1] == ("on_stop", "requested")
    host.stop_plugin("test-bot")  # idempotent
    assert [c for c in plugin.calls if c[0] == "on_stop"] == [("on_stop", "requested")]
    views = len(plugin.views)
    feed.advance(2)
    feed.ready()
    assert len(plugin.views) == views
    late = host.submit(
        "test-bot",
        OrderRequest(
            request_id="late",
            instrument_id=BTC_USD,
            side=Side.BUY,
            quantity=D("0.001"),
            order_type=OrderType.LIMIT,
            limit_price=D(80000),
        ),
    )
    assert late.status is OrderStatus.REJECTED
    assert "plugin is stopped" in late.reason
    for _ in range(3):
        feed.advance(2)
        feed.ready()
    assert {r.state.value for r in host.ledger.orders().values()} == {"cancelled"}
    host.shutdown()


def test_a_plugin_cannot_cancel_another_plugins_order(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path, "test-bot", "other-bot")
    host.start()
    feed.ready()
    feed.warm()
    owner = ScriptedPlugin(buy_once("0.001", OrderType.LIMIT, limit="80000"))
    host.add_plugin(owner)
    feed.ready()
    order_id = owner.updates[0].order_id
    assert order_id is not None

    def cancel_foreign(n: int, view: MarketView) -> Decision:
        return Decision(cancel_order_ids=(order_id,))

    intruder = ScriptedPlugin(cancel_foreign, plugin_id="other-bot")
    entry = host.add_plugin(intruder)
    feed.advance(2)
    feed.ready()
    assert entry.counters["errors"] >= 1
    assert host.ledger.orders()[order_id].state.value != "cancel_requested"
    host.shutdown()


def test_the_host_refuses_events_after_shutdown(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path)
    host.start()
    host.shutdown()
    with pytest.raises(HostError, match="not running"):
        feed.book()


def _limit(rid: str) -> OrderRequest:
    return OrderRequest(
        request_id=rid,
        instrument_id=BTC_USD,
        side=Side.BUY,
        quantity=D("0.0001"),
        order_type=OrderType.LIMIT,
        limit_price=D(70000),
    )


def test_a_repeated_request_id_is_refused_as_a_duplicate(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path, "test-bot")
    host.start()
    feed.ready()
    feed.warm()
    host.add_plugin(ScriptedPlugin())
    feed.ready()
    assert host.submit("test-bot", _limit("same")).status is OrderStatus.ACCEPTED
    again = host.submit("test-bot", _limit("same"))
    assert again.status is OrderStatus.REJECTED
    assert "duplicate" in again.reason
    assert len(host.ledger.orders()) == 1
    host.shutdown()


def test_the_order_rate_limit_of_the_risk_profile_is_enforced(tmp_path: Path) -> None:
    host, feed = make_host(tmp_path, "test-bot")  # max_orders_per_minute = 10
    host.start()
    feed.ready()
    feed.warm()
    host.add_plugin(ScriptedPlugin())
    feed.ready()
    results = [host.submit("test-bot", _limit(f"r{i}")).status for i in range(11)]
    assert results[:10] == [OrderStatus.ACCEPTED] * 10
    assert results[10] is OrderStatus.REJECTED
    host.shutdown()
