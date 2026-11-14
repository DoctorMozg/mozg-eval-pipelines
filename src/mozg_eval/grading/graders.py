"""Trace graders: pure functions of a trial's stored events; live runs and replays grade alike."""

import json
import re
from collections.abc import Sequence
from itertools import pairwise

from pydantic import JsonValue

from mozg_eval.events.models import (
    CheckResult,
    End,
    Event,
    FileChange,
    HookFired,
    Message,
    Payload,
    SkillActivated,
    SubagentSpawned,
    ToolCall,
    ToolResult,
)
from mozg_eval.grading.models import GradeResult, Verdict
from mozg_eval.runner.checks import in_range, needs_workspace
from mozg_eval.spec.models import (
    Case,
    FileChangedGrader,
    Grader,
    GraderBase,
    HookFiredGrader,
    JudgeGrader,
    RawRegexGrader,
    RegexGrader,
    SkillGrader,
    SkillNotExpected,
    SubagentGrader,
    ToolCalledGrader,
    ToolOrderGrader,
    ToolRef,
    regex_flags,
)

EXPECT = "expect"
_PATH_KEYS = ("file_path", "path", "notebook_path")


def _bare(name: str) -> str:
    return name.rpartition(":")[2]


def _compact(value: dict[str, JsonValue]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _matches_call(call: ToolCall, step: ToolRef) -> bool:
    return call.kind == step.kind and (
        step.match is None or bool(re.search(step.match, _compact(call.input)))
    )


class TrialGrader:
    """Grades one trial of a case; `raw` is the driver's raw stream, for `raw_regex`."""

    def __init__(self, case: Case, driver: str, raw: str | None) -> None:
        self.case = case
        self.driver = driver
        self.raw = raw

    def grade(self, events: Sequence[Event]) -> list[GradeResult]:
        payloads = [e.payload for e in events]
        results = [self._expect(payloads)] if self.case.expect.skill is not None else []
        for grader in self.case.graders:
            verdict, detail = self._grade(grader, payloads)
            results.append(
                GradeResult(
                    name=grader.name,
                    verdict=verdict,
                    detail=detail,
                    weight=grader.weight,
                    arm=grader.arm,
                )
            )
        return results

    @staticmethod
    def passed(events: Sequence[Event], grades: Sequence[GradeResult]) -> bool:
        """A trial passes when it ended cleanly and no applicable grade failed."""
        end = next(e.payload for e in reversed(events) if isinstance(e.payload, End))
        return end.status == "ok" and all(g.verdict != "fail" for g in grades)

    def _expect(self, payloads: list[Payload]) -> GradeResult:
        fired = [p.skill for p in payloads if isinstance(p, SkillActivated)]
        detail = f"fired: {', '.join(fired) or 'none'}"
        expected = self.case.expect.skill
        if isinstance(expected, SkillNotExpected):
            ok = _bare(expected.not_) not in fired and (
                expected.owner is None or _bare(expected.owner) in fired
            )
        else:
            ok = expected is not None and _bare(expected) in fired
        verdict: Verdict = "pass" if ok else "fail"
        return GradeResult(name=EXPECT, verdict=verdict, detail=detail, arm=self.case.expect.arm)

    def _grade(self, grader: Grader, payloads: list[Payload]) -> tuple[Verdict, str]:
        if needs_workspace(grader):
            check = next(
                (p for p in payloads if isinstance(p, CheckResult) and p.name == grader.name),
                None,
            )
            if check is None:
                return "n/a", "no workspace check was recorded"
            return ("pass" if check.passed else "fail"), check.detail
        if isinstance(grader, JudgeGrader):
            return "n/a", "the LLM judge arrives in milestone 11"
        if isinstance(grader, ToolOrderGrader):
            return self._tool_order(grader, payloads)
        if isinstance(grader, RawRegexGrader) and (
            grader.driver != self.driver or self.raw is None
        ):
            return "n/a", f"raw stream of driver {grader.driver} not available"
        if not isinstance(grader, GraderBase):
            raise ValueError(f"grader {grader.name} ({grader.type}) has no count to check")
        count = self._count(grader, payloads)
        return ("pass" if in_range(count, grader) else "fail"), f"{count} matches"

    def _count(self, grader: GraderBase, payloads: list[Payload]) -> int:
        match grader:
            case SkillGrader():
                return sum(
                    isinstance(p, SkillActivated) and p.skill == _bare(grader.skill)
                    for p in payloads
                )
            case SubagentGrader():
                return sum(
                    isinstance(p, SubagentSpawned) and p.subagent == _bare(grader.subagent)
                    for p in payloads
                )
            case ToolCalledGrader():
                return sum(
                    isinstance(p, ToolCall) and self._tool_matches(grader, p) for p in payloads
                )
            case HookFiredGrader():
                return sum(
                    isinstance(p, HookFired)
                    and p.event == grader.event
                    and (grader.match is None or bool(re.search(grader.match, p.output)))
                    for p in payloads
                )
            case FileChangedGrader():
                return sum(
                    isinstance(p, FileChange)
                    and (grader.path_match is None or bool(re.search(grader.path_match, p.path)))
                    for p in payloads
                )
            case RegexGrader():
                text = self._text(grader, payloads)
                return len(re.findall(grader.pattern, text, regex_flags(grader.flags)))
            case RawRegexGrader():
                return len(re.findall(grader.pattern, self.raw or "", regex_flags(grader.flags)))
        raise ValueError(f"no counter for grader {grader.name} ({type(grader).__name__})")

    @staticmethod
    def _tool_matches(grader: ToolCalledGrader, call: ToolCall) -> bool:
        if call.kind != grader.kind:
            return False
        if grader.match is not None and not re.search(grader.match, _compact(call.input)):
            return False
        if grader.path_match is None:
            return True
        path = next((call.input[k] for k in _PATH_KEYS if isinstance(call.input.get(k), str)), "")
        return bool(re.search(grader.path_match, str(path)))

    @staticmethod
    def _text(grader: RegexGrader, payloads: list[Payload]) -> str:
        if grader.target == "final":
            replies = [p.text for p in payloads if isinstance(p, Message) and p.role == "assistant"]
            return replies[-1] if replies else ""
        # transcript: what was said and what the tools returned, in order
        return "\n".join(
            p.text if isinstance(p, Message) else p.output
            for p in payloads
            if isinstance(p, Message | ToolResult)
        )

    @staticmethod
    def _tool_order(grader: ToolOrderGrader, payloads: list[Payload]) -> tuple[Verdict, str]:
        calls = [p for p in payloads if isinstance(p, ToolCall)]
        firsts: list[int] = []
        for step in grader.sequence:
            ref = step if isinstance(step, ToolRef) else ToolRef(kind=step, match=None)
            index = next((i for i, call in enumerate(calls) if _matches_call(call, ref)), None)
            if index is None:
                return "fail", f"no {ref.kind} call matches step {len(firsts) + 1}"
            firsts.append(index)
        ordered = all(a < b for a, b in pairwise(firsts))
        return ("pass" if ordered else "fail"), f"first calls at {firsts}"
