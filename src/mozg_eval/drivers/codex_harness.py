"""Writes a harness into a trial's HOME and CODEX_HOME where Codex reads user-level config."""

import json
import logging
import shutil
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mozg_eval.drivers.codex_plugins import HOOK_COMMANDS, instrument_hooks
from mozg_eval.drivers.harness_parts import (
    agent_name,
    expand_env,
    hooks_of,
    write_instructions,
)
from mozg_eval.drivers.toml_writer import TomlValue, dumps, merge
from mozg_eval.spec.harness import Harness, HttpServer, read_frontmatter

log = logging.getLogger(__name__)


class CodexHarness:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness

    def materialize(self, home: Path, workspace: Path, env: Mapping[str, str]) -> None:
        """Native files first; the neutral parts and config.toml merge into what they wrote."""
        harness, codex_home = self.harness, home / ".codex"
        config_path = codex_home / "config.toml"
        config = _read_toml(config_path)
        if native := harness.native.get("codex"):
            ignore = shutil.ignore_patterns("config.toml")
            shutil.copytree(native, codex_home, dirs_exist_ok=True, ignore=ignore)
            merge(config, _read_toml(native / "config.toml"))
        write_instructions(harness, codex_home / "AGENTS.md", "Codex")
        for folder in harness.skills:
            shutil.copytree(folder, home / ".agents" / "skills" / folder.name, dirs_exist_ok=True)
        for path in harness.agents:
            (codex_home / "agents").mkdir(parents=True, exist_ok=True)
            (codex_home / "agents" / f"{path.stem}.toml").write_text(_agent_toml(path))
        self._write_hooks(codex_home)
        merge(config, {"mcp_servers": self._mcp_servers(env)})
        # Codex skips a project's own .codex layer unless the folder is trusted.
        merge(config, {"projects": {str(workspace.resolve()): {"trust_level": "trusted"}}})
        config_path.write_text(dumps(config), encoding="utf-8")
        log.info("harness %s written to %s", harness.name, codex_home)

    def _write_hooks(self, codex_home: Path) -> None:
        """Every hook, native ones too, runs through the capture script that logs it."""
        path = codex_home / "hooks.json"
        native = json.loads(path.read_text(encoding="utf-8"))["hooks"] if path.exists() else {}
        hooks: dict[str, list[Any]] = native
        for event, groups in hooks_of(self.harness).items():
            hooks.setdefault(event, []).extend(groups)
        if not hooks:
            return
        commands = codex_home / HOOK_COMMANDS
        originals = instrument_hooks(hooks, f"harness {self.harness.name}", commands)
        commands.write_text(json.dumps(originals), encoding="utf-8")
        path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    def _mcp_servers(self, env: Mapping[str, str]) -> dict[str, TomlValue]:
        servers: dict[str, TomlValue] = {}
        for name, server in self.harness.manifest.mcp.items():
            if isinstance(server, HttpServer):
                headers = {k: expand_env(v, env) for k, v in server.headers.items()}
                servers[name] = {"url": server.url, "http_headers": dict(headers)}
            else:
                servers[name] = {
                    "command": server.command,
                    "args": list(server.args),
                    "env": {k: expand_env(v, env) for k, v in server.env.items()},
                }
        return servers


def _agent_toml(path: Path) -> str:
    """Codex agents are TOML; the Markdown body becomes their developer instructions."""
    meta, body = read_frontmatter(path)
    return dumps(
        {
            "name": agent_name(path),
            "description": str(meta["description"]),
            "developer_instructions": body.strip(),
        }
    )


def _read_toml(path: Path) -> dict[str, TomlValue]:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))
