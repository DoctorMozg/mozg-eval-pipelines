import pytest

from mozg_eval.grading.stats import SuiteStats, wilson
from mozg_eval.storage.base import TrialRecord


def trial(case: str, repeat: int, passed: bool | None) -> TrialRecord:
    return TrialRecord(
        id=f"{case}-{repeat}",
        run="r",
        case=case,
        arm="with",
        repeat=repeat,
        driver="claude",
        status="ok",
        passed=passed,
    )


class TestWilson:
    @pytest.mark.parametrize(
        ("successes", "n", "low", "high"),
        [(3, 3, 0.4385, 1.0), (0, 3, 0.0, 0.5615), (5, 10, 0.2366, 0.7634)],
    )
    def test_matches_published_values(
        self, successes: int, n: int, low: float, high: float
    ) -> None:
        assert wilson(successes, n) == pytest.approx((low, high), abs=1e-4)

    def test_no_data_is_the_whole_interval(self) -> None:
        assert wilson(0, 0) == (0.0, 1.0)

    def test_rejects_more_successes_than_trials(self) -> None:
        with pytest.raises(ValueError, match=r"within 0\.\.3"):
            wilson(4, 3)


class TestSuiteStats:
    @pytest.fixture
    def stats(self) -> SuiteStats:
        trials = [
            trial("steady", 1, True),
            trial("steady", 2, True),
            trial("flaky", 1, True),
            trial("flaky", 2, False),
            trial("ungraded", 1, None),
        ]
        return SuiteStats.of(trials, threshold=1.0)

    def test_groups_repeats_by_case(self, stats: SuiteStats) -> None:
        by_case = {c.case: (c.passes, c.runs, c.pass_all, c.flaky) for c in stats.cases}

        assert by_case == {
            "flaky": (1, 2, False, True),
            "steady": (2, 2, True, False),
            "ungraded": (0, 1, False, False),
        }

    def test_threshold_decides_which_cases_fail(self, stats: SuiteStats) -> None:
        assert [c.case for c in stats.failing()] == ["flaky", "ungraded"]
        lenient = SuiteStats(cases=stats.cases, threshold=0.5)
        assert [c.case for c in lenient.failing()] == ["ungraded"]

    def test_suite_rate_counts_cases_not_runs(self, stats: SuiteStats) -> None:
        rate, low, high = stats.pass_rate()

        assert rate == pytest.approx(1 / 3)
        assert (low, high) == pytest.approx(wilson(1, 3))
