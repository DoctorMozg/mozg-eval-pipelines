"""Writes a harness into a trial's Claude config dir and checks that the session loaded it."""

import json
import logging
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mozg_eval.drivers.harness_parts import agent_name, expand_env, hooks_of, skill_name
from mozg_eval.drivers.protocol import AgentError
from mozg_eval.spec.harness import Harness, HttpServer

log = logging.getLogger(__name__)


class ClaudeHarness:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness

    def materialize(self, config: Path, workspace: Path, env: Mapping[str, str]) -> None:
        """Native files first, then the neutral parts, merged into what native already wrote."""
        harness = self.harness
        if native := harness.native.get("claude"):
            shutil.copytree(native, config, dirs_exist_ok=True)
        # Rules load at launch like CLAUDE.md, and one file per instruction keeps `paths:` working.
        for path in harness.instructions:
            (config / "rules").mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, config / "rules" / path.name)
        for folder in harness.skills:
            shutil.copytree(folder, config / "skills" / folder.name, dirs_exist_ok=True)
        for path in harness.agents:
            (config / "agents").mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, config / "agents" / path.name)
        if harness.hooks:
            self._merge_hooks(config / "settings.json")
        self._write_claude_json(config / ".claude.json", workspace, env)
        log.info("harness %s written to %s", harness.name, config)

    def _merge_hooks(self, settings_path: Path) -> None:
        hooks = hooks_of(self.harness)
        settings = _read_json(settings_path)
        merged: dict[str, list[Any]] = settings.setdefault("hooks", {})
        for event, matchers in hooks.items():
            merged.setdefault(event, []).extend(matchers)
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    def _write_claude_json(self, path: Path, workspace: Path, env: Mapping[str, str]) -> None:
        data = _read_json(path)
        servers: dict[str, Any] = data.setdefault("mcpServers", {})
        for name, server in self.harness.manifest.mcp.items():
            if isinstance(server, HttpServer):
                headers = {k: expand_env(v, env) for k, v in server.headers.items()}
                servers[name] = {"type": "http", "url": server.url, "headers": headers}
            else:
                servers[name] = {
                    "type": "stdio",
                    "command": server.command,
                    "args": server.args,
                    "env": {k: expand_env(v, env) for k, v in server.env.items()},
                }
        # Headless runs ignore an untrusted project's permission rules, so trust the workspace.
        projects: dict[str, Any] = data.setdefault("projects", {})
        projects.setdefault(str(workspace.resolve()), {})["hasTrustDialogAccepted"] = True
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def missing(self, init: Mapping[str, Any]) -> list[str]:
        """Harness parts the session's init line doesn't show; instructions aren't listed there."""
        skills = set(init.get("skills") or [])
        agents = set(init.get("agents") or [])
        status = {s.get("name"): s.get("status") for s in init.get("mcp_servers") or []}
        gone = [
            f"skill {name}" for name in map(skill_name, self.harness.skills) if name not in skills
        ]
        for name in map(agent_name, self.harness.agents):
            if name not in agents:
                gone.append(f"subagent {name}")
        for name in self.harness.manifest.mcp:
            if status.get(name) != "connected":
                gone.append(f"MCP server {name} ({status.get(name, 'absent')})")
        return gone


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AgentError(f"{path} is not a JSON object")
    return data
