# Third-party licences

The Shadow Trading Suite contains **no vendored third-party source code**. Its dependencies are
installed separately by your package manager, each under its own licence. The table lists the
versions locked in `uv.lock`. The licence data comes from the installed package metadata
(`License-Expression` or classifier).

## Runtime dependencies (installed with the suite)

| Package | Locked version | Licence | GPL-3.0 compatible |
|---|---|---|---|
| duckdb | 1.5.6 | MIT | yes |
| pydantic | 2.13.5 | MIT | yes |
| pydantic-core | 2.46.5 | MIT | yes |
| annotated-types | 0.8.0 | MIT | yes |
| typing-inspection | 0.4.4 | MIT | yes |
| typing-extensions | 4.16.0 | PSF-2.0 | yes |
| websockets | 17.2 | BSD-3-Clause | yes |

## Development-only dependencies (not distributed with the package)

| Package | Locked version | Licence |
|---|---|---|
| pytest | 9.1.1 | MIT |
| pytest-socket | 0.8.1 | MIT |
| hypothesis | 6.168.3 | MPL-2.0 |
| mypy | 2.4.0 | MIT |
| ruff | 0.16.9 | MIT |
| pluggy, iniconfig, mypy-extensions, librt, ast-serialize | (lock) | MIT |
| packaging | (lock) | Apache-2.0 OR BSD-2-Clause |
| pathspec | (lock) | MPL-2.0 |
| Pygments | (lock) | BSD-2-Clause |
| sortedcontainers | (lock) | Apache-2.0 |

## Build tooling

| Tool | Licence |
|---|---|
| hatchling (build backend) | MIT |
| uv | Apache-2.0 OR MIT |

## Recorded public API samples

See [tests/fixtures/README.md](tests/fixtures/README.md).
