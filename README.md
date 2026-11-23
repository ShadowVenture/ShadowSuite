# Shadow Trading Suite

**An open-source, paper-trading-only platform for crypto trading bots, shipped without any bot.**
You bring your own bot as a plugin. The suite provides market-data adapters, a double-entry
paper ledger, central risk controls, a paper execution engine and a read-only local UI. Every
order request from a plugin passes through those central controls.

> **Status: 0.1.0.dev1, pre-alpha preview.** This is not a stable release.
>
> **Paper trading only.** The suite cannot place, amend or cancel a real exchange order. No such
> code path exists, it reads no credentials, and it has no wallet or fund-movement functions.
> Live trading is **not implemented** (see [docs/roadmap.md](docs/roadmap.md)). Nothing here is
> financial advice, and simulated results are not evidence of profitability.

Licence: **GPL-3.0-only** (see [LICENSE](LICENSE), [NOTICE](NOTICE) and
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)).

## What it does

```
            your bot (plugin)                       Shadow Trading Suite
  +------------------------------+   MarketView  +-----------------------------------------+
  | manifest, configure,         |<--------------| market state (public data, LIVE/stale)  |
  | on_market -> Decision        |               |                                         |
  |   signals, order requests ---+-------------->| host checks -> risk engine + portfolio  |
  | on_order_update, on_stop     |<-- updates ---| supervisor -> PAPER execution engine    |
  +------------------------------+               | -> double-entry paper ledger (DuckDB)   |
                                                 | status files -> read-only UI 127.0.0.1  |
                                                 +-----------------------------------------+
```

- The suite **starts without a bot**. The registry is empty, and the UI and CLI show an explicit
  empty state. No demo strategy is loaded.
- A bot is an external Python object that fulfils the plugin contract
  ([docs/plugins.md](docs/plugins.md)). You register it explicitly in the suite configuration or in
  code. Installed entry points are only listed, never loaded automatically.
- An order request goes through these stages in order:
  1. host checks;
  2. ledger-derived risk inputs (fail closed when missing or stale);
  3. the risk engine and portfolio supervisor;
  4. the paper execution engine.

  The plugin gets an `OrderUpdate` for every outcome. A refusal is never retried. The risk engine
  may reduce an order to the bot's limits but never increases it.

## Feature overview (honest status)

| Area | Status |
|---|---|
| Plugin contract (manifest, configuration, market view, signals, order requests, order updates, stop, errors) | **Implemented, tested** (offline) |
| Plugin registry (explicit registration, `module:factory` and entry points; empty by default) | **Implemented, tested** |
| Paper host: empty start, plugin admission, signal recording, central order path, error isolation, plugin stop, emergency stop, shutdown | **Implemented, tested** (deterministic offline scenarios) |
| Risk engine (per-bot limits, margin, liquidation distance, loss limits, order rate) and portfolio supervisor (exposure, concentration, stress, emergency shutdown); hash-chained risk audit with replay | **Implemented, tested** |
| Paper execution engine: latency, fills only on fresh LIVE books, within displayed depth and price band; maker queue model labelled ASSUMPTION; gap/restart cancellation | **Implemented, tested** |
| Double-entry paper ledger (DuckDB, owner-only files, balanced postings, replay verification) | **Implemented, tested** |
| EUR valuation from live triangulated public tickers; minute-close volatility; durable day-start/peak equity | **Implemented, tested** |
| Public REST adapters, Coinbase Advanced Trade and Crypto.com Exchange (public market data only, allowlisted paths, rate limits, retries) | **Implemented, tested offline on recorded public responses.** Not exercised against the live API in CI |
| Public WebSocket protocols, order books, reconnect and resync session | **Implemented, tested offline.** Not wired into the `sts` CLI yet |
| Offline scenario runner (`sts simulate`) | **Implemented, tested** |
| Read-only local web UI and JSON status API (`sts serve`, loopback only, GET/HEAD, strict CSP, no scripts) | **Implemented, tested** |
| Live public market-data mode for the host (`sts run` against real public streams) | **Not implemented** (roadmap) |
| Derivatives (perpetuals/futures) for plugins | **Not implemented** for plugins; plugins are admitted for SPOT only. The engine and risk layer model derivatives, but those paths are not exposed |
| Real (live) order execution, private APIs, API keys | **Not implemented, by design.** No order transport exists |
| Persistent kill switches, extra hard caps, historical backtesting store | **Not implemented** (roadmap) |
| Plugin sandboxing / process isolation | **Not implemented.** Plugins run in-process (see [SECURITY.md](SECURITY.md)) |

## Installation

Requirements: Linux (tested on Ubuntu, x86_64) and Python 3.12, 3.13 or 3.14. See
[docs/platforms.md](docs/platforms.md).

From a clone, with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/ShadowVenture/Shadow-Trading-Suite.git
cd Shadow-Trading-Suite
uv sync --frozen          # installs the locked dependencies into .venv
uv run pytest             # offline test suite (network sockets disabled)
```

From the preview wheel (a GitHub pre-release asset), with plain `pip`:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install shadow_trading_suite-0.1.0.dev1-py3-none-any.whl
sts version
```

## Start without bots

From a clone:

```bash
uv run sts version                                    # paper_only: true, live_trading: not implemented
uv run sts check --config config/suite.example.toml   # no plugins, no bots: ok
uv run sts simulate --scenario examples/scenarios/btc-usd-short.json --data-dir /tmp/sts-demo
uv run sts serve --status-dir /tmp/sts-demo/status    # http://127.0.0.1:8080/  -> "No bots installed."
```

`sts simulate` runs the paper host over an offline scenario (synthetic prices, recorded public
instrument metadata) and writes status files. `sts serve` shows them read-only.

## Your own bot

1. Implement the contract in your own package. You can start from
   [examples/plugin_skeleton/my_bot.py](examples/plugin_skeleton/my_bot.py), which only reports
   a price and never orders.
2. Add a `[[bots]]` risk profile with the same id to your portfolio file. A plugin without a
   risk profile is refused.
3. Register the plugin in your suite configuration (`[[plugins]] factory = "my_bot:create"`).
4. Run `sts check --config ...`, then `sts simulate ...`.

The full contract, lifecycle, order path and error rules are in [docs/plugins.md](docs/plugins.md).

## Documentation

- [docs/getting-started.md](docs/getting-started.md): installation, start without bots, scenarios, UI
- [docs/plugins.md](docs/plugins.md): the plugin contract (API version 1)
- [docs/paper-trading.md](docs/paper-trading.md): how orders are checked, simulated and booked
- [docs/security.md](docs/security.md): security limits, and what the suite never does
- [docs/platforms.md](docs/platforms.md): supported platforms
- [docs/limitations.md](docs/limitations.md): known limitations
- [docs/architecture.md](docs/architecture.md), [docs/venue-notes.md](docs/venue-notes.md), [docs/glossary.md](docs/glossary.md): layout, venue facts, internal design labels
- [docs/roadmap.md](docs/roadmap.md): missing features, including live functions
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [AGENTS.md](AGENTS.md), [CLAUDE.md](CLAUDE.md)

## Provenance

The suite is a cleaned, generic extract of a private paper-trading research system. It contains
the platform modules only: no bots, strategies, budgets, account bindings, credentials, trading
data or private reports. Its git history starts fresh at the first public commit.
