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


@dataclass(frozen=True)
class SpecIssue:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


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
            lines = self._line_map(yaml.compose(text, Loader=yaml.SafeLoader))
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
            issues = [self._issue(path, error, lines) for error in exc.errors()]
            raise CaseLoadError(sorted(issues, key=lambda issue: issue.line)) from exc
        log.debug("loaded case %s from %s", case.name, path)
        return case

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

    def _issue(self, path: Path, error: ErrorDetails, lines: dict[Loc, int]) -> SpecIssue:
        loc = self._data_loc(error["loc"], lines)
        where = ".".join(str(part) for part in error["loc"]) or "<case>"
        if error["type"] == "extra_forbidden":
            message = (
                f"unknown key '{error['loc'][-1]}' in {'.'.join(map(str, loc[:-1])) or '<case>'}"
            )
        else:
            message = f"{where}: {error['msg']}"
        return SpecIssue(path, lines.get(loc, 1), message)

    @staticmethod
    def _data_loc(loc: tuple[str | int, ...], lines: dict[Loc, int]) -> Loc:
        # Pydantic adds union tags (the grader type) to loc; keep only parts found in the YAML.
        found: Loc = ()
        for part in loc:
            if (*found, part) in lines:
                found = (*found, part)
        return found

    def _line_map(self, node: yaml.Node | None, loc: Loc = ()) -> dict[Loc, int]:
        lines: dict[Loc, int] = {}
        if node is None:
            return lines
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                child = (*loc, str(key_node.value))
                lines[child] = key_node.start_mark.line + 1
                lines.update(self._line_map(value_node, child))
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                child = (*loc, index)
                lines[child] = item.start_mark.line + 1
                lines.update(self._line_map(item, child))
        return lines
