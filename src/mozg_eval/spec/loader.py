import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from mozg_eval.spec.models import Case

log = logging.getLogger(__name__)

CASE_FILENAME = "case.mzeval.yaml"

type Loc = tuple[str | int, ...]


def line_map(node: yaml.Node | None, loc: Loc = ()) -> dict[Loc, int]:
    """The 1-based line of every key and list item in a composed YAML tree, by its path."""
    lines: dict[Loc, int] = {}
    if node is None:
        return lines
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            child = (*loc, str(key_node.value))
            lines[child] = key_node.start_mark.line + 1
            lines.update(line_map(value_node, child))
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            child = (*loc, index)
            lines[child] = item.start_mark.line + 1
            lines.update(line_map(item, child))
    return lines


@dataclass(frozen=True)
class SpecIssue:
    path: Path
    line: int | None  # None for a problem with a whole file or folder
    message: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line is not None else str(self.path)
        return f"{where}: {self.message}"


def validation_issue(
    path: Path, error: ErrorDetails, lines: dict[Loc, int], what: str
) -> SpecIssue:
    """A pydantic error as a file:line issue; `what` names the document root in messages."""
    # Pydantic adds union tags (a grader type, a server kind) to loc; keep the YAML's parts.
    found: Loc = ()
    for part in error["loc"]:
        if (*found, part) in lines:
            found = (*found, part)
    if error["type"] == "extra_forbidden":
        where = ".".join(map(str, found[:-1])) or what
        message = f"unknown key '{error['loc'][-1]}' in {where}"
    else:
        message = f"{'.'.join(map(str, error['loc'])) or what}: {error['msg']}"
    return SpecIssue(path, lines.get(found, 1), message)


class CaseLoadError(Exception):
    def __init__(self, issues: Sequence[SpecIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))


class CaseLoader:
    """Loads v2 case files and reports every problem with its file and line."""

    def load(self, path: Path) -> Case:
        text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
            lines = line_map(yaml.compose(text, Loader=yaml.SafeLoader))
        except yaml.MarkedYAMLError as exc:
            mark = exc.problem_mark or exc.context_mark
            line = mark.line + 1 if mark else 1
            raise CaseLoadError([SpecIssue(path, line, f"invalid YAML: {exc.problem}")]) from exc

        if not isinstance(data, dict):
            raise CaseLoadError([SpecIssue(path, 1, "a case file must be a YAML mapping")])
        if data.get("schema_version") == "1.1":
            message = "this is a claude plugin eval 1.1 case; convert it with `mzeval import`"
            raise CaseLoadError([SpecIssue(path, lines.get(("schema_version",), 1), message)])

        try:
            case = Case.model_validate(data)
        except ValidationError as exc:
            issues = [validation_issue(path, error, lines, "<case>") for error in exc.errors()]
            raise CaseLoadError(sorted(issues, key=lambda issue: issue.line or 0)) from exc
        if missing := self._missing_paths(path, case, lines):
            raise CaseLoadError(missing)
        log.debug("loaded case %s from %s", case.name, path)
        return case

    @staticmethod
    def _missing_paths(path: Path, case: Case, lines: dict[Loc, int]) -> list[SpecIssue]:
        """Bundles and the fixture resolve against the case folder, so a typo fails at load."""
        issues: list[SpecIssue] = []
        for index, bundle in enumerate(case.bundle):
            if not (path.parent / bundle).is_dir():
                message = f"bundle {bundle}: no such directory next to the case"
                issues.append(SpecIssue(path, lines.get(("bundle", index), 1), message))
        if case.fixture and not (path.parent / case.fixture).is_file():
            message = f"fixture {case.fixture}: no such file next to the case"
            issues.append(SpecIssue(path, lines.get(("fixture",), 1), message))
        return issues

    @staticmethod
    def discover(roots: Sequence[Path]) -> list[Path]:
        """Expand directories to the case files under them; files are taken as given."""
        found: list[Path] = []
        for root in roots:
            if root.is_dir():
                found.extend(sorted(root.rglob(CASE_FILENAME)))
            else:
                found.append(root)
        return found
