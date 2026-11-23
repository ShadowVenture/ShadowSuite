"""TEST-ONLY simulated external bots. They follow a fixed script; they are not strategies and
are never shipped in the package (they live under tests/). Each records every callback."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.core.orders import OrderType, Side
from shadow_trading_suite.plugins.contract import (
    Decision,
    MarketView,
    OrderRequest,
    OrderUpdate,
    PluginConfig,
    PluginConfigError,
    PluginManifest,
    Signal,
    SignalKind,
    StopReason,
)

BTC_USD = InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD")

Script = Callable[[int, MarketView], Decision]


def manifest(
    plugin_id: str = "test-bot", instruments: tuple[InstrumentId, ...] = (BTC_USD,)
) -> PluginManifest:
    return PluginManifest(
        plugin_id=plugin_id,
        name="Scripted test plugin",
        version="0.0.1",
        venue=instruments[0].venue,
        instruments=instruments,
        description="TEST ONLY: follows a fixed script",
    )


class ScriptedPlugin:
    def __init__(
        self,
        script: Script | None = None,
        *,
        plugin_id: str = "test-bot",
        instruments: tuple[InstrumentId, ...] = (BTC_USD,),
    ) -> None:
        self.manifest = manifest(plugin_id, instruments)
        self.script = script or (lambda n, view: Decision())
        self.calls: list[tuple[str, Any]] = []
        self.views: list[MarketView] = []
        self.updates: list[OrderUpdate] = []
        self.config: dict[str, Any] = {}
        self.n = 0

    def configure(self, config: PluginConfig) -> None:
        if config.get("refuse") is True:
            raise PluginConfigError("refused by test configuration")
        self.config = dict(config)
        self.calls.append(("configure", dict(config)))

    def on_start(self, now: datetime) -> None:
        self.calls.append(("on_start", now))

    def on_market(self, view: MarketView) -> Decision:
        self.views.append(view)
        self.n += 1
        self.calls.append(("on_market", self.n))
        return self.script(self.n, view)

    def on_order_update(self, update: OrderUpdate) -> None:
        self.updates.append(update)
        self.calls.append(("on_order_update", update.status.value))

    def on_stop(self, reason: StopReason) -> None:
        self.calls.append(("on_stop", reason.value))


class RaisingPlugin(ScriptedPlugin):
    def on_market(self, view: MarketView) -> Decision:
        super().on_market(view)
        raise RuntimeError("simulated plugin bug")


def buy_once(
    quantity: str = "0.001",
    order_type: OrderType = OrderType.MARKET,
    limit: str | None = None,
    request_id: str = "r1",
    at_call: int = 1,
) -> Script:
    def script(n: int, view: MarketView) -> Decision:
        if n != at_call:
            return Decision()
        return Decision(
            signals=(
                Signal(
                    instrument_id=BTC_USD,
                    kind=SignalKind.BULLISH,
                    strength=Decimal("0.5"),
                    note="scripted",
                ),
            ),
            orders=(
                OrderRequest(
                    request_id=request_id,
                    instrument_id=BTC_USD,
                    side=Side.BUY,
                    quantity=Decimal(quantity),
                    order_type=order_type,
                    limit_price=Decimal(limit) if limit else None,
                ),
            ),
        )

    return script


def create_buyer() -> ScriptedPlugin:
    """Factory used by the CLI test (`factory = "tests.fixtures.plugins.scripted:create_buyer"`).
    Buys once on its 105th market callback (after the scenario's volatility warm-up)."""
    return ScriptedPlugin(buy_once(at_call=105))
