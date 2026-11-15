"""Writes a harness into a trial's Pi agent dir and names the flags that load it."""

import json
import logging
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mozg_eval.drivers.harness_parts import expand_env
from mozg_eval.spec.harness import Harness, HttpServer, read_frontmatter

log = logging.getLogger(__name__)


class PiHarness:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness

    def materialize(self, agent_dir: Path, env: Mapping[str, str]) -> list[str]:
        """Writes the parts; returns the flags that load them, as the driver turns discovery off."""
        harness = self.harness
        if native := harness.native.get("pi"):
            shutil.copytree(
                native, agent_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns("mcp.json")
            )
        self._write_instructions(agent_dir / "AGENTS.md")
        for folder in harness.skills:
            shutil.copytree(folder, agent_dir / "skills" / folder.name, dirs_exist_ok=True)
        servers = self._mcp_servers(native / "mcp.json" if native else None, env)
        if servers:
            (agent_dir / "mcp.json").write_text(
                json.dumps({"mcpServers": servers}), encoding="utf-8"
            )
        log.info("harness %s written to %s", harness.name, agent_dir)
        flags: list[str] = []
        if (agent_dir / "skills").is_dir():
            flags += ["--skill", str(agent_dir / "skills")]
        extensions = agent_dir / "extensions"
        for entry in sorted(extensions.iterdir()) if extensions.is_dir() else []:
            flags += ["-e", str(entry)]
        if servers:
            flags += ["-e", "builtin:mcp"]
        return flags

    def _write_instructions(self, path: Path) -> None:
        if not self.harness.instructions:
            return
        bodies = [path.read_text(encoding="utf-8").rstrip()] if path.exists() else []
        for source in self.harness.instructions:
            meta, body = read_frontmatter(source)
            if "paths" in meta:
                log.warning("%s: Pi has no path-scoped instructions; it loads always", source)
            bodies.append(body.strip())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n\n".join(bodies) + "\n", encoding="utf-8")

    def _mcp_servers(self, native: Path | None, env: Mapping[str, str]) -> dict[str, Any]:
        """Native mcp.json servers, then the manifest's; Any because entries are free-form JSON."""
        servers: dict[str, Any] = {}
        if native is not None and native.is_file():
            servers |= json.loads(native.read_text(encoding="utf-8")).get("mcpServers", {})
        for name, server in self.harness.manifest.mcp.items():
            if isinstance(server, HttpServer):
                headers = {k: expand_env(v, env) for k, v in server.headers.items()}
                servers[name] = {"url": server.url, "headers": headers}
            else:
                servers[name] = {
                    "command": server.command,
                    "args": server.args,
                    "env": {k: expand_env(v, env) for k, v in server.env.items()},
                }
        return servers
