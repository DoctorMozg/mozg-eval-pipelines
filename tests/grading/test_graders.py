from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mozg_eval.drivers.claude import ClaudeDriver, StreamLine, normalize
from mozg_eval.drivers.codex import CodexDriver
from mozg_eval.drivers.protocol import Capabilities
from mozg_eval.events.models import (
    CheckResult,
    End,
    EndStatus,
    Event,
    FileChange,
    HookFired,
    Message,
    Payload,
    ToolCall,
    ToolResult,
)
from mozg_eval.grading.graders import TrialGrader
from mozg_eval.grading.models import GradeResult, Verdict
from mozg_eval.grading.support import DriverSupport
from mozg_eval.spec.models import Case

SKILL_FIRE = Path(__file__).parents[1] / "drivers" / "fixtures" / "claude_skill_fire.jsonl"
T0 = datetime(2026, 11, 14, tzinfo=UTC)
CLAUDE = DriverSupport("claude", ClaudeDriver.capabilities)
CODEX = DriverSupport("codex", CodexDriver.capabilities)


def events_of(payloads: list[Payload]) -> list[Event]:
    return [
        Event(run="r", trial="t", seq=i, t=T0, payload=p) for i, p in enumerate(payloads, start=1)
    ]


@pytest.fixture(scope="module")
def recorded() -> list[Event]:
    """The recorded mz-memory trace: Skill memory-note, a denied AskUserQuestion, a reply."""
    lines = SKILL_FIRE.read_text().splitlines()
    payloads = [p for line in lines for p in normalize(StreamLine.model_validate_json(line))]
    return events_of(payloads)


def case(**fields: object) -> Case:
    base: dict[str, Any] = {
        "schema": "mzeval/2",
        "name": "c",
        "kind": "behaviour",
        "execution": {"prompt": "p"},
    }
    return Case.model_validate(base | fields)


def grade(
    events: list[Event], *graders: dict[str, object], **fields: object
) -> dict[str, GradeResult]:
    named = [{"name": f"g{i}", **g} for i, g in enumerate(graders)]
    results = TrialGrader(case(graders=named, **fields), CLAUDE, None).grade(events)
    return {r.name: r for r in results}


def verdicts(results: dict[str, GradeResult]) -> dict[str, str]:
    return {name: r.verdict for name, r in results.items()}


class TestRecordedTrace:
    def test_fire_case_passes_on_the_skill_that_fired(self, recorded: list[Event]) -> None:
        results = grade(recorded, kind="fire", expect={"skill": "mz-memory:memory-note"})

        assert results["expect"] == GradeResult(
            name="expect", verdict="pass", detail="fired: memory-note", arm="with"
        )

    def test_near_miss_fails_when_the_wrong_skill_fires(self, recorded: list[Event]) -> None:
        results = grade(
            recorded, kind="near-miss", expect={"skill": {"not": "memory-note", "owner": "recall"}}
        )

        assert results["expect"].verdict == "fail"

    def test_trace_graders_on_the_recorded_run(self, recorded: list[Event]) -> None:
        results = grade(
            recorded,
            {"type": "skill", "skill": "memory-note", "max": 1},
            {"type": "tool_called", "kind": "ask_user", "match": "Approve"},
            {"type": "tool_called", "kind": "shell", "min": 0, "max": 0},
            {"type": "hook_fired", "event": "SessionStart", "match": "Project memory"},
            {"type": "regex", "pattern": "(?i)approve", "target": "final", "min": 2},
            {"type": "regex", "pattern": "No such tool available", "target": "transcript"},
            {"type": "tool_order", "sequence": ["skill", "ask_user"]},
            {"type": "tool_order", "sequence": ["ask_user", "skill"]},
        )

        assert verdicts(results) == {
            "g0": "pass",
            "g1": "pass",
            "g2": "pass",
            "g3": "pass",
            "g4": "pass",
            "g5": "pass",
            "g6": "pass",
            "g7": "fail",
        }

    def test_recorded_trial_passes_overall(self, recorded: list[Event]) -> None:
        grades = list(grade(recorded, expect={"skill": "memory-note"}).values())

        assert TrialGrader.passed(recorded, grades)


