"""Regrades a stored run's judge graders from its stored events; no agent runs again."""

import logging
from collections.abc import Mapping

from mozg_eval.grading.graders import TrialGrader
from mozg_eval.judge.grading import TrialJudge, merged
from mozg_eval.runner.suite import TrialSummary
from mozg_eval.spec.models import Case
from mozg_eval.storage.config import Stores

log = logging.getLogger(__name__)


class Rejudger:
    def __init__(self, stores: Stores, ws: str, judge: TrialJudge) -> None:
        self.stores = stores
        self.ws = ws
        self.judge = judge

    async def rejudge(self, run_id: str, cases: Mapping[str, Case]) -> list[TrialSummary]:
        """Trials of cases without judge graders are left as they are and not returned."""
        results = self.stores.results
        if results.get_run(self.ws, run_id) is None:
            raise LookupError(f"no stored run {run_id!r}")
        trials = results.list_trials(self.ws, run_id)
        # Check every case first, so a missing one doesn't leave the run half rejudged.
        if missing := sorted({t.case for t in trials} - cases.keys()):
            raise LookupError(f"run {run_id} has trials of cases not given: {', '.join(missing)}")
        stored = results.list_grades(self.ws, run_id)
        summaries = []
        for trial in trials:
            events = [e async for e in self.stores.events.read(self.ws, run_id, trial.id)]
            judged = await self.judge.grade(cases[trial.case], [e.payload for e in events], None)
            if not judged.grades:
                continue
            grades = merged(stored.get(trial.id, []), judged.grades)
            passed = TrialGrader.passed(events, grades)
            results.regrade(self.ws, trial.id, judged.grades, passed)
            results.add_spend(self.ws, trial.id, judged.spend)
            record = trial.model_copy(update={"passed": passed})
            summaries.append(TrialSummary(trial.case, record, grades, judge_spend=judged.spend))
        log.info("rejudged %d of %d trials in run %s", len(summaries), len(trials), run_id)
        return summaries
