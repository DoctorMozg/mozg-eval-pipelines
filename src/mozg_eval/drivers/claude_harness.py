"""Writes a harness into a trial's Claude config dir and checks that the session loaded it."""

import json
import logging
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mozg_eval.drivers.protocol import AgentError
from mozg_eval.spec.harness import Harness, HttpServer, read_frontmatter

log = logging.getLogger(__name__)

# Hook commands name the harness's own scripts through this token, as plugins use theirs.
HARNESS_ROOT = "${HARNESS_ROOT}"
_ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def expand_env(value: str, env: Mapping[str, str]) -> str:
    """Fills `${VAR}` from the environment, so a harness file never has to hold a secret."""

    def fill(match: re.Match[str]) -> str:
        name = match[1]
        if name not in env:
            raise AgentError(f"harness needs ${{{name}}}, which the environment doesn't set")
        return env[name]

    return _ENV_REF.sub(fill, value)


def _skill_name(folder: Path) -> str:
    meta, _ = read_frontmatter(folder / "SKILL.md")
    return str(meta.get("name") or folder.name)


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
        assert self.harness.hooks is not None
        text = self.harness.hooks.read_text(encoding="utf-8")
        hooks = json.loads(text.replace(HARNESS_ROOT, str(self.harness.root.resolve())))["hooks"]
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
            f"skill {name}" for name in map(_skill_name, self.harness.skills) if name not in skills
        ]
        for path in self.harness.agents:
            name = str(read_frontmatter(path)[0]["name"])
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
