"""A minimal plugin SKELETON for documentation (docs/plugins.md). Not a strategy.

It is never loaded automatically. It only reports the current mid price as an INFO signal and
never requests an order. Copy it into your own package and replace `on_market` with your logic.

    [[plugins]]
    factory = "my_bot:create"            # after `pip install` of your package
"""

from datetime import datetime

from shadow_trading_suite.core.instrument import InstrumentId, Venue
from shadow_trading_suite.plugins.contract import (
    Decision,
    MarketView,
    OrderUpdate,
    PluginConfig,
    PluginConfigError,
    PluginManifest,
    Signal,
    SignalKind,
    StopReason,
)

BTC_USD = InstrumentId(venue=Venue.COINBASE, symbol="BTC-USD")


class SkeletonBot:
    manifest = PluginManifest(
        plugin_id="my-bot",  # needs a [[bots]] risk profile with bot_id = "my-bot"
        name="Skeleton bot",
        version="0.1.0",
        venue=Venue.COINBASE,
        instruments=(BTC_USD,),
        description="Documentation skeleton: reports the mid price, never orders.",
    )

    def __init__(self) -> None:
        self.note_prefix = "mid"

    def configure(self, config: PluginConfig) -> None:
        unknown = set(config) - {"note_prefix"}
        if unknown:
            raise PluginConfigError(f"unknown settings: {sorted(unknown)}")
        self.note_prefix = str(config.get("note_prefix", self.note_prefix))

    def on_start(self, now: datetime) -> None:
        pass

    def on_market(self, view: MarketView) -> Decision:
        quote = view.quotes.get(str(BTC_USD))
        if quote is None:  # no fresh live book: do nothing
            return Decision()
        mid = (quote.bid + quote.ask) / 2
        info = Signal(instrument_id=BTC_USD, kind=SignalKind.INFO, note=f"{self.note_prefix} {mid}")
        return Decision(signals=(info,))

    def on_order_update(self, update: OrderUpdate) -> None:
        pass

    def on_stop(self, reason: StopReason) -> None:
        pass


def create() -> SkeletonBot:
    return SkeletonBot()
