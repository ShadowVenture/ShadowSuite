# Contributing

Thank you for your interest. This is a pre-alpha, paper-trading-only project.

## Ground rules

- **Paper only.** Contributions that add a real order path, credential handling, private
  endpoints, wallet or fund-movement functions will not be accepted. The tests in
  `tests/safety/` must never be weakened to make code pass.
- **No bots or strategies in this repository.** The suite ships the platform only; bots live in
  their own packages and use the plugin contract.
- **Decimal only** for money, UTC-aware datetimes, an injected `Clock`, and no invented or
  filled-in data: missing or stale inputs fail closed.
- **Network access** only through `exchanges/http.py` (public REST) and `streaming/transport.py`
  (public WebSocket). A new endpoint must be public market data and needs a recorded fixture.
- Tests are deterministic and offline (`pytest-socket` disables sockets).
- By contributing you agree that your contribution is licensed under GPL-3.0-only.

## Development

```bash
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run mypy && uv run mypy tests examples
uv run pytest
```

Every pull request runs the same checks in CI on Python 3.12, 3.13 and 3.14, plus a secret scan.

## Pull requests

- Keep changes small and focused, with tests.
- Describe what changed and how you tested it.
- Do not commit data directories, ledgers, logs, `.env` files or anything secret.
