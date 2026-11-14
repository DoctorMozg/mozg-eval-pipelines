import pytest

from mozg_eval.events.models import EndStatus
from mozg_eval.grading.compare import Comparison, arm_scores, mcnemar_exact, trial_score
from mozg_eval.grading.models import GradeResult, Verdict
from mozg_eval.spec.models import Arm
from mozg_eval.storage.base import TrialRecord


def trial(case: str = "c", status: EndStatus = "ok", tid: str = "t") -> TrialRecord:
    return TrialRecord(
        id=tid, run="r", case=case, arm="with", repeat=1, driver="claude", status=status
    )


def grade(verdict: Verdict, weight: float = 1.0, arm: Arm = "both") -> GradeResult:
    return GradeResult(name=f"{verdict}-{weight}-{arm}", verdict=verdict, weight=weight, arm=arm)


class TestTrialScore:
    def test_weighted_share_of_applicable_grades(self) -> None:
        grades = [grade("pass", 3), grade("fail", 1), grade("n/a", 5)]

        assert trial_score(trial(), grades, ablation=False) == 0.75

    def test_ablation_leaves_out_with_only_graders(self) -> None:
        grades = [grade("pass"), grade("fail", arm="with")]

        assert trial_score(trial(), grades, ablation=False) == 0.5
        assert trial_score(trial(), grades, ablation=True) == 1.0

    def test_errored_trial_scores_zero_and_ungradable_scores_none(self) -> None:
        assert trial_score(trial(status="timeout"), [grade("pass")], ablation=False) == 0.0
        assert trial_score(trial(), [grade("n/a")], ablation=False) is None

    def test_arm_scores_groups_by_case_and_drops_ungradable(self) -> None:
        trials = [trial("a", tid="1"), trial("a", tid="2"), trial("b", tid="3")]
        grades = {"1": [grade("pass")], "2": [grade("fail")], "3": [grade("n/a")]}

        assert arm_scores(trials, grades, ablation=False) == {"a": [1.0, 0.0]}


class TestMcNemar:
    @pytest.mark.parametrize(
        ("b", "c", "p"), [(0, 5, 0.0625), (1, 9, 22 / 1024), (3, 3, 1.0), (0, 0, 1.0)]
    )
    def test_matches_binomial_tail(self, b: int, c: int, p: float) -> None:
        assert mcnemar_exact(b, c) == pytest.approx(p)

    def test_reported_only_with_one_run_per_arm(self) -> None:
        single = Comparison.of({"a": [1.0], "b": [1.0]}, {"a": [0.0], "b": [1.0]})
        repeated = Comparison.of({"a": [1.0, 1.0]}, {"a": [0.0, 0.0]})

        assert single.mcnemar() == 1.0
        assert repeated.mcnemar() is None


class TestComparison:
    def test_pairs_cases_and_lists_the_rest(self) -> None:
        comparison = Comparison.of({"a": [1.0], "b": [0.5]}, {"a": [0.25], "c": [1.0]})

        assert [c.case for c in comparison.cases] == ["a"]
        assert comparison.unpaired == ["b", "c"]
        assert comparison.mean_delta() == 0.75

    def test_deterministic_runs_give_a_point_interval(self) -> None:
        comparison = Comparison.of({"hook": [1.0] * 3}, {"hook": [1 / 3] * 3})

        assert comparison.bootstrap_ci() == pytest.approx((2 / 3, 2 / 3))

    def test_interval_covers_the_mean_and_is_reproducible(self) -> None:
        treatment = {f"c{i}": [1.0, 0.0, 1.0] if i % 2 else [1.0] * 3 for i in range(8)}
        control = {f"c{i}": [0.0, 0.0, 1.0] for i in range(8)}
        comparison = Comparison.of(treatment, control)

        low, high = comparison.bootstrap_ci()

        assert low < comparison.mean_delta() < high
        assert (low, high) == comparison.bootstrap_ci()

    def test_summary_without_pairs_says_so(self) -> None:
        lines = Comparison.of({"a": [1.0]}, {}).summary("with", "without")

        assert lines == ["not graded in both arms: a", "no case was graded in both arms"]
