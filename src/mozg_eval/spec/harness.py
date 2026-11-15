"""A harness: the agent setup a run evaluates, such as instructions, skills, hooks and MCP servers.

A harness is a directory. Its manifest holds what needs data (plugins, MCP servers); every other
part is a file or folder by convention, so a harness reads like the config it becomes.
"""

import hashlib
import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import Discriminator, Field, Tag, ValidationError

from mozg_eval.spec.loader import SpecIssue, line_map, validation_issue
from mozg_eval.spec.models import EnvName, Name, Slug, StrictModel

log = logging.getLogger(__name__)

HARNESS_FILENAME = "harness.mzeval.yaml"
HARNESS_SCHEMA = "mzeval-harness/1"

type HarnessPart = Literal["instructions", "skills", "agents", "hooks", "mcp", "plugins"]
type NativeDriver = Literal["claude", "codex", "opencode", "pi"]
NATIVE_DRIVERS: tuple[NativeDriver, ...] = ("claude", "codex", "opencode", "pi")
# Entries a harness directory may hold besides its manifest; anything else is a typo.
_ENTRIES = {"instructions", "skills", "agents", "hooks.json", "native", "README.md"}
_UNHASHED = {"__pycache__", "node_modules"}


class StdioServer(StrictModel):
    command: Name
    args: list[str] = Field(default_factory=list)
    env: dict[EnvName, str] = Field(default_factory=dict)


class HttpServer(StrictModel):
    url: Annotated[str, Field(pattern=r"^https?://")]
    headers: dict[str, str] = Field(default_factory=dict)


def _server_kind(value: object) -> str:
    url = value.get("url") if isinstance(value, dict) else getattr(value, "url", None)
    return "http" if url is not None else "stdio"


# One error per mistake: a server with `url` is HTTP, any other is stdio.
type McpServer = Annotated[
    Annotated[StdioServer, Tag("stdio")] | Annotated[HttpServer, Tag("http")],
    Discriminator(_server_kind),
]


class HarnessManifest(StrictModel):
    schema_: Literal["mzeval-harness/1"] = Field(alias="schema")
    name: Slug
    description: str = ""
    plugins: list[Name] = Field([], description="plugin directories, relative to the harness")
    mcp: dict[Slug, McpServer] = Field({}, description="MCP servers by name")


@dataclass(frozen=True)
class Harness:
    root: Path
    manifest: HarnessManifest
    instructions: tuple[Path, ...] = ()
    skills: tuple[Path, ...] = ()
    agents: tuple[Path, ...] = ()
    hooks: Path | None = None
    native: Mapping[NativeDriver, Path] = field(default_factory=dict)
    digest: str = ""

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def plugins(self) -> tuple[Path, ...]:
        return tuple((self.root / plugin).resolve() for plugin in self.manifest.plugins)

    def parts(self) -> frozenset[HarnessPart]:
        present: dict[HarnessPart, object] = {
            "instructions": self.instructions,
            "skills": self.skills,
            "agents": self.agents,
            "hooks": self.hooks,
            "mcp": self.manifest.mcp,
            "plugins": self.manifest.plugins,
        }
        return frozenset(part for part, value in present.items() if value)


class HarnessLoadError(Exception):
    def __init__(self, issues: Sequence[SpecIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))


