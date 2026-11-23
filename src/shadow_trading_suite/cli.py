"""`sts`: the Shadow Trading Suite command line (paper only).

    sts version
    sts plugins  [--config suite.toml]          configured plugins + installed entry points
    sts check    [--config suite.toml]          validate config, portfolio and plugins
    sts simulate --scenario FILE [--config suite.toml] [--data-dir DIR]
                                                run the paper host over an offline scenario
    sts serve    [--status-dir DIR] [--host 127.0.0.1] [--port 8080]
                                                read-only local web UI

Every command refuses to start unless PAPER_TRADING=true and LIVE_TRADING_ENABLED is false or
unset (defaults are paper). Nothing here sends an order to an exchange: no such code exists.
"""

import argparse
import json
import os
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shadow_trading_suite import __version__
from shadow_trading_suite.config.settings import credential_env_names, load_settings
from shadow_trading_suite.core.errors import ConfigError, SafetyViolationError
from shadow_trading_suite.core.fs import owner_only_files
from shadow_trading_suite.host.paper import HostError, PaperHost
from shadow_trading_suite.host.scenario import Scenario, ScenarioError
from shadow_trading_suite.plugins.registry import (
    PluginError,
    installed_entry_points,
    load_entry_point,
    load_factory,
    validate_plugin,
)
from shadow_trading_suite.risk.config import default_portfolio_config
from shadow_trading_suite.suite_config import LoadedConfig, PluginSpec, load_suite_config
from shadow_trading_suite.web.server import WebApp, make_server


def _print(doc: Any) -> None:
    print(json.dumps(doc, indent=2, sort_keys=True, default=str))


def _config(path: str | None) -> LoadedConfig | None:
    return load_suite_config(Path(path)) if path else None


def _instantiate(spec: PluginSpec) -> object:
    factory = (
        load_factory(spec.factory) if spec.factory else load_entry_point(str(spec.entry_point))
    )
    return factory()


def cmd_plugins(args: argparse.Namespace) -> int:
    loaded = _config(args.config)
    specs = loaded.config.plugins if loaded else ()
    _print(
        {
            "configured": [s.model_dump(mode="json", exclude={"config"}) for s in specs],
            "installed_entry_points": installed_entry_points(),
            "note": "installed entry points are listed only; nothing is loaded automatically",
        }
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    loaded = _config(args.config)
    portfolio = loaded.portfolio if loaded else default_portfolio_config()
    plugins: list[dict[str, Any]] = []
    problems: list[str] = []
    for spec in loaded.config.plugins if loaded else ():
        try:
            manifest = validate_plugin(_instantiate(spec))
        except PluginError as exc:
            problems.append(str(exc))
            continue
        ids = {b.bot_id for b in portfolio.bots}
        if manifest.plugin_id not in ids:
            problems.append(f"{manifest.plugin_id}: no [[bots]] risk profile in the portfolio")
        plugins.append(manifest.model_dump(mode="json"))
    _print(
        {
            "ok": not problems,
            "paper_only": True,
            "portfolio_id": portfolio.portfolio_id,
            "bots_in_portfolio": [b.bot_id for b in portfolio.bots],
            "plugins": plugins,
            "problems": problems,
        }
    )
    return 0 if not problems else 1


def _warn_credentials() -> None:
    names = credential_env_names(os.environ)
    if names:
        print(
            "warning: credential-like variables are set and IGNORED (the suite never reads "
            f"credentials): {', '.join(names)}",
            file=sys.stderr,
        )


def cmd_simulate(args: argparse.Namespace) -> int:
    settings = load_settings(os.environ)
    _warn_credentials()
    loaded = _config(args.config)
    portfolio = loaded.portfolio if loaded else default_portfolio_config()
    scenario = Scenario.load(Path(args.scenario))
    clock = scenario.clock()
    data_dir = Path(args.data_dir) if args.data_dir else (loaded.data_dir if loaded else None)
    if data_dir is None:
        raise ConfigError("simulate needs --data-dir or a --config with suite.data_dir")
    events = 0
    host = PaperHost(
        portfolio=portfolio,
        data_dir=data_dir,
        instruments=scenario.instruments,
        clock=clock,
        settings=settings,
        status_dir=data_dir / "status",
    )
    try:
        for spec in loaded.config.plugins if loaded else ():
            host.add_plugin(_instantiate(spec), spec.config)
        host.start()
        events = scenario.play(host, clock)
    finally:
        host.shutdown()
    _print((host.final_status or {}) | {"events_applied": events})
    return 0


def _interrupt(*_: object) -> None:
    raise KeyboardInterrupt


def cmd_serve(args: argparse.Namespace) -> int:
    load_settings(os.environ)  # paper-only gate
    app = WebApp(Path(args.status_dir), lambda: datetime.now(UTC))
    server = make_server(args.host, args.port, app)
    signal.signal(signal.SIGTERM, _interrupt)  # shutdown() here would deadlock serve_forever
    host, port = server.server_address[:2]
    print(f"Shadow Trading Suite UI (read-only, paper only): http://{host!s}:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sts", description="Shadow Trading Suite (paper only)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("version")
    for name in ("plugins", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("--config")
    sp = sub.add_parser("simulate")
    sp.add_argument("--scenario", required=True)
    sp.add_argument("--config")
    sp.add_argument("--data-dir")
    sp = sub.add_parser("serve")
    sp.add_argument("--status-dir", default="data/status")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)
    return p


@owner_only_files
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        load_settings(os.environ)  # paper-only gate for EVERY command (exit 3 on refusal)
        if args.command == "version":
            _print({"version": __version__, "paper_only": True, "live_trading": "not implemented"})
            return 0
        handler = {
            "plugins": cmd_plugins,
            "check": cmd_check,
            "simulate": cmd_simulate,
            "serve": cmd_serve,
        }[args.command]
        return handler(args)
    except SafetyViolationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except (ConfigError, HostError, PluginError, ScenarioError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
