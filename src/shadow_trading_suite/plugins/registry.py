"""Plugin registry: explicit registration only; empty by default.

Nothing is loaded automatically. A plugin is registered only when the operator names it:

- in code: `PluginRegistry.register(plugin)`;
- in the suite configuration: `[[plugins]] factory = "package.module:factory"`, loaded by
  `load_factory`, or `entry_point = "name"` from the `shadow_trading_suite.plugins` entry-point
  group (`installed_entry_points` only LISTS what is installed; it never imports or starts it).

The suite ships no plugin and registers none.
"""

import importlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import metadata
from typing import Any

from pydantic import ValidationError

from shadow_trading_suite.plugins.contract import (
    PLUGIN_API_VERSION,
    BotPlugin,
    PluginManifest,
)

ENTRY_POINT_GROUP = "shadow_trading_suite.plugins"
_FACTORY = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


class PluginError(Exception):
    """A plugin could not be loaded or registered (nothing was registered)."""


class PluginState(StrEnum):
    REGISTERED = "registered"
    CONFIGURED = "configured"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class PluginEntry:
    plugin: BotPlugin
    manifest: PluginManifest
    state: PluginState = PluginState.REGISTERED
    error: str | None = None
    signals: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(
        default_factory=lambda: {"requests": 0, "accepted": 0, "rejected": 0, "errors": 0}
    )


def validate_plugin(plugin: object) -> PluginManifest:
    """The plugin's manifest, or PluginError when the object does not fulfil the contract."""
    if not isinstance(plugin, BotPlugin):
        raise PluginError(f"{type(plugin).__name__} does not implement the BotPlugin contract")
    manifest = getattr(plugin, "manifest", None)
    if isinstance(manifest, PluginManifest):
        return manifest
    try:
        return PluginManifest.model_validate(manifest)
    except ValidationError as exc:
        raise PluginError(f"invalid plugin manifest: {exc}") from exc


class PluginRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, PluginEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[PluginEntry]:
        return iter(sorted(self._entries.values(), key=lambda e: e.manifest.plugin_id))

    def __contains__(self, plugin_id: object) -> bool:
        return plugin_id in self._entries

    def get(self, plugin_id: str) -> PluginEntry:
        try:
            return self._entries[plugin_id]
        except KeyError:
            raise PluginError(f"no plugin {plugin_id!r} is registered") from None

    def register(self, plugin: object) -> PluginEntry:
        manifest = validate_plugin(plugin)
        if manifest.api_version != PLUGIN_API_VERSION:
            raise PluginError(f"plugin API {manifest.api_version} is not supported")
        if manifest.plugin_id in self._entries:
            raise PluginError(f"plugin id {manifest.plugin_id!r} is already registered")
        entry = PluginEntry(plugin=plugin, manifest=manifest)  # type: ignore[arg-type]
        self._entries[manifest.plugin_id] = entry
        return entry

    def discard(self, plugin_id: str) -> None:
        """Undo a registration that was refused before the plugin was configured."""
        entry = self._entries.get(plugin_id)
        if entry is not None and entry.state is PluginState.REGISTERED:
            del self._entries[plugin_id]

    def describe(self) -> list[dict[str, Any]]:
        """JSON-ready summary for the status file and the UI (no plugin code is called)."""
        return [
            {
                "plugin_id": e.manifest.plugin_id,
                "name": e.manifest.name,
                "version": e.manifest.version,
                "venue": e.manifest.venue.value,
                "instruments": [str(i) for i in e.manifest.instruments],
                "state": e.state.value,
                "error": e.error,
                "counters": dict(e.counters),
                "recent_signals": e.signals[-10:],
            }
            for e in self
        ]


def load_factory(spec: str) -> Callable[[], object]:
    """`package.module:factory` -> the callable (imported, not called)."""
    if not _FACTORY.match(spec):
        raise PluginError(f"plugin factory must be 'package.module:name', got {spec!r}")
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise PluginError(f"cannot import plugin module {module_name!r}: {exc}") from exc
    factory = getattr(module, attr, None)
    if not callable(factory):
        raise PluginError(f"{spec!r} is not callable")
    return factory  # type: ignore[no-any-return]


def installed_entry_points() -> list[str]:
    """Names of installed plugin entry points (listed only; nothing is imported)."""
    return sorted(ep.name for ep in metadata.entry_points(group=ENTRY_POINT_GROUP))


def load_entry_point(name: str) -> Callable[[], object]:
    matches = [ep for ep in metadata.entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if len(matches) != 1:
        raise PluginError(f"no single installed plugin entry point named {name!r}")
    try:
        factory = matches[0].load()
    except Exception as exc:
        raise PluginError(f"cannot load plugin entry point {name!r}: {exc}") from exc
    if not callable(factory):
        raise PluginError(f"entry point {name!r} is not callable")
    return factory  # type: ignore[no-any-return]
