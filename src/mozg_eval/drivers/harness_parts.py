"""Reading a harness's parts the same way for every driver that writes them into its home."""

import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mozg_eval.drivers.protocol import AgentError
from mozg_eval.spec.harness import Harness, read_frontmatter

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


def skill_name(folder: Path) -> str:
    meta, _ = read_frontmatter(folder / "SKILL.md")
    return str(meta.get("name") or folder.name)


def agent_name(path: Path) -> str:
    return str(read_frontmatter(path)[0]["name"])


def hooks_of(harness: Harness) -> dict[str, list[Any]]:
    """hooks.json's `hooks` map, harness root filled in; Any because handlers are free-form."""
    if harness.hooks is None:
        return {}
    text = harness.hooks.read_text(encoding="utf-8")
    hooks: dict[str, list[Any]] = json.loads(
        text.replace(HARNESS_ROOT, str(harness.root.resolve()))
    )["hooks"]
    return hooks


def write_instructions(harness: Harness, path: Path, agent: str) -> None:
    """Appends each instruction's body to the agent's one user-level instructions file."""
    if not harness.instructions:
        return
    bodies = [path.read_text(encoding="utf-8").rstrip()] if path.exists() else []
    for source in harness.instructions:
        meta, body = read_frontmatter(source)
        if "paths" in meta:
            log.warning("%s: %s has no path-scoped instructions; it loads always", source, agent)
        bodies.append(body.strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(bodies) + "\n", encoding="utf-8")
