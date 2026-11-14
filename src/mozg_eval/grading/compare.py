"""Paired comparisons of two arms (with/without plugin, run A/B): the case is the unit."""

import math
import random
from collections.abc import Mapping, Sequence
from statistics import fmean
from typing import Self

from mozg_eval.grading.models import GradeResult
from mozg_eval.spec.models import StrictModel
from mozg_eval.storage.base import TrialRecord

BOOTSTRAP_RESAMPLES = 2000


def trial_score(trial: TrialRecord, grades: Sequence[GradeResult], ablation: bool) -> float | None:
    """Weighted share of applicable grades that passed; None when nothing could be graded."""
    if trial.status in ("error", "timeout"):
        return 0.0
    # In an ablation run "with" graders can only pass with the plugin, so they are indicators.
    counted = [g for g in grades if g.verdict != "n/a" and (not ablation or g.arm == "both")]
    total = sum(g.weight for g in counted)
    if not counted or total == 0:
        return None
    return sum(g.weight for g in counted if g.verdict == "pass") / total


def arm_scores(
    trials: Sequence[TrialRecord], grades: Mapping[str, Sequence[GradeResult]], ablation: bool
) -> dict[str, list[float]]:
    """Scores per case; trials with nothing gradable are left out."""
    scores: dict[str, list[float]] = {}
    for trial in trials:
        score = trial_score(trial, grades.get(trial.id, []), ablation)
        if score is not None:
            scores.setdefault(trial.case, []).append(score)
    return scores


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pair counts."""
    if b < 0 or c < 0:
        raise ValueError(f"discordant counts must be non-negative, got {b} and {c}")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / (1 << n)
    return min(1.0, 2 * tail)


class CaseDelta(StrictModel):
    case: str
    treatment: list[float]
    control: list[float]

    @property
    def delta(self) -> float:
        return fmean(self.treatment) - fmean(self.control)


class Comparison(StrictModel):
    cases: list[CaseDelta]
    unpaired: list[str]

    @classmethod
    def of(cls, treatment: Mapping[str, list[float]], control: Mapping[str, list[float]]) -> Self:
        paired = sorted(treatment.keys() & control.keys())
        return cls(
            cases=[CaseDelta(case=c, treatment=treatment[c], control=control[c]) for c in paired],
            unpaired=sorted(treatment.keys() ^ control.keys()),
        )

    def mean_delta(self) -> float:
        if not self.cases:
            raise ValueError("no case was graded in both arms")
        return fmean(c.delta for c in self.cases)

    def bootstrap_ci(
        self, resamples: int = BOOTSTRAP_RESAMPLES, seed: int = 0
    ) -> tuple[float, float]:
        """95% percentile interval: resample cases, then each case's runs in both arms."""
        if not self.cases:
            raise ValueError("no case was graded in both arms")
        rng = random.Random(seed)
        means = []
        for _ in range(resamples):
            deltas = []
            for case in rng.choices(self.cases, k=len(self.cases)):
                treated = rng.choices(case.treatment, k=len(case.treatment))
                untreated = rng.choices(case.control, k=len(case.control))
                deltas.append(fmean(treated) - fmean(untreated))
            means.append(fmean(deltas))
        means.sort()
        return means[int(0.025 * resamples)], means[math.ceil(0.975 * resamples) - 1]

    def mcnemar(self) -> float | None:
        """Exact McNemar on full passes, only when every case ran once in each arm."""
        if not self.cases or any(len(c.treatment) != 1 or len(c.control) != 1 for c in self.cases):
            return None
        wins = sum(c.treatment[0] == 1.0 and c.control[0] < 1.0 for c in self.cases)
        losses = sum(c.treatment[0] < 1.0 and c.control[0] == 1.0 for c in self.cases)
        return mcnemar_exact(wins, losses)

    def summary(self, treatment: str, control: str) -> list[str]:
        lines = [
            f"{c.case}: {treatment} {fmean(c.treatment):.2f}, {control} {fmean(c.control):.2f}, "
            f"delta {c.delta:+.2f}"
            for c in self.cases
        ]
        if self.unpaired:
            lines.append(f"not graded in both arms: {', '.join(self.unpaired)}")
        if not self.cases:
            lines.append("no case was graded in both arms")
            return lines
        low, high = self.bootstrap_ci()
        line = (
            f"delta score {self.mean_delta():+.2f} (95% CI {low:+.2f} to {high:+.2f}) "
            f"over {len(self.cases)} cases, {treatment} vs {control}"
        )
        if (p := self.mcnemar()) is not None:
            line += f"; exact McNemar p={p:.3g}"
        lines.append(line)
        return lines
