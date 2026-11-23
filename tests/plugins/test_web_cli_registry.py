"""Web UI (read-only, loopback, empty state), CLI and registry behaviour."""

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread

import pytest

from shadow_trading_suite.cli import main
from shadow_trading_suite.core.errors import SafetyViolationError
from shadow_trading_suite.observability.status import write_status
from shadow_trading_suite.plugins.registry import (
    PluginError,
    PluginRegistry,
    load_factory,
)
from shadow_trading_suite.web.server import SECURITY_HEADERS, WebApp, make_server, render
from tests.fixtures.plugins.scripted import ScriptedPlugin

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _app(tmp: Path) -> WebApp:
    return WebApp(tmp / "status", lambda: NOW)


# -- web UI -----------------------------------------------------------------------------------


def test_the_ui_shows_an_explicit_empty_state_without_any_status(tmp_path: Path) -> None:
    code, ctype, body = _app(tmp_path).respond("GET", "/", {})
    assert code == 200
    assert ctype.startswith("text/html")
    html = body.decode()
    assert "No bots installed." in html
    assert "no runtime status file" in html
    assert "PAPER TRADING ONLY" in html
    assert "<script" not in html


def test_the_ui_shows_registered_plugins_escaped(tmp_path: Path) -> None:
    status = tmp_path / "status"
    write_status(status, "runtime", {"started": True, "portfolio_id": "p", "stopped_at": None}, NOW)
    write_status(
        status,
        "plugins",
        {
            "plugins": [
                {
                    "plugin_id": "x-bot",
                    "name": "<b>evil</b>",
                    "version": "1.0.0",
                    "venue": "coinbase",
                    "instruments": ["coinbase:BTC-USD"],
                    "state": "running",
                    "error": None,
                    "counters": {"requests": 1},
                }
            ]
        },
        NOW,
    )
    html = _app(tmp_path).respond("GET", "/", {})[2].decode()
    assert "x-bot" in html
    assert "&lt;b&gt;evil&lt;/b&gt;" in html
    assert "<b>evil</b>" not in html
    assert "No bots installed." not in html
    api = json.loads(_app(tmp_path).respond("GET", "/api/v1/status", {})[2])
    assert api["plugins"][0]["plugin_id"] == "x-bot"
    assert api["paper_only"] is True


def test_a_stale_runtime_status_is_labelled(tmp_path: Path) -> None:
    write_status(
        tmp_path / "status", "runtime", {"started": True, "stopped_at": None}, NOW.replace(hour=11)
    )
    view = _app(tmp_path).view()
    assert "stale" in str(view["runtime_problem"])


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_the_ui_is_read_only(tmp_path: Path, method: str) -> None:
    assert _app(tmp_path).respond(method, "/", {})[0] == 405


def test_unknown_routes_and_foreign_host_headers_are_refused(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert app.respond("GET", "/admin", {})[0] == 404
    app.allowed_hosts = frozenset({"127.0.0.1:8080"})
    assert app.respond("GET", "/", {"Host": "evil.example:8080"})[0] == 421
    assert app.respond("GET", "/", {"Host": "127.0.0.1:8080"})[0] == 200


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::1", "localhost"])  # noqa: S104
def test_the_ui_binds_ipv4_loopback_only(tmp_path: Path, host: str) -> None:
    with pytest.raises(SafetyViolationError):
        make_server(host, 0, _app(tmp_path))


def test_render_has_no_inline_script_or_style() -> None:
    html = render({"version": "x", "runtime": None, "plugins": []})
    assert "<script" not in html
    assert "style=" not in html


@pytest.mark.enable_socket
def test_the_served_ui_sends_the_security_headers(tmp_path: Path) -> None:
    server = make_server("127.0.0.1", 0, _app(tmp_path))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
            headers = dict(resp.headers.items())
            assert resp.status == 200
            assert "No bots installed." in resp.read().decode()
        for name, value in SECURITY_HEADERS:
            assert headers[name] == value
        req = urllib.request.Request(f"http://127.0.0.1:{port}/", method="POST", data=b"x")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req)  # noqa: S310 - fixed loopback http URL
        assert err.value.code == 405
        err.value.close()
    finally:
        server.shutdown()
        server.server_close()


# -- registry -----------------------------------------------------------------------------------


