# Supported platforms

| Platform | Status |
|---|---|
| Linux x86_64 (Ubuntu 24.04 in CI; developed on a recent Ubuntu) | Supported, tested |
| Python 3.12, 3.13, 3.14 | Supported, tested in CI |
| Other Linux distributions / aarch64 | Expected to work (pure Python; needs DuckDB wheels); not tested |
| macOS | Not tested. The safety tests use POSIX file modes |
| Windows | Not supported (POSIX file permissions and umask are required) |

Runtime dependencies: `duckdb`, `pydantic` (with `pydantic-core`), `websockets`. The suite
needs no GPU, no database server and no exchange account.
