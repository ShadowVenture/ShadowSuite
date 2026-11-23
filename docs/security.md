# Security limits

These are properties of the code, enforced by tests in `tests/safety/`. Do not weaken them to
make code pass.

## What the suite never does

- **No real orders.** No code path submits, amends or cancels an order at an exchange. The REST
  allowlists contain public market-data paths only. A test fails if an order or private
  endpoint literal appears in the source.
- **No credentials.** No API key, secret or token is read. Credential-like environment
  variables are ignored and only named in a warning.
- **No wallets or fund movement.** There are no withdrawal, transfer, payout or wallet
  functions. Wallet, payout and key-material variables are refused at start-up.
- **No live mode.** `LIVE_TRADING_ENABLED=true` or `PAPER_TRADING=false` stops every command
  with exit code 3.

## Network boundaries

| Direction | Where | Rules |
|---|---|---|
| Outbound HTTPS | `exchanges/http.py` only | GET, allowlisted public paths per venue, allowlisted headers, rate limits, size caps |
| Outbound WebSocket | `streaming/transport.py` only | allowlisted public market-data URLs; every outbound frame is validated |
| Inbound HTTP | `web/server.py` only | 127.0.0.0/8 only, GET/HEAD only, Host-header check, strict CSP without scripts, no CORS |

A static test checks that no other module imports a network library. The plugin, host and UI
modules import no network client at all.

## Files

The CLI runs with umask 0077. The ledger and status files are owner-only. DuckDB runs with
extensions disabled and file access limited to the data directory.

## Plugins are not sandboxed

A plugin is ordinary Python code in the same process, so it can technically do anything the
process can do: read files, open sockets, import other modules. The plugin contract is an API
boundary, not a security boundary. **Install only plugins you trust and have reviewed.** Process
isolation is on the [roadmap](roadmap.md).

See [../SECURITY.md](../SECURITY.md) for how to report vulnerabilities.
