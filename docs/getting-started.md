# Getting started

## 1. Install

Requirements: Linux and Python 3.12-3.14 (see [platforms.md](platforms.md)).

```bash
git clone https://github.com/ShadowVenture/Shadow-Trading-Suite.git
cd Shadow-Trading-Suite
uv sync --frozen
uv run pytest            # about 900 offline tests; network sockets are disabled
```

Without uv, install the preview wheel into a virtual environment
(`pip install shadow_trading_suite-<version>-py3-none-any.whl`). The `sts` command is then
available.

## 2. Start without bots

The suite ships no bot. With no configuration it uses an empty portfolio and an empty plugin
registry:

```bash
uv run sts version
uv run sts check                                   # {"ok": true, "plugins": [], ...}
uv run sts plugins                                 # configured: [], installed entry points: []
```

## 3. Run an offline scenario

A scenario is a JSON file with instrument metadata and an ordered list of public market events
(see `src/shadow_trading_suite/host/scenario.py`). The example contains synthetic prices for
BTC-USD and the EUR-rate tickers. Its instrument metadata is a recorded public Coinbase product
row.

```bash
uv run sts simulate --scenario examples/scenarios/btc-usd-short.json --data-dir /tmp/sts-demo
```

The command prints the final host status as JSON (`"plugins": []` without bots) and writes:

- `/tmp/sts-demo/ledger.duckdb`: the paper ledger (owner-only file permissions);
- `/tmp/sts-demo/status/runtime.json` and `plugins.json`: status files for the UI.

## 4. Look at it in the UI

```bash
uv run sts serve --status-dir /tmp/sts-demo/status          # http://127.0.0.1:8080/
```

The UI is read-only. It binds 127.0.0.1 only and has no scripts and no controls. Without bots
it shows **"No bots installed."** Status files older than 120 s are labelled stale.
`/api/v1/status` returns the same data as JSON.

## 5. Add your own bot

See [plugins.md](plugins.md). In short:

1. Write a plugin.
2. Add a `[[bots]]` risk profile to a copy of `config/portfolio.example.toml`.
3. Reference both in a copy of `config/suite.example.toml`.
4. Run:

```bash
uv run sts check --config my-suite.toml
uv run sts simulate --scenario examples/scenarios/btc-usd-short.json --config my-suite.toml
```

## Safety switches

Every command refuses to run (exit code 3) unless `PAPER_TRADING` is `true` (the default) and
every `*LIVE_TRADING*` variable is `false` or unset. Wallet or payout variables are refused.
Credential-like variables (for example `COINBASE_API_KEY`) are never read; `sts simulate`
names them in a warning.
