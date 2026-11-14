import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from mozg_eval.judge.cache import JudgeCache
from mozg_eval.judge.verdict import CriterionJudge, Verdict

EVIDENCE = "Title: The Harbour Wakes\nThe film holds no twist,\n  so the title promises none."
CRITERION = "PASS if the reply says the film holds no twist."


class ScriptedJudge:
    """A model that answers each request with the next of its verdicts and counts the requests."""

    def __init__(self, *quotes: str, verdict: str = "pass") -> None:
        self.quotes = list(quotes)
        self.verdict = verdict
        self.calls = 0

    def __call__(self, _messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        quote = self.quotes[min(self.calls, len(self.quotes) - 1)]
        self.calls += 1
        args = {"reasoning": "checked", "verdict": self.verdict, "evidence_quote": quote}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])


@pytest.fixture
def cache(tmp_path: Path) -> JudgeCache:
    return JudgeCache(tmp_path / "judge-cache")


def judge(script: ScriptedJudge, cache: JudgeCache, criterion: str = CRITERION) -> Verdict:
    return asyncio.run(CriterionJudge(FunctionModel(script), cache).judge(criterion, EVIDENCE))


class TestEvidence:
    def test_made_up_quote_is_retried_then_ends_unknown_and_uncached(
        self, cache: JudgeCache
    ) -> None:
        script = ScriptedJudge("The twist is that he never returns.")

        first = judge(script, cache)
        judge(script, cache)

        assert first.verdict == "unknown"
        assert script.calls == 6

    @pytest.mark.parametrize("short", ["", "a", "twist"])
    def test_too_short_quote_is_retried_until_a_real_one_comes(
        self, cache: JudgeCache, short: str
    ) -> None:
        script = ScriptedJudge(short, "The film holds no twist,")

        assert judge(script, cache).verdict == "pass"
        assert script.calls == 2

    def test_quote_matches_across_reflowed_whitespace(self, cache: JudgeCache) -> None:
        script = ScriptedJudge("holds no twist, so the title promises none.", verdict="fail")

        assert judge(script, cache).verdict == "fail"
        assert script.calls == 1

    def test_unknown_needs_no_quote(self, cache: JudgeCache) -> None:
        assert judge(ScriptedJudge("", verdict="unknown"), cache).verdict == "unknown"


class TestCache:
    def test_same_judgement_is_answered_from_the_cache(self, cache: JudgeCache) -> None:
        script = ScriptedJudge("The film holds no twist,")

        first = judge(script, cache)
        second = judge(script, cache)

        assert second == first
        assert script.calls == 1

    def test_another_criterion_is_judged_afresh(self, cache: JudgeCache) -> None:
        script = ScriptedJudge("The film holds no twist,")

        judge(script, cache)
        judge(script, cache, criterion="PASS if the title names the harbour.")

        assert script.calls == 2
