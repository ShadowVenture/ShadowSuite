"""The shipped examples: an empty configuration and a documentation skeleton (never auto-loaded)."""

import json
from pathlib import Path

import pytest

from shadow_trading_suite.cli import main
from shadow_trading_suite.plugins.registry import load_factory, validate_plugin
from shadow_trading_suite.suite_config import load_suite_config
from tests.plugins.support import make_host

ROOT = Path(__file__).resolve().parents[2]


def test_the_example_suite_configuration_installs_no_plugin() -> None:
    loaded = load_suite_config(ROOT / "config" / "suite.example.toml")
    assert loaded.config.plugins == ()
    assert loaded.portfolio.bots == ()


def test_check_with_the_example_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--config", str(ROOT / "config" / "suite.example.toml")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plugins"] == []


def test_the_skeleton_fulfils_the_contract_and_never_orders(tmp_path: Path) -> None:
    plugin = load_factory("examples.plugin_skeleton.my_bot:create")()
    manifest = validate_plugin(plugin)
    host, feed = make_host(tmp_path, manifest.plugin_id)
    host.start()
    entry = host.add_plugin(plugin, {"note_prefix": "mid-price"})
    feed.ready()
    feed.advance(1)
    feed.ready()
    assert entry.counters["requests"] == 0
    assert entry.signals[-1]["note"].startswith("mid-price 82000")
    assert host.ledger.orders() == {}
    host.shutdown()


def test_the_skeleton_refuses_unknown_settings(tmp_path: Path) -> None:
    plugin = load_factory("examples.plugin_skeleton.my_bot:create")()
    host, _ = make_host(tmp_path, "my-bot")
    entry = host.add_plugin(plugin, {"typo": "x"})
    host.start()
    assert entry.state.value == "failed"
