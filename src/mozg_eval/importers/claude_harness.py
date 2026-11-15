"""Copies an allowlist of a Claude config dir into a harness; logins and session data never move."""

import json
import logging
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import yaml

from mozg_eval.drivers.harness_parts import HARNESS_ROOT
from mozg_eval.spec.harness import HARNESS_FILENAME, HARNESS_SCHEMA, HarnessLoader, read_frontmatter

log = logging.getLogger(__name__)

_IGNORED = shutil.ignore_patterns(".*", "__pycache__", "node_modules")


class ClaudePart(StrEnum):
    # Settings beyond hooks stay out: `env` and `.claude.json` routinely hold tokens.
    instructions = "instructions"
    skills = "skills"
    agents = "agents"
    hooks = "hooks"


@dataclass
class ImportSummary:
    copied: dict[ClaudePart, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class ClaudeHarnessImporter:
    def __init__(self, config: Path, home: Path) -> None:
        self.config = config
        # Hook commands name their scripts in any of these spellings of the config dir.
        prefixes = [str(config.resolve())]
        if config.resolve().is_relative_to(home.resolve()):
            rel = config.resolve().relative_to(home.resolve()).as_posix()
            prefixes += [f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"]
        alternatives = "|".join(map(re.escape, prefixes))
        self._script_ref = re.compile(rf"(?:{alternatives})/([^\s\"'`;|&)]+)")

    def run(self, dest: Path, name: str, parts: Sequence[ClaudePart]) -> ImportSummary:
        """Writes the harness at `dest`, which must be new or empty, then checks it loads."""
        if dest.exists() and any(dest.iterdir()):
            raise FileExistsError(f"{dest} is not empty")
        dest.mkdir(parents=True, exist_ok=True)
        manifest = {"schema": HARNESS_SCHEMA, "name": name, "description": "Imported from Claude"}
        (dest / HARNESS_FILENAME).write_text(yaml.safe_dump(manifest, sort_keys=False))
        summary = ImportSummary()
        steps = {
            ClaudePart.instructions: self._instructions,
            ClaudePart.skills: self._skills,
            ClaudePart.agents: self._agents,
            ClaudePart.hooks: self._hooks,
        }
        for part in parts:
            summary.copied[part] = steps[part](dest, summary.warnings)
        HarnessLoader().load(dest)
        log.info("imported %s from %s into %s", dict(summary.copied), self.config, dest)
        return summary

    def _instructions(self, dest: Path, warnings: list[str]) -> int:
        sources = [self.config / "CLAUDE.md"] if (self.config / "CLAUDE.md").is_file() else []
        rules = self.config / "rules"
        sources += sorted(rules.rglob("*.md")) if rules.is_dir() else []
        for source in sources:
            # Harness instructions are flat, so a nested rule keeps its folders in its name.
            rel = source.relative_to(rules) if source.is_relative_to(rules) else Path(source.name)
            target = dest / "instructions" / "-".join(rel.parts)
            if target.exists():
                warnings.append(f"{source}: skipped, {target.name} was already taken")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return len(list((dest / "instructions").glob("*.md")))

    def _skills(self, dest: Path, warnings: list[str]) -> int:
        root = self.config / "skills"
        folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
        copied = 0
        for folder in folders:
            if not (folder / "SKILL.md").is_file():
                warnings.append(f"{folder}: skipped, no SKILL.md")
                continue
            shutil.copytree(folder, dest / "skills" / folder.name, ignore=_IGNORED)
            copied += 1
        return copied

    def _agents(self, dest: Path, warnings: list[str]) -> int:
        root = self.config / "agents"
        copied = 0
        for path in sorted(root.glob("*.md")) if root.is_dir() else []:
            meta, _ = read_frontmatter(path)
            if not {"name", "description"} <= meta.keys():
                warnings.append(f"{path}: skipped, frontmatter needs name and description")
                continue
            (dest / "agents").mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest / "agents" / path.name)
            copied += 1
        return copied

    def _hooks(self, dest: Path, warnings: list[str]) -> int:
        settings = self.config / "settings.json"
        hooks = json.loads(settings.read_text()).get("hooks") if settings.is_file() else None
        if not hooks:
            return 0
        count = 0
        for groups in hooks.values():
            for group in groups:
                for handler in group.get("hooks", []):
                    count += 1
                    if "command" in handler:
                        handler["command"] = self._move_scripts(handler["command"], dest, warnings)
        (dest / "hooks.json").write_text(json.dumps({"hooks": hooks}, indent=2) + "\n")
        return count

    def _move_scripts(self, command: str, dest: Path, warnings: list[str]) -> str:
        """Copies each script the command runs from the config dir and points it at the copy."""

        def move(match: re.Match[str]) -> str:
            rel = Path(match[1])
            source = self.config / rel
            if not source.is_file():
                warnings.append(f"hook names {match[0]}, which isn't a file; left as is")
                return match[0]
            target = dest / "scripts" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            return f"{HARNESS_ROOT}/scripts/{rel.as_posix()}"

        return self._script_ref.sub(move, command)
