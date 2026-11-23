# Instructions for coding agents

These rules apply to any automated coding agent (Codex, Claude Code and others) working in this
repository. `CLAUDE.md` contains the same rules.

## Non-negotiable

1. **Paper trading only.** Never add code that submits, amends or cancels a real exchange order,
   reads or stores credentials, calls a private or authenticated endpoint, or moves funds.
   Never add wallet, withdrawal, transfer or payout functions.
2. **Never weaken `tests/safety/`** or the settings gate (`config/settings.py`) to make code
   pass. If a safety test fails, the change is wrong.
3. **No bots or strategies in this repository.** Test fixtures may use scripted plugins only
   (`tests/fixtures/plugins/`). Do not add demo strategies that are loaded automatically.
4. **Network access** only through `exchanges/http.py` (public REST GET, allowlisted paths) and
   `streaming/transport.py` (allowlisted public WebSocket URLs). Inbound only through
   `web/server.py` (loopback, read-only).
5. **Decimal only** for financial values, UTC-aware datetimes, an injected `Clock`. Missing or
   stale data fails closed; never invent or fill values.
6. Plugin orders go only through `PaperHost.submit` (host checks, then risk, then paper
   execution). Do not add a second order path.
7. Never commit secrets, `.env` files, data directories, ledgers or logs.

## Commands

```bash
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run mypy && uv run mypy tests examples
uv run pytest                       # offline; sockets disabled
```

## Layout

See [docs/architecture.md](docs/architecture.md). The plugin contract is in
`src/shadow_trading_suite/plugins/contract.py` and is documented in
[docs/plugins.md](docs/plugins.md).
