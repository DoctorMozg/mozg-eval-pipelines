import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.models.function import FunctionModel

from mozg_eval.events.models import FileChange, Message, Payload, ToolResult
from mozg_eval.grading.models import GradeResult
from mozg_eval.judge.cache import JudgeCache
from mozg_eval.judge.grading import JudgeSetupError, TrialJudge
from mozg_eval.judge.verdict import CriterionJudge
from mozg_eval.spec.models import Case

PAYLOADS: list[Payload] = [
    Message(role="assistant", text="Reading the script first."),
    ToolResult(id="1", ok=True, output="script: a fisherman coils a rope"),
    Message(role="assistant", text="Title: The Harbour Wakes\nNo twist here."),
]
type Judges = Callable[..., FunctionModel]


def case(*graders: dict[str, Any]) -> Case:
    named = [{"name": f"j{i}", "type": "judge", **g} for i, g in enumerate(graders)]
    return Case.model_validate(
        {
            "schema": "mzeval/2",
            "name": "c",
            "kind": "behaviour",
            "execution": {"prompt": "p"},
            "graders": named,
        }
    )


@pytest.fixture
def cache(tmp_path: Path) -> JudgeCache:
    return JudgeCache(tmp_path / "judge-cache")


def grade(
    judge: TrialJudge, the_case: Case, workspace: Path | None = None
) -> dict[str, GradeResult]:
    return {g.name: g for g in asyncio.run(judge.grade(the_case, PAYLOADS, workspace))}


class TestTrialJudge:
    def test_one_failed_item_fails_the_grader(
        self, quoting_judge: Judges, cache: JudgeCache
    ) -> None:
        the_case = case({"checklist": ["holds no twist", "invents no name"], "focus": "final"})
        judge = TrialJudge.for_cases([the_case], quoting_judge("pass", "fail"), cache)

        result = grade(judge, the_case)["j0"]

        assert result.verdict == "fail"
        assert result.detail == "1. pass: 'No twist here.'; 2. fail: 'No twist here.'"

    def test_unknown_item_leaves_the_grade_na(
        self, quoting_judge: Judges, cache: JudgeCache
    ) -> None:
        the_case = case({"checklist": ["a", "b"]})
        judge = TrialJudge.for_cases([the_case], quoting_judge("pass", "unknown"), cache)

        assert grade(judge, the_case)["j0"].verdict == "n/a"

    def test_without_any_model_the_grade_says_how_to_get_one(self, cache: JudgeCache) -> None:
        the_case = case({"checklist": ["a"]})

        result = grade(TrialJudge.for_cases([the_case], None, cache), the_case)["j0"]

        assert (result.verdict, result.detail) == (
            "n/a",
            "no judge model: pass --judge or set the grader's model",
        )

    def test_graders_own_model_wins_over_the_default(
        self, quoting_judge: Judges, cache: JudgeCache
    ) -> None:
        the_case = case({"checklist": ["a"]}, {"checklist": ["b"], "model": "strict"})
        strict = CriterionJudge(quoting_judge("fail"), cache)
        judge = TrialJudge(CriterionJudge(quoting_judge("pass"), cache), {"strict": strict})

        results = grade(judge, the_case)

        assert (results["j0"].verdict, results["j1"].verdict) == ("pass", "fail")

    def test_file_focus_reads_the_workspace_while_it_exists(
        self, quoting_judge: Judges, cache: JudgeCache, tmp_path: Path
    ) -> None:
        (tmp_path / "listing.md").write_text("Draft\nTitle: Dawn Pier\n")
        the_case = case({"checklist": ["a"], "focus": {"file": "listing.md"}})
        judge = TrialJudge.for_cases([the_case], quoting_judge("pass"), cache)

        live = grade(judge, the_case, tmp_path)["j0"]
        gone = grade(judge, the_case)["j0"]

        assert live.detail == "1. pass: 'Title: Dawn Pier'"
        assert (gone.verdict, gone.detail) == (
            "n/a",
            "the judge reads files, and the trial's workspace is gone",
        )

    def test_created_files_focus_reads_each_created_file(
        self, quoting_judge: Judges, cache: JudgeCache, tmp_path: Path
    ) -> None:
        (tmp_path / "new.md").write_text("draft listing")
        the_case = case({"checklist": ["a"], "focus": "created_files"})
        judge = TrialJudge.for_cases([the_case], quoting_judge("pass"), cache)
        created = FileChange(path=str(tmp_path / "new.md"), change="created")

        [result] = asyncio.run(judge.grade(the_case, [*PAYLOADS, created], tmp_path))

        assert result.detail == "1. pass: 'draft listing'"

    def test_unknown_model_name_is_a_setup_error(self, cache: JudgeCache) -> None:
        the_case = case({"checklist": ["a"], "model": "nonsense"})

        with pytest.raises(JudgeSetupError, match="nonsense"):
            TrialJudge.for_cases([the_case], None, cache)
