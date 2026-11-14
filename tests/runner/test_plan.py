from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mozg_eval.drivers.claude import ClaudeDriver
from mozg_eval.grading.models import JudgeSpend
from mozg_eval.grading.support import DriverSupport
from mozg_eval.runner.plan import CaseEstimate, CostHistory, Planner
from mozg_eval.spec.models import Case
from mozg_eval.storage.base import RunRecord, TrialRecord
from mozg_eval.storage.sql import SqlResultStore


def case(name: str, **extra: object) -> Case:
    return Case.model_validate(
        {
            "schema": "mzeval/2",
            "name": name,
            "kind": "behaviour",
            "bundle": ["../plugin"],
            "execution": {"prompt": "p", "runs": 3},
            "graders": [{"name": "g", "type": "regex", "pattern": "x"}],
        }
        | extra
    )


@pytest.fixture
def results(tmp_path: Path) -> Iterator[SqlResultStore]:
    store = SqlResultStore(f"sqlite:///{tmp_path / 'mzeval.db'}")
    yield store
    store.close()


def add_trial(results: SqlResultStore, tid: str, run: str, cost: float | None, driver: str) -> None:
    trial = TrialRecord(
        id=tid, run=run, case="a", arm="with", repeat=1, driver=driver, status="ok", cost_usd=cost
    )
    results.add_trial("local", trial)


class TestCostHistory:
    def test_mean_trial_cost_adds_priced_judge_calls_on_the_same_driver(
        self, results: SqlResultStore
    ) -> None:
        for run in ("r1", "r2"):
            results.add_run(
                "local", RunRecord(id=run, started_at=datetime.now(UTC), trigger="local")
            )
        add_trial(results, "t1", "r1", 0.01, "claude")
        add_trial(results, "t2", "r2", 0.03, "claude")
        add_trial(results, "t3", "r2", None, "claude")
        add_trial(results, "t4", "r2", 9.0, "codex")
        judged = JudgeSpend(model="m", input_tokens=1, output_tokens=1, cost_usd=0.002)
        unpriced = JudgeSpend(model="m", input_tokens=1, output_tokens=1, cost_usd=None)
        results.add_spend("local", "t1", [judged, unpriced])

        history = CostHistory.load(results, "local", "claude")

        assert history.mean("a") == pytest.approx(0.021)
        assert history.mean("b") is None


class TestPlanner:
    @pytest.fixture
    def planner(self) -> Planner:
        support = DriverSupport("claude", ClaudeDriver.capabilities)
        return Planner(support, CostHistory({"a": [0.01], "b": [0.02]}))

    def test_trials_follow_runs_and_ablation_arms(self, planner: Planner) -> None:
        cases = [case("a"), case("b", drivers={"exclude": ["claude"]}), case("c")]

        estimates = planner.estimate(cases, runs=None, ablation=True)

        assert estimates == [
            CaseEstimate(case="a", trials=6, per_trial=0.01),
            CaseEstimate(case="b", trials=0, skipped="case excludes driver claude"),
            CaseEstimate(case="c", trials=6),
        ]
        assert planner.estimate([case("a")], runs=1, ablation=False)[0].trials == 1

    def test_summary_totals_only_what_history_can_price(self, planner: Planner) -> None:
        lines = Planner.summary(planner.estimate([case("a"), case("c")], None, False))

        assert lines == [
            "a: 3 trials x $0.0100 = $0.0300",
            "c: 3 trials, no stored cost to estimate from",
            "plan: 2 cases, 6 trials, estimated $0.0300 plus 1 cases with no cost history",
        ]
