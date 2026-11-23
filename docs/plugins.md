# Plugin contract (plugin API version 1)

A **bot** is an external plugin. The suite ships no bot and loads none automatically. This
document is the contract between your bot and the suite. The source of truth is
`src/shadow_trading_suite/plugins/contract.py`.

## The interface

```python
class BotPlugin(Protocol):
    manifest: PluginManifest

    def configure(self, config: PluginConfig) -> None: ...  # raise PluginConfigError to refuse
    def on_start(self, now: datetime) -> None: ...
    def on_market(self, view: MarketView) -> Decision: ...
    def on_order_update(self, update: OrderUpdate) -> None: ...
    def on_stop(self, reason: StopReason) -> None: ...
```

Any object with these members works; no base class is required (`typing.Protocol`, checked at
registration). All callbacks are **synchronous** and must return quickly. They run on the
host's single thread.

### Manifest (registration data)

| Field | Rule |
|---|---|
| `plugin_id` | `^[a-z0-9][a-z0-9_-]{0,63}$`, unique per host; `portfolio` is reserved. Must equal a `[[bots]] bot_id` in the portfolio. |
| `name` | 1-80 characters (shown in the UI, HTML-escaped) |
| `version` | semantic version, e.g. `1.2.0` |
| `api_version` | `1` |
| `venue` | `coinbase` or `cryptocom` |
| `instruments` | 1-32 distinct `InstrumentId`s on that venue; SPOT only in this preview |
| `description` | optional, up to 500 characters |

### Configuration

The suite configuration passes a flat mapping of `str`, `int`, `bool` or exact `Decimal`
values (floats are refused) to `configure` once, before `on_start`. If `configure` raises, the
plugin becomes FAILED and is never started.

```toml
[[plugins]]
factory = "my_bot.plugin:create"   # a zero-argument callable returning the plugin
# entry_point = "my-bot"           # alternative: entry point in group shadow_trading_suite.plugins
[plugins.config]
threshold = "0.5"                  # strings for decimals
window = 20
```

### Market data: `MarketView`

`on_market` is called after book or trade updates for the plugin's own instruments. The view
is read-only and contains only that plugin's data:

- `quotes[str(instrument_id)]`: best bid/ask from a **fresh, LIVE, two-sided** book. A missing
  key means no usable quote. Treat that as "do nothing" and never guess a price.
- `last_trade_price`, `holdings` (paper inventory), `open_orders` (own orders only), `now`.

### Signals and order requests: `Decision`

```python
Decision(
    signals=(
        Signal(instrument_id=..., kind=SignalKind.BULLISH, strength=Decimal("0.4"), note="..."),
    ),
    orders=(
        OrderRequest(
            request_id="r-17",
            instrument_id=...,
            side=Side.BUY,
            quantity=Decimal("0.001"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("80000"),
        ),
    ),
    cancel_order_ids=("...",),  # own order ids only
)
```

- **Signals** are informational. They are recorded in the status (UI, JSON API) and never
  executed.
- **Order requests** are paper orders. `request_id` must be unique per plugin. Market orders are
  IOC by default; limit orders are GTC (post-only orders must rest).
- At most 16 orders and 16 cancellations per decision.

### Order updates: `OrderUpdate`

Every outcome reaches `on_order_update`. The statuses are `rejected` (with `reason`),
`accepted` (with the accepted `quantity`), `partially_filled`, `filled`, `cancel_requested`,
`cancelled` and `expired`. `submit()` also returns the first update synchronously.

### Stop and errors

- `on_stop(reason)` is called exactly once, with one of three reasons. `requested` means an
  operator stop (`PaperHost.stop_plugin`) or an emergency stop. `shutdown` means the host shut
  down. `error` means the plugin raised.
- **Any exception** from a callback, or a wrong return type from `on_market`, moves the plugin
  to **FAILED**. The host requests the cancellation of its resting paper orders, calls
  `on_stop(error)` (errors raised there are suppressed) and never calls the plugin again. Other
  plugins continue.
- A stopped or failed plugin can never submit again.

## Order path (central controls)

Every `OrderRequest` passes the following stages, in order. The first refusal ends the request.

1. **Host checks:**
   - the plugin is RUNNING;
   - the instrument is in its manifest;
   - for a BUY, the portfolio is not in emergency shutdown;
   - paper capital has been bootstrapped from a live EUR rate;
   - the order terms are consistent (for example, no limit price on a market order).
2. **Risk inputs from the ledger:** economic equity, durable 00:00 UTC day-start and peak
   equity, the live triangulated EUR rate and minute-close volatility. If any input is
   missing or stale, the request is refused ("risk inputs unavailable"). A spot SELL needs no
   volatility estimate.
3. **Risk engine and portfolio supervisor:** per-bot limits (position margin, total margin,
   leverage, liquidation distance, daily loss, drawdown, open positions) and portfolio limits
   (gross exposure, concentration, stress loss, emergency thresholds) and the order rate
   (`max_orders_per_minute`). A repeated `request_id` is refused as a duplicate. The engine may
   **reduce** the quantity (RESIZE) and never increases it. A spot SELL above the holding is
   refused.
4. **Paper execution engine:** validates the order terms, reserves paper collateral and books
   the order in the ledger (one atomic unit of work, audited). Fills happen on later market
   events, after a simulated latency, only on fresh LIVE books, within the displayed depth and
   a price band.

There is **no stage after 4**. Nothing is ever sent to an exchange.

## Lifecycle

```
register (manifest validated, risk profile + instruments admitted)
  -> configure(config)            FAILED on error
  -> on_start(now)                when the host starts (or immediately if it already runs)
  -> on_market / on_order_update  RUNNING
  -> on_stop(reason)              STOPPED (requested / shutdown) or FAILED (error)
```

Admission refuses a plugin that has no `[[bots]]` risk profile, a venue different from its
profile, an instrument unknown to the host, a non-spot instrument, or the reserved id.

## Registering

- **Configuration file:** `[[plugins]] factory = "pkg.module:create"` or
  `entry_point = "name"` (group `shadow_trading_suite.plugins`). `sts plugins` lists installed
  entry points but does not import them.
- **Code:** `PaperHost.add_plugin(plugin, config)`.

## Testing your bot

Use deterministic offline scenarios (`sts simulate --scenario ... --config ...`) or drive a
`PaperHost` with a `ManualClock` as in `tests/plugins/support.py`. Unit tests run with network
sockets disabled.

## Versioning

`PLUGIN_API_VERSION = 1`. An incompatible change will introduce a new API version. A manifest
declares the version it was written for.
