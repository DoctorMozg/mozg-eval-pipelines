"""Small-n statistics: the case is the unit, repeats of one case are not independent samples."""

import math
from collections.abc import Sequence
from itertools import groupby
from typing import Self

from mozg_eval.spec.models import StrictModel
from mozg_eval.storage.base import TrialRecord

Z_95 = 1.959964


def wilson(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval; unlike the normal approximation it stays sane at n = 3."""
    if not 0 <= successes <= n:
        raise ValueError(f"successes {successes} must be within 0..{n}")
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - margin), min(1.0, centre + margin)


class CaseStats(StrictModel):
    case: str
    runs: int
    passes: int

    @property
    def pass_at_1(self) -> float:
        return self.passes / self.runs

    @property
    def pass_all(self) -> bool:
        """pass^k: every repeat passed, the reliability metric."""
        return self.passes == self.runs

    @property
    def flaky(self) -> bool:
        return 0 < self.passes < self.runs


class SuiteStats(StrictModel):
    cases: list[CaseStats]
    threshold: float

    @classmethod
    def of(cls, trials: Sequence[TrialRecord], threshold: float) -> Self:
        """Trials without a verdict count as failures, so an ungraded run can't look green."""
        if not 0 < threshold <= 1:
            raise ValueError(f"threshold {threshold} must be within (0, 1]")
        by_case = groupby(sorted(trials, key=lambda t: t.case), key=lambda t: t.case)
        cases = []
        for case, group in by_case:
            runs = list(group)
            cases.append(
                CaseStats(case=case, runs=len(runs), passes=sum(bool(t.passed) for t in runs))
            )
        return cls(cases=cases, threshold=threshold)

    def failing(self) -> list[CaseStats]:
        return [c for c in self.cases if c.pass_at_1 < self.threshold]

    def pass_rate(self) -> tuple[float, float, float]:
        """Share of cases at or above the threshold, with its Wilson 95% interval."""
        passing = len(self.cases) - len(self.failing())
        low, high = wilson(passing, len(self.cases))
        return (passing / len(self.cases) if self.cases else 0.0), low, high