class TestEventGraders:
    def test_file_changed_counts_any_tool_or_script(self) -> None:
        events = events_of(
            [
                FileChange(path="/ws/.mz/memory/MEMORY.md", change="created"),
                FileChange(path="/ws/notes.txt", change="modified"),
                End(status="ok"),
            ]
        )

        results = grade(
            events,
            {"type": "file_changed", "path_match": r"MEMORY\.md$"},
            {"type": "file_changed", "min": 3},
            {"type": "file_changed", "change": "created", "path_match": r"notes"},
        )

        assert verdicts(results) == {"g0": "pass", "g1": "fail", "g2": "fail"}

    def test_hook_fired_without_an_event_matches_any_hook(self) -> None:
        events = events_of([HookFired(event="UserPromptSubmit", output="pnpm exclusively")])

        results = grade(
            events,
            {"type": "hook_fired", "match": "pnpm"},
            {"type": "hook_fired", "event": "SessionStart", "match": "pnpm"},
        )

        assert verdicts(results) == {"g0": "pass", "g1": "fail"}

    def test_replies_and_tool_results_match_one_item_at_a_time(self) -> None:
        events = events_of(
            [
                Message(role="assistant", text="Panel ready for review"),
                ToolResult(id="1", ok=True, output="token ttl: 15 minutes"),
                Message(role="assistant", text="Not selected: lens-x"),
            ]
        )

        results = grade(
            events,
            {"type": "regex", "target": "replies", "pattern": "^Panel ready[\\s\\S]*Not selected"},
            {"type": "regex", "target": "replies", "pattern": "Not selected"},
            {"type": "regex", "target": "tool_results", "pattern": "15 minutes"},
            {"type": "regex", "target": "replies", "pattern": "15 minutes", "min": 0, "max": 0},
        )

        assert verdicts(results) == {"g0": "fail", "g1": "pass", "g2": "pass", "g3": "pass"}

    def test_tool_path_match_reads_the_file_path_argument(self) -> None:
        call = ToolCall(id="1", kind="write", name="Write", input={"file_path": "/ws/a.md"})

        results = grade(
            events_of([call]), {"type": "tool_called", "kind": "write", "path_match": r"\.md$"}
        )

        assert results["g0"].verdict == "pass"

    def test_live_graders_read_their_check_result(self) -> None:
        events = events_of(
            [End(status="ok"), CheckResult(name="g0", passed=False, detail="exit 1")]
        )

        results = grade(
            events,
            {"type": "command", "run": "pytest"},
            {"type": "file_exists", "path": "*.md"},
        )

        assert (results["g0"].verdict, results["g0"].detail) == ("fail", "exit 1")
        assert results["g1"].verdict == "n/a"

    def test_judge_and_other_drivers_raw_stream_are_not_applicable(self) -> None:
        results = grade(
            events_of([End(status="ok")]),
            {"type": "judge", "checklist": ["is polite"]},
            {"type": "raw_regex", "driver": "codex", "pattern": "x"},
        )

        assert verdicts(results) == {"g0": "n/a", "g1": "n/a"}

    def test_raw_regex_counts_over_the_raw_stream(self) -> None:
        graded = TrialGrader(
            case(
                graders=[{"name": "g", "type": "raw_regex", "driver": "claude", "pattern": "hook"}]
            ),
            CLAUDE,
            '{"type":"hook_started"}\n{"type":"hook_response"}\n',
        ).grade(events_of([End(status="ok")]))

        assert (graded[0].verdict, graded[0].detail) == ("pass", "2 matches")


class TestTrialVerdict:
    @pytest.mark.parametrize(
        ("status", "verdict", "passed"),
        [
            ("ok", "pass", True),
            ("ok", "n/a", True),
            ("ok", "fail", False),
            ("timeout", "pass", False),
        ],
    )
    def test_needs_a_clean_end_and_no_failed_grade(
        self, status: EndStatus, verdict: Verdict, passed: bool
    ) -> None:
        events = events_of([Message(role="assistant", text="hi"), End(status=status)])
        grades = [GradeResult(name="g", verdict=verdict)]

        assert TrialGrader.passed(events, grades) is passed

    def test_check_results_after_end_dont_hide_the_end(self) -> None:
        events = events_of(
            [End(status="error", reason="x"), ToolResult(id="1", ok=True, output="")]
        )

        assert not TrialGrader.passed(events, [])


class TestDriverSupport:
    """What a driver can't report grades n/a, never fail, and a pass needs real evidence."""

    APPLY_PATCH = ToolCall(
        id="p",
        kind="edit",
        name="apply_patch",
        input={"changes": [{"path": "/ws/.mz/notes.md", "kind": "add"}]},
    )

    def on(self, support: DriverSupport, *graders: dict[str, object]) -> dict[str, str]:
        named = [{"name": f"g{i}", **g} for i, g in enumerate(graders)]
        events = events_of([self.APPLY_PATCH, End(status="ok")])
        return verdicts(
            {r.name: r for r in TrialGrader(case(graders=named), support, None).grade(events)}
        )

    def test_codex_answers_none_of_the_graders_it_has_no_events_for(self) -> None:
        unanswerable: list[dict[str, object]] = [
            {"type": "subagent", "subagent": "researcher"},
            {"type": "tool_called", "kind": "write", "min": 0, "max": 0},
            {"type": "tool_called", "kind": "edit", "match": "old_string"},
            {"type": "tool_order", "sequence": ["read", "edit"]},
        ]

        assert set(self.on(CODEX, *unanswerable).values()) == {"n/a"}
        assert set(self.on(CLAUDE, *unanswerable).values()) == {"pass", "fail"}

    def test_path_match_reads_the_paths_of_an_apply_patch(self) -> None:
        verdict = self.on(
            CODEX, {"type": "tool_called", "kind": "edit", "path_match": r"\.mz/notes"}
        )

        assert verdict == {"g0": "pass"}

    def test_driver_without_skills_leaves_routing_ungraded(self) -> None:
        support = DriverSupport("bare", Capabilities())
        results = TrialGrader(
            case(kind="fire", expect={"skill": "memory-note"}), support, None
        ).grade(events_of([End(status="ok")]))

        assert (results[0].verdict, results[0].detail) == (
            "n/a",
            "bare reports no skill activations",
        )

    def test_case_is_applicable_when_one_assertion_can_be_answered(self) -> None:
        only_subagent = case(graders=[{"name": "s", "type": "subagent", "subagent": "x"}])
        routed = case(
            kind="fire",
            expect={"skill": "audit"},
            graders=[{"name": "s", "type": "subagent", "subagent": "x"}],
        )

        assert not CODEX.applicable(only_subagent)
        assert CODEX.gaps(only_subagent) == ["codex reports no subagent names"]
        assert CODEX.applicable(routed)
        assert CLAUDE.applicable(only_subagent)
