"""Renders Claude Code plugins as native Codex plugins in a local marketplace, one per trial."""

import json
import logging
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mozg_eval.drivers.claude_plugins import (
    IGNORED,
    ClaudeManifest,
    PluginRenderError,
    resolve_plugin_root,
)

log = logging.getLogger(__name__)

MARKETPLACE = "mzeval"
CAPTURE = Path(__file__).with_name("codex_hook_capture.py")
HOOK_COMMANDS = ".mzeval-hook-commands.json"
# Claude's file tools are one tool on Codex; other matchers name tools both agents share.
_MATCHER_RENAMES = {"Write": "apply_patch", "Edit": "apply_patch", "MultiEdit": "apply_patch"}


def _rename_matcher(matcher: str) -> str:
    names = dict.fromkeys(_MATCHER_RENAMES.get(name, name) for name in matcher.split("|"))
    return "|".join(names)


class CodexMarketplace:
    """`root` holds the marketplace; `codex_home` is the trial's CODEX_HOME it installs into."""

    def __init__(self, root: Path, codex_home: Path) -> None:
        self.root = root
        self.codex_home = codex_home
        self.plugins: list[str] = []

    def render(self, source: Path) -> str:
        """Copy one Claude plugin in as a Codex plugin and return its name."""
        manifest = ClaudeManifest.of(source)
        name, version = manifest.name, manifest.version
        if name in self.plugins:
            raise PluginRenderError(f"{source}: a plugin named {name} is already in this trial")
        target = self.root / "plugins" / name
        shutil.copytree(source, target, ignore=IGNORED)
        (target / ".codex-plugin").mkdir()
        codex_manifest = manifest.model_dump_json(exclude_none=True)
        (target / ".codex-plugin" / "plugin.json").write_text(codex_manifest)
        installed = self.codex_home / "plugins" / "cache" / MARKETPLACE / name / version
        resolve_plugin_root(target, installed)
        self._instrument_hooks(target, name)
        self.plugins.append(name)
        log.debug("rendered plugin %s %s from %s", name, version, source)
        return name

    def write_index(self) -> None:
        entries = [
            {"name": name, "source": {"source": "local", "path": f"./plugins/{name}"}}
            for name in self.plugins
        ]
        index = self.root / ".agents" / "plugins" / "marketplace.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps({"name": MARKETPLACE, "plugins": entries}))

    def install_commands(self, codex: str) -> list[list[str]]:
        """The `codex plugin` calls that register the marketplace and enable every plugin."""
        commands = [[codex, "plugin", "marketplace", "add", str(self.root)]]
        commands += [[codex, "plugin", "add", f"{name}@{MARKETPLACE}"] for name in self.plugins]
        return commands

    @staticmethod
    def _instrument_hooks(plugin: Path, name: str) -> None:
        """Route every hook through the capture script, which logs it and runs the original."""
        path = plugin / "hooks" / "hooks.json"
        if not path.is_file():
            return
        config = json.loads(path.read_text(encoding="utf-8"))
        originals = instrument_hooks(config.get("hooks", {}), name)
        (plugin / "hooks" / HOOK_COMMANDS).write_text(json.dumps(originals))
        path.write_text(json.dumps(config))


def instrument_hooks(
    hooks: dict[str, list[Any]], owner: str, commands: Path | None = None
) -> dict[str, str]:
    """Point each command hook at the capture script, in place; returns the originals by key.

    Plugin hooks find `commands` through PLUGIN_ROOT, so only other hooks pass its path.
    """
    originals: dict[str, str] = {}
    for event, groups in hooks.items():
        for g, group in enumerate(groups):
            if "matcher" in group:
                group["matcher"] = _rename_matcher(group["matcher"])
            kept: list[dict[str, Any]] = []
            for h, handler in enumerate(group.get("hooks", [])):
                if (kind := handler.get("type")) != "command":
                    log.warning("%s: %s hook of type %s dropped", owner, event, kind)
                    continue
                key = f"{event}:{g}:{h}"
                originals[key] = handler["command"]
                handler["command"] = _capture_command(owner, event, key, commands)
                kept.append(handler)
            group["hooks"] = kept
    return originals


def _capture_command(owner: str, event: str, key: str, commands: Path | None) -> str:
    args: Sequence[str] = (sys.executable, str(CAPTURE), owner, event, key)
    if commands is not None:
        args = (*args, str(commands))
    return " ".join(f'"{arg}"' for arg in args)
