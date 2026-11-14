"""Claude Code plugins copied into a trial, for agents that read skills from plain directories."""

import logging
import shutil
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger(__name__)

IGNORED = shutil.ignore_patterns(".git", "__pycache__")


class PluginRenderError(ValueError):
    pass


class ClaudeManifest(BaseModel):
    # The name becomes a directory in the trial, so no separators or leading dots.
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    version: str = "0.0.0"
    description: str | None = None

    @classmethod
    def of(cls, source: Path) -> Self:
        path = source / ".claude-plugin" / "plugin.json"
        if not path.is_file():
            raise PluginRenderError(f"{source}: no .claude-plugin/plugin.json")
        try:
            return cls.model_validate_json(path.read_bytes())
        except ValidationError as exc:
            raise PluginRenderError(f"{path}: {exc.errors()[0]['msg']}") from exc


def resolve_plugin_root(plugin: Path, root: Path) -> None:
    """Claude substitutes the plugin root inside skill text; other agents show the raw text."""
    skills = plugin / "skills"
    for doc in skills.rglob("*.md") if skills.is_dir() else []:
        text = doc.read_text(encoding="utf-8")
        if "${CLAUDE_PLUGIN_ROOT}" in text:
            doc.write_text(text.replace("${CLAUDE_PLUGIN_ROOT}", str(root)), encoding="utf-8")


class PluginCopies:
    """Copies each plugin under `root`, its skill text pointing at the copy."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.names: list[str] = []

    def add(self, source: Path) -> Path:
        name = ClaudeManifest.of(source).name
        if name in self.names:
            raise PluginRenderError(f"{source}: a plugin named {name} is already in this trial")
        target = self.root / name
        shutil.copytree(source, target, ignore=IGNORED)
        resolve_plugin_root(target, target)
        self.names.append(name)
        log.debug("copied plugin %s from %s", name, source)
        return target
