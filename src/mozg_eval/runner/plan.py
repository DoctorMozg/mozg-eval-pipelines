"""Estimates what a suite run will cost from stored runs, without starting any agent."""

import logging
from collections.abc import Sequence
from statistics import fmean
from typing import Self

from mozg_eval.grading.support import DriverSupport
from mozg_eval.runner.suite import SuiteRunner
from mozg_eval.spec.models import Case, StrictModel
from mozg_eval.storage.base import ResultStore

log = logging.getLogger(__name__)


class CostHistory:
    """Past cost per trial (agent plus priced judge calls) of each case on one driver."""

    def __init__(self, per_trial: dict[str, list[float]]) -> None:
        self.per_trial = per_trial

    @classmethod
    def load(cls, results: ResultStore, ws: str, driver: str) -> Self:
        per_trial: dict[str, list[float]] = {}
        for run in results.list_runs(ws):
            spend = results.list_spend(ws, run.id)
            for trial in results.list_trials(ws, run.id):
                # A trial whose agent cost is unknown can't anchor an estimate.
                if trial.driver != driver or trial.cost_usd is None:
                    continue
                judged = sum(s.cost_usd for s in spend.get(trial.id, []) if s.cost_usd is not None)
                per_trial.setdefault(trial.case, []).append(trial.cost_usd + judged)
        log.debug("cost history for %d cases on %s", len(per_trial), driver)
        return cls(per_trial)

    def mean(self, case: str) -> float | None:
        costs = self.per_trial.get(case)
        return fmean(costs) if costs else None


class CaseEstimate(StrictModel):
    case: str
    trials: int
    per_trial: float | None = None
    skipped: str | None = None

    @property
    def cost(self) -> float | None:
        return None if self.per_trial is None else self.per_trial * self.trials


class Planner:
    def __init__(self, support: DriverSupport, history: CostHistory) -> None:
        self.support = support
        self.history = history

    def estimate(
        self, cases: Sequence[Case], runs: int | None, ablation: bool
    ) -> list[CaseEstimate]:
        estimates = []
        for case in cases:
            if reason := self.support.skip_reason(case):
                estimates.append(CaseEstimate(case=case.name, trials=0, skipped=reason))
                continue
            arms = SuiteRunner.arms(case, ablation)
            trials = (runs or case.execution.runs) * len(arms)
            mean = self.history.mean(case.name)
            estimates.append(CaseEstimate(case=case.name, trials=trials, per_trial=mean))
        return estimates

    @staticmethod
    def summary(estimates: Sequence[CaseEstimate]) -> list[str]:
        lines = []
        for e in estimates:
            if e.skipped:
                lines.append(f"{e.case}: skipped, {e.skipped}")
            elif e.cost is None:
                lines.append(f"{e.case}: {e.trials} trials, no stored cost to estimate from")
            else:
                lines.append(f"{e.case}: {e.trials} trials x ${e.per_trial:.4f} = ${e.cost:.4f}")
        planned = [e for e in estimates if not e.skipped]
        known = [e.cost for e in planned if e.cost is not None]
        total = f"plan: {len(planned)} cases, {sum(e.trials for e in planned)} trials, "
        if not known:
            total += "no stored cost to estimate from"
        else:
            total += f"estimated ${sum(known):.4f}"
            if unknown := len(planned) - len(known):
                total += f" plus {unknown} cases with no cost history"
        lines.append(total)
        return lines
