"""Converts `claude plugin eval` 1.1 cases (case.yaml) into mzeval/2 cases."""

import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from mozg_eval.drivers.claude import CLAUDE_TOOL_KINDS
from mozg_eval.importers.claude_wire import (
    Rewrite,
    ignoring_case,
    rewrite_trace,
    rewrite_write_input,
)
from mozg_eval.spec.loader import CASE_FILENAME
from mozg_eval.spec.models import SCHEMA, Case

log = logging.getLogger(__name__)

SOURCE_FILENAME = "case.yaml"

_NAMED_INPUT = r'^"{key}"\\s\*:\\s\*"\(\?:\[\\w-\]\+:\)\?([\w-]+)"$'
_SKILL_INPUT = re.compile(_NAMED_INPUT.format(key="skill"))
_SUBAGENT_INPUT = re.compile(_NAMED_INPUT.format(key="subagent_type"))
_FILE_PATH_PREFIX = '"file_path"\\s*:\\s*"'
_KIND_TAGS = {
    "fire": "fire",
    "positive": "fire",
    "near-miss": "near-miss",
    "negative": "near-miss",
    "gate": "gate",
    "trap": "trap",
    "behavior": "behaviour",
    "args": "behaviour",
    "hooks": "behaviour",
}

# 1.1 data is untyped YAML; these aliases mark where it enters before validation.
type RawCase = dict[str, Any]
type RawGrader = dict[str, Any]


class CaseImportError(Exception):
    """A 1.1 case that can't be converted at all."""


@dataclass(frozen=True)
class Flag:
    """A converted grader whose meaning may have shifted and needs a human look."""

    case: Path
    grader: str
    reason: str


@dataclass
class Conversion:
    case: RawCase
    flags: list[Flag] = field(default_factory=list)
    rules: Counter[str] = field(default_factory=Counter)


@dataclass
class ImportSummary:
    written: list[Path] = field(default_factory=list)
    existing: list[Path] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    rules: Counter[str] = field(default_factory=Counter)


