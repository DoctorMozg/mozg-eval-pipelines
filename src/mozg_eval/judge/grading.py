"""Grades a case's `judge` graders from a trial's events, and from files while they exist."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from pydantic_ai import UserError
from pydantic_ai.models import Model

from mozg_eval.events.models import FileChange, Payload
from mozg_eval.grading.graders import event_texts
from mozg_eval.grading.models import GradeResult, JudgeSpend
from mozg_eval.grading.models import Verdict as GradeVerdict
from mozg_eval.judge.cache import JudgeCache
from mozg_eval.judge.verdict import CriterionJudge, Verdict
from mozg_eval.spec.models import Case, FileTarget, JudgeGrader


def _evidence(
    grader: JudgeGrader, payloads: Sequence[Payload], workspace: Path | None
) -> str | None:
    """The text the judge reads, or None when it lives in files that are gone."""
    if grader.focus == "created_files" or isinstance(grader.focus, FileTarget):
        if workspace is None:
            return None
        if isinstance(grader.focus, FileTarget):
            path = workspace / grader.focus.file
            return path.read_text(errors="replace") if path.is_file() else ""
        created = [p.path for p in payloads if isinstance(p, FileChange) and p.change == "created"]
        return "\n\n".join(f"{path}:\n{Path(path).read_text(errors='replace')}" for path in created)
    return "\n\n".join(event_texts(grader.focus, payloads))


def _combined(grader: JudgeGrader, verdicts: list[Verdict]) -> GradeResult:
    found = {v.verdict for v in verdicts}
    verdict: GradeVerdict = "fail" if "fail" in found else "n/a" if "unknown" in found else "pass"
    detail = "; ".join(
        f"{i}. {v.verdict}: {v.evidence_quote[:80] or v.reasoning[:80]!r}"
        for i, v in enumerate(verdicts, 1)
    )
    return GradeResult(
        name=grader.name, verdict=verdict, detail=detail, weight=grader.weight, arm=grader.arm
    )


@dataclass(frozen=True)
class JudgedTrial:
    grades: list[GradeResult] = field(default_factory=list)
    spend: list[JudgeSpend] = field(default_factory=list)


class JudgeSetupError(ValueError):
    """A judge model that can't be used, such as an unknown name or a missing API key."""


class TrialJudge:
    """A grader's own `model` wins over the default; no model at all leaves the grade n/a."""

    def __init__(self, default: CriterionJudge | None, named: Mapping[str, CriterionJudge]) -> None:
        self.default = default
        self.named = named

    @classmethod
    def for_cases(
        cls, cases: Iterable[Case], default: Model | str | None, cache: JudgeCache
    ) -> Self:
        """Builds every judge up front, so a missing API key stops the run before any agent."""
        models = {
            g.model for c in cases for g in c.graders if isinstance(g, JudgeGrader) and g.model
        }
        try:
            named = {model: CriterionJudge(model, cache) for model in sorted(models)}
            return cls(None if default is None else CriterionJudge(default, cache), named)
        except UserError as exc:
            raise JudgeSetupError(str(exc)) from exc

    async def grade(
        self, case: Case, payloads: Sequence[Payload], workspace: Path | None
    ) -> JudgedTrial:
        judged = JudgedTrial()
        for grader in case.graders:
            if isinstance(grader, JudgeGrader):
                await self._grade(grader, payloads, workspace, judged)
        return judged

    async def _grade(
        self,
        grader: JudgeGrader,
        payloads: Sequence[Payload],
        workspace: Path | None,
        into: JudgedTrial,
    ) -> None:
        judge = self.named[grader.model] if grader.model else self.default

        def not_judged(why: str) -> None:
            into.grades.append(
                GradeResult(
                    name=grader.name,
                    verdict="n/a",
                    detail=why,
                    weight=grader.weight,
                    arm=grader.arm,
                )
            )

        if judge is None:
            return not_judged("no judge model: pass --judge or set the grader's model")
        if grader.baseline is not None:
            return not_judged("judging against a baseline isn't built yet")
        evidence = _evidence(grader, payloads, workspace)
        if evidence is None:
            return not_judged("the judge reads files, and the trial's workspace is gone")
        judgements = [await judge.judge(item, evidence) for item in grader.checklist]
        into.grades.append(_combined(grader, [j.verdict for j in judgements]))
        into.spend.extend(j.spend for j in judgements if j.spend is not None)
        return None


def merged(grades: Sequence[GradeResult], judged: Sequence[GradeResult]) -> list[GradeResult]:
    """Trace grades with the judge's grades in place of the placeholders of the same name."""
    by_name = {g.name: g for g in judged}
    return [by_name.get(g.name, g) for g in grades]
