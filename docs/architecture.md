# Architecture

```
src/shadow_trading_suite/
  core/          Decimal-only money, UTC time and clocks, domain models, errors, owner-only files
  config/        settings (paper-only gate, credential and wallet refusal)
  exchanges/     public REST: http.py (the only HTTP client), policy, rate limits, retries,
                 coinbase/ and cryptocom/ adapters (public market data only)
  streaming/     public WebSocket: transport.py (the only WS client), venue protocols,
                 order books, reconnect/resync sessions, MarketState snapshots
  instruments/   instrument registry, simulation permission, example jurisdiction policy
  accounting/    double-entry journal, TradingLedger (capital, orders, fills, lots, margin,
                 funding, liquidation, replay), reporting (economic equity, EUR valuation)
  storage/       hardened DuckDB opener and the ledger store (schema v2)
  risk/          config, margin, per-bot risk engine, portfolio supervisor, capital manager,
                 audited pipeline with replay
  execution/     assumptions, pure planners, matching, PaperExecutionEngine, recovery, replay
  paper/         EUR triangulation, minute-close volatility, day-start/peak equity, quote quality
  plugins/       contract (API v1) and registry
  host/          PaperHost (the single order path for plugins) and offline scenarios
  observability/ atomic status files
  web/           read-only loopback UI and JSON status API
  cli.py         `sts`
```

Design rules:

- Financial values are `Decimal` only (floats are rejected).
- Datetimes are UTC-aware, and every component takes an injected `Clock`.
- Ledger changes are atomic units of work with balanced postings and a hash-chained audit.
- Planners are pure, so replays reproduce state exactly.
- Missing or stale data refuses; values are never invented or filled in.