class ClaudePluginEvalImporter:
    def import_tree(
        self, roots: Sequence[Path], out: Path | None = None, *, force: bool = False
    ) -> ImportSummary:
        """Write a v2 case for every 1.1 case under `roots`, next to it or mirrored under `out`."""
        summary = ImportSummary()
        for root in roots:
            for case_file in sorted(root.rglob(SOURCE_FILENAME)):
                conversion = self.convert(case_file)
                relative = case_file.parent.relative_to(root)
                target_dir = out / root.name / relative if out else case_file.parent
                target = target_dir / CASE_FILENAME
                if target.exists() and not force:
                    summary.existing.append(target)
                    continue
                if out:
                    self._absolute_paths(conversion.case, case_file.parent)
                target_dir.mkdir(parents=True, exist_ok=True)
                target.write_text(_dump_yaml(conversion.case), encoding="utf-8")
                summary.written.append(target)
                summary.flags.extend(conversion.flags)
                summary.rules.update(conversion.rules)
        log.info("imported %d cases, %d flags", len(summary.written), len(summary.flags))
        return summary

    @staticmethod
    def _absolute_paths(case: RawCase, source_dir: Path) -> None:
        # A case written elsewhere must still reach its plugins and fixture script.
        for key in ("bundle", "add_dirs"):
            if key in case:
                case[key] = [(source_dir / item).resolve().as_posix() for item in case[key]]
        for key in ("fixture", "history_file"):
            if key in case:
                case[key] = (source_dir / case[key]).resolve().as_posix()

    def convert(self, case_file: Path) -> Conversion:
        raw = yaml.safe_load(case_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != "1.1":
            raise CaseImportError(f"{case_file}: not a claude plugin eval 1.1 case")
        if (case_file.parent / "prompt.md").exists():
            raise CaseImportError(f"{case_file}: the prompt.md layout is not supported yet")
        return _CaseConverter(case_file, raw).run()


class _CaseConverter:
    def __init__(self, case_file: Path, raw: RawCase) -> None:
        self.case_file = case_file
        self.raw = raw
        self.result = Conversion(case={})

    def run(self) -> Conversion:
        raw = self.raw
        graders = list(raw.get("graders") or [])
        kind = self._kind(graders)
        expect, graders, kind = self._expect(kind, graders)

        case: RawCase = {"schema": SCHEMA, "name": raw["name"]}
        for key in ("description", "expected_outcome"):
            if raw.get(key):
                case[key] = raw[key]
        case["kind"] = kind
        if raw.get("tags"):
            case["tags"] = raw["tags"]
        if expect:
            case["expect"] = expect
        case["bundle"] = raw.get("plugins") or self._enclosing_plugin()
        context = raw.get("context") or {}
        if "scaffold_script" in context:
            case["fixture"] = context["scaffold_script"]
        if "history_file" in context:
            case["history_file"] = context["history_file"]
        if "add_dirs" in context:
            case["add_dirs"] = context["add_dirs"]
        case["execution"] = self._execution(raw.get("execution") or {})
        converted = [g for grader in graders for g in self._grader(grader)]
        if converted:
            case["graders"] = converted

        try:
            Case.model_validate(case)
        except ValidationError as exc:
            raise CaseImportError(
                f"{self.case_file}: the converted case is invalid:\n{exc}"
            ) from exc
        self.result.case = case
        return self.result

    def _note(self, rule: str) -> None:
        self.result.rules[rule] += 1

    def _flag(self, grader: str, reason: str) -> None:
        self.result.flags.append(Flag(self.case_file, grader, reason))

    def _kind(self, graders: list[RawGrader]) -> str:
        for tag in self.raw.get("tags") or []:
            if tag in _KIND_TAGS:
                return _KIND_TAGS[tag]
        skill_graders = [g for g in graders if g["type"] == "tool_used" and g["tool"] == "Skill"]
        if any(g.get("max") == 0 for g in skill_graders):
            return "near-miss"
        return "fire" if skill_graders else "behaviour"

    def _expect(
        self, kind: str, graders: list[RawGrader]
    ) -> tuple[dict[str, Any], list[RawGrader], str]:
        """Lift the routing graders of fire and near-miss cases into `expect.skill`."""
        if kind not in ("fire", "near-miss"):
            return {}, graders, kind
        silent = self._routing_grader(graders, silent=True)
        fires = self._routing_grader(graders, silent=False)
        lifted: list[RawGrader] = []
        if kind == "fire" and fires:
            expect: dict[str, Any] = {"skill": self._skill_name(fires)}
            lifted = [fires]
        elif kind == "near-miss" and silent:
            skill: dict[str, str] = {"not": self._skill_name(silent)}
            lifted = [silent]
            if fires and _arm(fires) == _arm(silent):
                skill["owner"] = self._skill_name(fires)
                lifted.append(fires)
            expect = {"skill": skill}
        else:
            self._flag(
                "<case>",
                f"tagged {kind} but has no routing grader to label it; imported as behaviour",
            )
            return {}, graders, "behaviour"

        if _arm(lifted[0]) == "both":
            expect["arm"] = "both"
        remaining = [g for g in graders if all(g is not lifted_grader for lifted_grader in lifted)]
        for grader in lifted:
            self._note("tool_used Skill -> expect.skill")
            fires_with_limits = grader.get("max", 1) != 0 and (
                grader.get("max") is not None or grader.get("min", 1) > 1
            )
            if fires_with_limits:
                # expect.skill only says "fires"; a count such as "at most twice" stays a grader.
                self._note("routing count limits kept as a skill grader")
                remaining.append(grader)
        return expect, remaining, kind

    @staticmethod
    def _routing_grader(graders: list[RawGrader], *, silent: bool) -> RawGrader | None:
        for grader in graders:
            if grader["type"] != "tool_used" or grader["tool"] != "Skill":
                continue
            if not _SKILL_INPUT.match(grader.get("input_match", "")):
                continue
            if float(grader.get("weight", 1)) != 1:
                continue
            if (grader.get("max") == 0) == silent:
                return grader
        return None

    @staticmethod
    def _skill_name(grader: RawGrader) -> str:
        match = _SKILL_INPUT.match(grader["input_match"])
        assert match is not None  # _routing_grader only returns graders whose pattern matched
        return match.group(1)

    def _enclosing_plugin(self) -> list[str]:
        for parent in self.case_file.parents:
            if (parent / ".claude-plugin" / "plugin.json").is_file():
                self._note("nearest enclosing plugin -> bundle")
                return [Path(*[".."] * _depth(self.case_file.parent, parent)).as_posix()]
        self._flag("<case>", "no plugins: and no enclosing plugin; bundle left empty")
        return []

    def _execution(self, raw: dict[str, Any]) -> dict[str, Any]:
        execution: dict[str, Any] = {"prompt": raw.get("prompt", "")}
        tools = []
        for tool in raw.get("allowed_tools") or []:
            if tool not in CLAUDE_TOOL_KINDS:
                raise CaseImportError(f"{self.case_file}: no canonical tool kind for {tool!r}")
            tools.append(CLAUDE_TOOL_KINDS[tool])
        if tools:
            execution["tools"] = tools
        renames = {"max_turns": "max_turns", "timeout_seconds": "timeout_s"}
        for old, new in renames.items():
            if old in raw:
                execution[new] = raw[old]
        if "runs" in self.raw:
            execution["runs"] = self.raw["runs"]
        for key in ("model", "append_system_prompt", "env"):
            if key in raw:
                execution[key] = raw[key]
        return execution

    def _grader(self, grader: RawGrader) -> list[dict[str, Any]]:
        convert = {
            "tool_used": self._tool_used,
            "regex": self._regex,
            "file_exists": self._file_exists,
            "llm": self._llm,
            "baseline": self._llm,
            "tool_order": self._tool_order,
        }.get(grader["type"])
        if convert is None:
            raise CaseImportError(f"{self.case_file}: unknown grader type {grader['type']!r}")
        result = convert(grader)
        converted = []
        for part in result if isinstance(result, list) else [result]:
            suffix = part.pop("suffix", "")
            named = {"name": grader["name"] + suffix, **part}
            default_arm = "with" if named["type"] == "skill" else "both"
            arm = _arm(grader)
            if arm != default_arm:
                named["arm"] = arm
            if float(grader.get("weight", 1)) != 1:
                named["weight"] = grader["weight"]
            converted.append(named)
        return converted

    def _rewritten(
        self, name: str, rewrite: Rewrite, limits: dict[str, Any]
    ) -> list[dict[str, Any]]:
        self._note(rewrite.rule)
        self._flag(
            name, "rewritten by shape from a regex over Claude's JSON; check the new pattern"
        )
        return [g | limits for g in rewrite.graders]

    def _tool_used(self, grader: RawGrader) -> dict[str, Any] | list[dict[str, Any]]:
        tool = grader["tool"]
        if tool not in CLAUDE_TOOL_KINDS:
            raise CaseImportError(f"{self.case_file}: no canonical tool kind for {tool!r}")
        kind = CLAUDE_TOOL_KINDS[tool]
        pattern = grader.get("input_match", "")
        limits: dict[str, Any] = {k: grader[k] for k in ("min", "max") if k in grader}
        named = {"skill": _SKILL_INPUT, "subagent": _SUBAGENT_INPUT}.get(kind)
        if named and (match := named.match(pattern)):
            self._note(f"tool_used {tool} -> {kind}")
            return {"type": kind, kind: match.group(1), **limits}
        if pattern.startswith(_FILE_PATH_PREFIX) and kind in ("write", "edit"):
            # Files written through Bash or a script count too, so the check holds on every agent.
            self._note(f"tool_used {tool} file_path -> file_changed")
            return {"type": "file_changed", "path_match": _path_regex(pattern), **limits}
        if pattern.startswith(_FILE_PATH_PREFIX):
            self._note(f"tool_used {tool} file_path -> tool_called path_match")
            return {
                "type": "tool_called",
                "kind": kind,
                "path_match": _path_regex(pattern),
                **limits,
            }
        if (
            pattern
            and kind in ("write", "edit")
            and (rewrite := rewrite_write_input(pattern, absent=limits.get("max") == 0))
        ):
            return self._rewritten(grader["name"], rewrite, limits)
        converted: dict[str, Any] = {"type": "tool_called", "kind": kind, **limits}
        if pattern:
            converted["match"] = pattern
            self._flag(
                grader["name"], f"{tool} input_match is a regex over Claude's tool input JSON"
            )
        self._note(f"tool_used {tool} -> tool_called")
        return converted

    def _regex(self, grader: RawGrader) -> dict[str, Any] | list[dict[str, Any]]:
        mode = str(grader.get("match", "contains"))
        limits = _match_limits(mode)
        flags = {"flags": grader["flags"]} if grader.get("flags") else {}
        target = grader.get("target", "last_message")
        # count:N counts regex matches, which a file or hook count can't stand in for. The `m`
        # flag only re-anchored `^` to a stream line, and a rewrite anchors at a message or file.
        raw_flags = str(grader.get("flags", ""))
        portable = (
            target == "trace" and set(raw_flags) <= set("im") and not mode.startswith("count:")
        )
        if portable and (rewrite := rewrite_trace(grader["pattern"])):
            if "i" in raw_flags:
                rewrite = ignoring_case(rewrite)
            return self._rewritten(grader["name"], rewrite, limits)
        if target == "files":
            return self._created_paths(grader, limits)
        if target == "trace":
            self._note("regex trace -> raw_regex claude")
            self._flag(grader["name"], "regex over Claude's trace: kept as raw_regex, Claude only")
            return {
                "type": "raw_regex",
                "driver": "claude",
                "pattern": grader["pattern"],
                **flags,
                **limits,
            }
        self._note("regex -> regex")
        return {
            "type": "regex",
            "pattern": grader["pattern"],
            "target": self._target(target),
            **flags,
            **limits,
        }

    def _created_paths(self, grader: RawGrader, limits: dict[str, int]) -> dict[str, Any]:
        """1.1 `target: files` matches created paths, not contents; each path is checked alone."""
        self._note("regex files -> file_changed created path_match")
        # `m` only re-anchored `^` to each line of the joined path list.
        inline = str(grader.get("flags", "")).replace("m", "")
        pattern = f"(?{inline}){grader['pattern']}" if inline else grader["pattern"]
        if grader["pattern"].startswith("^"):
            self._flag(grader["name"], "path_match runs over absolute paths; check the anchors")
        return {"type": "file_changed", "change": "created", "path_match": pattern, **limits}

    def _target(self, target: str | dict[str, str]) -> str | dict[str, str]:
        if isinstance(target, dict):
            if target.get("source") != "file":
                raise CaseImportError(f"{self.case_file}: unsupported regex target {target!r}")
            return {"file": target["path"]}
        targets = {"last_message": "final", "files": "created_files"}
        if target not in targets:
            raise CaseImportError(f"{self.case_file}: unsupported regex target {target!r}")
        return targets[target]

    def _file_exists(self, grader: RawGrader) -> dict[str, Any]:
        self._note("file_exists -> file_exists")
        converted: dict[str, Any] = {"type": "file_exists", "path": grader["path"]}
        if grader.get("exists") is False:
            converted["exists"] = False
        return converted

    def _llm(self, grader: RawGrader) -> dict[str, Any]:
        self._note(f"{grader['type']} -> judge")
        self._flag(grader["name"], "criteria imported as one checklist item; split it by hand")
        converted: dict[str, Any] = {"type": "judge", "checklist": [grader["criteria"].strip()]}
        # 1.1 documents no focus default for llm; last_message is the only default it states.
        converted["focus"] = self._target(grader["focus"]) if "focus" in grader else "final"
        if grader["type"] == "baseline":
            converted["baseline"] = grader["baseline_file"]
        return converted

    def _tool_order(self, grader: RawGrader) -> dict[str, Any]:
        self._note("tool_order -> tool_order")
        steps: list[str | dict[str, str]] = []
        for step in (grader["before"], grader["after"]):
            if isinstance(step, str):
                steps.append(CLAUDE_TOOL_KINDS[step])
            else:
                steps.append(
                    {"kind": CLAUDE_TOOL_KINDS[step["tool"]], "match": step["input_match"]}
                )
                self._flag(
                    grader["name"], "tool_order input_match is over Claude's tool input JSON"
                )
        return {"type": "tool_order", "sequence": steps}


def _arm(grader: RawGrader) -> str:
    arm = grader.get("arm")
    if arm is None:
        # 1.1 leaves Skill graders out of two-arm scores unless told otherwise.
        return (
            "with"
            if grader.get("type") == "tool_used" and grader.get("tool") == "Skill"
            else "both"
        )
    return {"with-only": "with", "both": "both"}[arm]


def _match_limits(match: str) -> dict[str, int]:
    if match == "contains":
        return {}
    if match == "not_contains":
        return {"min": 0, "max": 0}
    if match.startswith("count:") and match[6:].isdigit():
        count = int(match[6:])
        return {"min": count, "max": count}
    raise CaseImportError(f"unknown regex match mode {match!r}")


def _path_regex(input_match: str) -> str:
    """Turn a regex over `"file_path": "..."` into one over the path itself."""
    body = input_match.removeprefix(_FILE_PATH_PREFIX)
    if body.endswith('"') and not body.endswith('\\"'):
        body = body[:-1] + "$"
    return "^" + body


def _depth(child: Path, ancestor: Path) -> int:
    return len(child.relative_to(ancestor).parts)


class _BlockStyleDumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_BlockStyleDumper.add_representer(str, _represent_str)


def _dump_yaml(case: RawCase) -> str:
    return yaml.dump(case, Dumper=_BlockStyleDumper, sort_keys=False, allow_unicode=True, width=100)


def render_report(summary: ImportSummary) -> str:
    lines = [
        "# Migration report: claude plugin eval 1.1 -> mzeval/2",
        "",
        f"- Cases written: {len(summary.written)}",
        f"- Skipped, target already exists: {len(summary.existing)}",
        f"- Graders flagged for review: {len(summary.flags)}",
        "",
        "## Conversion rules applied",
        "",
        "| Rule | Count |",
        "| --- | --- |",
        *(f"| {rule} | {count} |" for rule, count in summary.rules.most_common()),
        "",
        "## Flagged graders",
        "",
        "| Case | Grader | Why |",
        "| --- | --- | --- |",
        *(f"| `{flag.case}` | `{flag.grader}` | {flag.reason} |" for flag in summary.flags),
    ]
    if summary.existing:
        lines += ["", "## Skipped", "", *(f"- `{path}`" for path in summary.existing)]
    return "\n".join(lines) + "\n"