def test_the_registry_is_empty_by_default_and_describes_nothing() -> None:
    registry = PluginRegistry()
    assert len(registry) == 0
    assert registry.describe() == []


@pytest.mark.parametrize("spec", ["no-colon", "os:system:x", "a b:c", ":f", "m:"])
def test_bad_factory_specs_are_refused(spec: str) -> None:
    with pytest.raises(PluginError):
        load_factory(spec)


def test_a_factory_is_imported_not_called() -> None:
    factory = load_factory("tests.fixtures.plugins.scripted:create_buyer")
    assert callable(factory)
    assert isinstance(factory(), ScriptedPlugin)


def test_a_manifest_with_a_foreign_venue_instrument_is_invalid() -> None:
    from pydantic import ValidationError

    from shadow_trading_suite.core.instrument import InstrumentId, Venue
    from shadow_trading_suite.plugins.contract import PluginManifest

    with pytest.raises(ValidationError, match="plugin's venue"):
        PluginManifest(
            plugin_id="x",
            name="x",
            version="1.0.0",
            venue=Venue.COINBASE,
            instruments=(InstrumentId(venue=Venue.CRYPTOCOM, symbol="BTC_USD"),),
        )


# -- CLI ------------------------------------------------------------------------------------------


def test_cli_version_and_check_without_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["paper_only"] is True
    assert main(["check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["plugins"] == []
    assert out["bots_in_portfolio"] == []


@pytest.mark.parametrize(
    "argv",
    [
        ["version"],
        ["check"],
        ["plugins"],
        ["serve", "--port", "0"],
        ["simulate", "--scenario", "x.json", "--data-dir", "/nonexistent"],
    ],
)
@pytest.mark.parametrize(
    ("name", "value"),
    [("LIVE_TRADING_ENABLED", "true"), ("PAPER_TRADING", "false"), ("WALLET_ADDRESS", "x")],
)
def test_every_cli_command_refuses_unsafe_settings(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    assert main(argv) == 3


def test_cli_simulates_the_example_scenario_without_bots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = ROOT / "examples" / "scenarios" / "btc-usd-short.json"
    code = main(["simulate", "--scenario", str(scenario), "--data-dir", str(tmp_path / "d")])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["plugins"] == []
    assert report["events_applied"] > 0
    assert (tmp_path / "d" / "status" / "runtime.json").is_file()


def test_cli_simulates_with_an_external_test_plugin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "portfolio.toml").write_text(
        (ROOT / "config" / "portfolio.example.toml")
        .read_text()
        .replace(
            "bots = []\n",
            """
[[bots]]
bot_id = "test-bot"
display_name = "Test bot"
venue = "coinbase"
allocation_eur = "1000"
max_leverage = "1"
max_position_margin = "0.20"
max_total_margin = "0.50"
max_effective_leverage = "1"
min_liquidation_distance = "0.05"
daily_loss_limit = "0.10"
max_drawdown = "0.25"
max_open_positions = 3
max_orders_per_minute = 10
""",
        )
    )
    (tmp_path / "suite.toml").write_text(
        '[suite]\ndata_dir = "data"\nportfolio = "portfolio.toml"\n\n'
        '[[plugins]]\nfactory = "tests.fixtures.plugins.scripted:create_buyer"\n'
    )
    scenario = ROOT / "examples" / "scenarios" / "btc-usd-short.json"
    code = main(["simulate", "--scenario", str(scenario), "--config", str(tmp_path / "suite.toml")])
    assert code == 0, capsys.readouterr().err
    report = json.loads(capsys.readouterr().out)
    (plugin,) = report["plugins"]
    assert plugin["plugin_id"] == "test-bot"
    assert plugin["counters"]["requests"] == 1
    assert plugin["counters"]["accepted"] == 1
    assert plugin["state"] == "stopped"  # the shutdown stops every plugin


@pytest.mark.enable_socket
def test_sts_serve_stops_cleanly_on_sigterm(tmp_path: Path) -> None:
    import os
    import signal
    import subprocess
    import sys
    import time

    env = {k: v for k, v in os.environ.items() if k not in ("LIVE_TRADING_ENABLED",)}
    cmd = [sys.executable, "-m", "shadow_trading_suite.cli", "serve", "--port", "0"]
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, this interpreter
        [*cmd, "--status-dir", str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert "http://127.0.0.1:" in line
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
