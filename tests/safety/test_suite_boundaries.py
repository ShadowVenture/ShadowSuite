"""Static and behavioural safety boundaries of the suite. Never weaken these to make code pass.

- paper only: the settings refuse live mode, credentials and wallet configuration;
- network: outbound HTTP only through `exchanges/http.py`, WebSockets only through
  `streaming/transport.py`, inbound only through `web/server.py` (loopback);
- the REST allowlists contain public market-data paths only; no order path exists;
- plugins and the host never import a network module;
- no bot or strategy ships in the package.
"""

import ast
import re
from pathlib import Path

import pytest

from shadow_trading_suite.config.settings import credential_env_names, load_settings
from shadow_trading_suite.core.errors import ConfigError, SafetyViolationError
from shadow_trading_suite.exchanges.coinbase.adapter import ENDPOINT_POLICY as COINBASE
from shadow_trading_suite.exchanges.cryptocom.adapter import ENDPOINT_POLICY as CRYPTOCOM
from shadow_trading_suite.streaming.transport import ALLOWED_WS_URLS

SRC = Path(__file__).resolve().parents[2] / "src" / "shadow_trading_suite"
FILES = sorted(SRC.rglob("*.py"))

NETWORK_MODULES = {
    "urllib.request",
    "http.client",
    "http.server",
    "socket",
    "ssl",
    "websockets",
    "requests",
    "httpx",
    "aiohttp",
}
ALLOWED = {
    "exchanges/http.py": {"urllib.request", "ssl", "http.client"},
    "streaming/transport.py": {"websockets", "ssl"},
    "web/server.py": {"http.server"},
}
FORBIDDEN_LIBS = {"ccxt", "coinbase", "cryptography", "jwt", "nacl", "web3", "eth_account"}


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def _rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _network(module: str) -> bool:
    return module in NETWORK_MODULES or module.split(".")[0] in {
        "websockets",
        "requests",
        "httpx",
        "aiohttp",
        "socket",
        "ssl",
    }


def test_network_modules_are_imported_only_at_the_chokepoints() -> None:
    offenders = []
    for f in FILES:
        used = {m for m in _imports(f) if _network(m)}
        allowed = ALLOWED.get(_rel(f), set())
        extra = sorted(m for m in used if m not in allowed and m.split(".")[0] not in allowed)
        if extra:
            offenders.append((_rel(f), extra))
    assert offenders == []


def test_no_exchange_sdk_signing_or_wallet_library_is_imported() -> None:
    offenders = [
        (_rel(f), m) for f in FILES for m in _imports(f) if m.split(".")[0] in FORBIDDEN_LIBS
    ]
    assert offenders == []


def test_plugins_host_and_ui_never_import_a_network_or_exchange_client_module() -> None:
    banned = re.compile(
        r"^shadow_trading_suite\.(exchanges\.(http|client|service|coinbase\.adapter_net)|"
        r"streaming\.(transport|session|service))$"
    )
    for sub in ("plugins", "host", "web"):
        for f in sorted((SRC / sub).rglob("*.py")):
            bad = [m for m in _imports(f) if banned.match(m)]
            assert bad == [], (_rel(f), bad)


def test_the_rest_allowlists_are_public_market_data_only() -> None:
    patterns = [*COINBASE.path_patterns, *CRYPTOCOM.path_patterns]
    for p in patterns:
        assert "order" not in p.lower(), p
        assert "private" not in p.lower(), p
        assert "account" not in p.lower(), p
        assert "/market/" in p or "/public/" in p or p.endswith("/time"), p


def test_the_websocket_allowlist_is_the_public_market_feeds_only() -> None:
    for url in ALLOWED_WS_URLS.values():
        assert url.startswith("wss://"), url
        assert "user" not in url, url
        assert "private" not in url, url


def test_no_order_or_private_endpoint_literal_exists_in_the_source() -> None:
    needle = re.compile(
        r"""["'](/api/v3/brokerage/orders|/orders\b|private/create-order|private/cancel-order|"""
        r"""private/get-account|/withdraw)""",
        re.IGNORECASE,
    )
    offenders = [(_rel(f), m.group(0)) for f in FILES for m in needle.finditer(f.read_text())]
    assert offenders == []


def test_no_bot_or_strategy_ships_in_the_package() -> None:
    assert not (SRC / "strategies").exists()
    names = {f.stem for f in FILES} | {d.name for d in SRC.rglob("*") if d.is_dir()}
    assert not any(re.fullmatch(r"cb0\d.*", n) for n in names), names
    text = "\n".join(f.read_text() for f in FILES)
    assert not re.search(r"\bcb-0[1-6]\b", text)


@pytest.mark.parametrize(
    "env",
    [
        {"LIVE_TRADING_ENABLED": "true"},
        {"PAPER_TRADING": "false"},
        {"WALLET_ADDRESS": "x"},
        {"PAYOUT_DESTINATION_ADDRESS": "x"},
    ],
)
def test_the_settings_refuse_live_mode_credentials_and_wallets(env: dict[str, str]) -> None:
    with pytest.raises((SafetyViolationError, ConfigError)):
        load_settings(env)


def test_credentials_in_the_environment_are_never_read_only_named() -> None:
    env = {"COINBASE_API_KEY": "placeholder", "CRYPTO_COM_API_SECRET": "placeholder"}
    settings = load_settings(env)
    assert "placeholder" not in settings.model_dump_json()
    assert credential_env_names(env) == ["COINBASE_API_KEY", "CRYPTO_COM_API_SECRET"]


def test_the_web_ui_binds_loopback_by_default() -> None:
    assert load_settings({}).api_host == "127.0.0.1"
