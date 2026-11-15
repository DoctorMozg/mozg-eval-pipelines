"""Advice on valid cases that can pass for the wrong reason; warnings never fail a lint."""

import json
import logging
from pathlib import Path

import yaml

from mozg_eval.spec.loader import SpecIssue, line_map
from mozg_eval.spec.models import Case, HookFiredGrader

log = logging.getLogger(__name__)


class CaseLinter:
    def warnings(self, path: Path, case: Case) -> list[SpecIssue]:
        issues: list[SpecIssue] = []
        events: set[str] = set()
        for bundle in case.bundle:
            hooks = path.parent / bundle / "hooks" / "hooks.json"
            if not hooks.is_file():
                continue
            try:
                events |= set(json.loads(hooks.read_text(encoding="utf-8")).get("hooks", {}))
            except json.JSONDecodeError as exc:
                issues.append(
                    SpecIssue(path, None, f"{bundle}/hooks/hooks.json isn't valid JSON: {exc}")
                )
        if len(events) < 2:
            return issues
        # Without an event, a grader meant for one hook also passes when an unrelated hook fires.
        lines = line_map(yaml.compose(path.read_text(encoding="utf-8"), Loader=yaml.SafeLoader))
        named = ", ".join(sorted(events))
        for index, grader in enumerate(case.graders):
            if isinstance(grader, HookFiredGrader) and grader.event is None:
                message = (
                    f"hook_fired grader {grader.name} has no event, so any of the bundle's hooks"
                    f" ({named}) passes it"
                )
                issues.append(SpecIssue(path, lines.get(("graders", index), 1), message))
        log.debug("%s: %d lint warnings", path, len(issues))
        return issues
