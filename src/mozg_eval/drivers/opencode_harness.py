"""Writes a harness into a trial's OpenCode 2 config dir, ~/.config/opencode under its HOME."""

import json
import logging
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from mozg_eval.drivers.harness_parts import expand_env, write_instructions
from mozg_eval.spec.harness import Harness, HttpServer, read_frontmatter

log = logging.getLogger(__name__)


def merge_json(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Objects merge, lists append `extra`'s items, so rules from `extra` come last and win."""
    for key, value in extra.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merge_json(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            base[key] = [*current, *value]
        else:
            base[key] = value
    return base


class OpenCodeHarness:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness

    def materialize(self, config_dir: Path, env: Mapping[str, str]) -> dict[str, Any]:
        """Writes the files; returns the opencode.json the driver's own config merges onto.

        Any because opencode.json is free-form JSON from the harness's native folder.
        """
        harness = self.harness
        config: dict[str, Any] = {}
        if native := harness.native.get("opencode"):
            ignore = shutil.ignore_patterns("opencode.json")
            shutil.copytree(native, config_dir, dirs_exist_ok=True, ignore=ignore)
            if (native / "opencode.json").is_file():
                config = json.loads((native / "opencode.json").read_text(encoding="utf-8"))
        write_instructions(harness, config_dir / "AGENTS.md", "OpenCode")
        for folder in harness.skills:
            shutil.copytree(folder, config_dir / "skills" / folder.name, dirs_exist_ok=True)
        for path in harness.agents:
            (config_dir / "agents").mkdir(parents=True, exist_ok=True)
            (config_dir / "agents" / path.name).write_text(_subagent(path), encoding="utf-8")
        if harness.manifest.mcp:
            merge_json(config, {"mcp": {"servers": self._mcp_servers(env)}})
        log.info("harness %s written to %s", harness.name, config_dir)
        return config

    def _mcp_servers(self, env: Mapping[str, str]) -> dict[str, Any]:
        servers: dict[str, Any] = {}
        for name, server in self.harness.manifest.mcp.items():
            if isinstance(server, HttpServer):
                headers = {k: expand_env(v, env) for k, v in server.headers.items()}
                # No one can finish an OAuth login in a headless trial.
                servers[name] = {
                    "type": "remote",
                    "url": server.url,
                    "headers": headers,
                    "oauth": False,
                }
            else:
                servers[name] = {
                    "type": "local",
                    "command": [server.command, *server.args],
                    "environment": {k: expand_env(v, env) for k, v in server.env.items()},
                }
        return servers


def _subagent(path: Path) -> str:
    """OpenCode takes the id from the file name; Claude-only keys would just draw warnings."""
    meta, body = read_frontmatter(path)
    front = yaml.safe_dump({"description": meta["description"], "mode": "subagent"})
    return f"---\n{front}---\n{body}"