# Any: frontmatter is untyped YAML that each driver reads its own keys from.
def read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """The YAML frontmatter of a Markdown file and the body after it; ({}, text) without one."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise ValueError("frontmatter has no closing ---")
    data = yaml.safe_load(head) or {}
    if not isinstance(data, dict):
        raise ValueError("frontmatter is not a YAML mapping")
    return data, body


class HarnessLoader:
    """Loads a harness directory and reports every problem with its file and, where known, line."""

    def load(self, root: Path) -> Harness:
        manifest_path = root / HARNESS_FILENAME
        manifest, issues = self._manifest(manifest_path)
        issues += self._unexpected_entries(root)
        instructions = self._instructions(root / "instructions", issues)
        skills = self._skills(root / "skills", issues)
        agents = self._agents(root / "agents", issues)
        hooks = self._hooks(root / "hooks.json", issues)
        native = self._native(root / "native", issues)
        if manifest is not None:
            issues += self._missing_plugins(root, manifest, manifest_path)
        if issues or manifest is None:
            raise HarnessLoadError(issues)
        plugins = [(root / plugin).resolve() for plugin in manifest.plugins]
        harness = Harness(
            root, manifest, instructions, skills, agents, hooks, native, digest([root, *plugins])
        )
        log.debug("loaded harness %s from %s: %s", harness.name, root, sorted(harness.parts()))
        return harness

    @staticmethod
    def discover(roots: Sequence[Path]) -> list[Path]:
        """Harness directories under the given paths, found by their manifest."""
        found: list[Path] = []
        for root in roots:
            if root.is_dir():
                found.extend(sorted(path.parent for path in root.rglob(HARNESS_FILENAME)))
            elif root.name == HARNESS_FILENAME:
                found.append(root.parent)
        return found

    def _manifest(self, path: Path) -> tuple[HarnessManifest | None, list[SpecIssue]]:
        if not path.is_file():
            return None, [SpecIssue(path, None, f"a harness needs a {HARNESS_FILENAME}")]
        text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
            lines = line_map(yaml.compose(text, Loader=yaml.SafeLoader))
        except yaml.MarkedYAMLError as exc:
            mark = exc.problem_mark or exc.context_mark
            return None, [SpecIssue(path, mark.line + 1 if mark else 1, f"invalid YAML: {exc}")]
        if not isinstance(data, dict):
            return None, [SpecIssue(path, 1, "a harness manifest must be a YAML mapping")]
        try:
            return HarnessManifest.model_validate(data), []
        except ValidationError as exc:
            errors = exc.errors()
            return None, [validation_issue(path, e, lines, "<harness>") for e in errors]

    @staticmethod
    def _unexpected_entries(root: Path) -> list[SpecIssue]:
        allowed = ", ".join(sorted(_ENTRIES))
        return [
            SpecIssue(entry, None, f"unexpected entry in a harness; it may hold {allowed}")
            for entry in sorted(root.iterdir())
            if entry.name not in _ENTRIES | {HARNESS_FILENAME} and not entry.name.startswith(".")
        ]

    @staticmethod
    def _instructions(folder: Path, issues: list[SpecIssue]) -> tuple[Path, ...]:
        if not folder.is_dir():
            return ()
        files: list[Path] = []
        for entry in sorted(folder.iterdir()):
            if entry.is_file() and entry.suffix == ".md":
                files.append(entry)
            elif not entry.name.startswith("."):
                issues.append(SpecIssue(entry, None, "instructions are flat .md files"))
        return tuple(files)

    @staticmethod
    def _skills(folder: Path, issues: list[SpecIssue]) -> tuple[Path, ...]:
        if not folder.is_dir():
            return ()
        skills: list[Path] = []
        for entry in sorted(folder.iterdir()):
            if (entry / "SKILL.md").is_file():
                skills.append(entry)
            elif not entry.name.startswith("."):
                issues.append(SpecIssue(entry, None, "a skill is a folder holding SKILL.md"))
        return tuple(skills)

    @staticmethod
    def _agents(folder: Path, issues: list[SpecIssue]) -> tuple[Path, ...]:
        if not folder.is_dir():
            return ()
        agents: list[Path] = []
        for entry in sorted(folder.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.suffix != ".md" or not entry.is_file():
                issues.append(SpecIssue(entry, None, "a subagent is a .md file"))
                continue
            try:
                meta, _ = read_frontmatter(entry)
            except (ValueError, yaml.YAMLError) as exc:
                issues.append(SpecIssue(entry, 1, f"invalid frontmatter: {exc}"))
                continue
            if missing := [key for key in ("name", "description") if not meta.get(key)]:
                issues.append(SpecIssue(entry, 1, f"frontmatter lacks {', '.join(missing)}"))
            else:
                agents.append(entry)
        return tuple(agents)

    @staticmethod
    def _hooks(path: Path, issues: list[SpecIssue]) -> Path | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append(SpecIssue(path, exc.lineno, f"invalid JSON: {exc.msg}"))
            return None
        events = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(events, dict) or not all(isinstance(v, list) for v in events.values()):
            message = 'hooks.json holds {"hooks": {"<Event>": [...]}}, as Claude settings do'
            issues.append(SpecIssue(path, 1, message))
            return None
        return path

    @staticmethod
    def _native(folder: Path, issues: list[SpecIssue]) -> dict[NativeDriver, Path]:
        if not folder.is_dir():
            return {}
        native: dict[NativeDriver, Path] = {}
        for driver in NATIVE_DRIVERS:
            if (folder / driver).is_dir():
                native[driver] = folder / driver
        for entry in sorted(folder.iterdir()):
            if entry.name not in native and not entry.name.startswith("."):
                message = f"native overlays are folders named {', '.join(NATIVE_DRIVERS)}"
                issues.append(SpecIssue(entry, None, message))
        return native

    @staticmethod
    def _missing_plugins(
        root: Path, manifest: HarnessManifest, manifest_path: Path
    ) -> list[SpecIssue]:
        return [
            SpecIssue(
                manifest_path, None, f"plugin {plugin}: no such directory next to the harness"
            )
            for plugin in manifest.plugins
            if not (root / plugin).is_dir()
        ]


def digest(bases: Sequence[Path]) -> str:
    """A hash of every file under the harness and its plugins, so a trial records its setup."""
    sha = hashlib.sha256()
    for base in bases:
        for path in _files(base):
            sha.update(f"{path.relative_to(base)}\0".encode())
            sha.update(path.read_bytes())
    return sha.hexdigest()[:16]


def _files(base: Path) -> Iterator[Path]:
    for path in sorted(base.rglob("*")):
        parts = path.relative_to(base).parts
        if path.is_file() and not any(p.startswith(".") or p in _UNHASHED for p in parts):
            yield path
