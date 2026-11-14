"""Runs a suite's cases on one driver: a fresh workspace and HOME per trial, results stored."""

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from mozg_eval.drivers.protocol import Driver, Limits, TrialRequest
from mozg_eval.events.models import End, EndStatus, Event, Payload, SkillActivated, Usage
from mozg_eval.grading.graders import TrialGrader
from mozg_eval.grading.models import GradeResult
from mozg_eval.grading.support import DriverSupport
from mozg_eval.judge.grading import TrialJudge, merged
from mozg_eval.runner.checks import WorkspaceChecks, needs_workspace, snapshot
from mozg_eval.runner.settings import resolve_settings
from mozg_eval.runner.trial import TrialRunner, TurnGuard
from mozg_eval.spec.loader import CaseLoader
from mozg_eval.spec.models import Case
from mozg_eval.storage.base import ArtifactRef, RunRecord, TrialRecord, new_id
from mozg_eval.storage.config import Stores

log = logging.getLogger(__name__)

# (home, raw_dir) -> a driver for one trial; drivers keep per-trial process state.
type DriverFactory = Callable[[Path, Path], Driver]
# Builds the suite's judges once its cases are loaded, before any trial starts.
type JudgeFactory = Callable[[Sequence[Case]], TrialJudge]


class FixtureError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrialSummary:
    case: str
    record: TrialRecord
    grades: list[GradeResult]
    skills: list[str] = field(default_factory=list)


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    yield path.read_bytes()


async def _exec(*argv: str, cwd: Path, env: Mapping[str, str] | None = None) -> None:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await process.communicate()
    if process.returncode != 0:
        raise FixtureError(f"{' '.join(argv)} exited with {process.returncode}: {out.decode()}")


def _outcome(events: list[Event]) -> tuple[EndStatus, float | None]:
    end = next(e.payload for e in reversed(events) if isinstance(e.payload, End))
    costs = [
        e.payload.cost_usd
        for e in events
        if isinstance(e.payload, Usage) and e.payload.cost_usd is not None
    ]
    return end.status, sum(costs) if costs else None


class SuiteRunner:
    def __init__(
        self,
        stores: Stores,
        support: DriverSupport,
        make_driver: DriverFactory,
        ws: str,
        overrides: Mapping[str, JsonValue],
        make_judge: JudgeFactory,
    ) -> None:
        self.stores = stores
        self.support = support
        self.driver_name = support.driver
        self.make_driver = make_driver
        self.ws = ws
        self.overrides = overrides
        self.make_judge = make_judge

    def runnable(self, case: Case) -> str | None:
        """Return why the case can't run on this driver yet, or None."""
        drivers = case.drivers
        if (drivers.only is not None and self.driver_name not in drivers.only) or (
            self.driver_name in drivers.exclude
        ):
            return f"case excludes driver {self.driver_name}"
        if case.history_file or case.add_dirs:
            return "history_file and add_dirs are not supported yet"
        if not self.support.applicable(case):
            # Every grade would be n/a, so the trial could only pass vacuously.
            return f"nothing to grade on {self.driver_name}: {'; '.join(self.support.gaps(case))}"
        return None

    async def run(self, case_files: Sequence[Path]) -> tuple[RunRecord, list[TrialSummary]]:
        loader = CaseLoader()
        cases = [(path.parent, loader.load(path)) for path in case_files]
        judge = self.make_judge([case for _, case in cases])
        run = RunRecord(id=new_id(), started_at=datetime.now(UTC), trigger="local")
        self.stores.results.add_run(self.ws, run)
        log.info("run %s: %d cases on %s", run.id, len(cases), self.driver_name)
        summaries = []
        for case_dir, case in cases:
            if reason := self.runnable(case):
                log.warning("skipping %s: %s", case.name, reason)
                continue
            execution = case.execution.model_copy(update=self.overrides)
            for repeat in range(1, execution.runs + 1):
                summaries.append(await self._trial(run, judge, case_dir, case, repeat))
        return run, summaries

    async def _trial(
        self, run: RunRecord, judge: TrialJudge, case_dir: Path, case: Case, repeat: int
    ) -> TrialSummary:
        execution = case.execution.model_copy(update=self.overrides)
        trial_id = new_id()
        # The temp dir holds the workspace and the HOME with linked credentials; both go with it.
        with tempfile.TemporaryDirectory(prefix="mzeval-") as tmp:
            workspace, home, raw_dir = Path(tmp, "ws"), Path(tmp, "home"), Path(tmp, "raw")
            workspace.mkdir()
            home.mkdir()
            await _exec("git", "init", "-q", ".", cwd=workspace)
            check_env = os.environ | {"HOME": str(home)}
            if case.fixture:
                await _exec("bash", str(case_dir / case.fixture), cwd=workspace, env=check_env)
            before = snapshot(workspace)
            request = TrialRequest(
                trial=trial_id,
                prompt=execution.prompt,
                workspace=str(workspace),
                env=execution.env,
                plugins=[str((case_dir / plugin).resolve()) for plugin in case.bundle],
                tools=execution.tools,
                model=execution.model,
                append_system_prompt=execution.append_system_prompt,
                limits=Limits(max_turns=execution.max_turns, timeout_s=execution.timeout_s),
            )
            runner = TrialRunner(
                self.ws,
                run.id,
                self.make_driver(home, raw_dir),
                self.stores.events,
                [TurnGuard(execution.max_turns)],
            )
            events = await runner.run(request, resolve_settings(case.execution, self.overrides))
            checks = WorkspaceChecks(workspace, before, check_env)
            post_run: list[Payload] = [*checks.changes()]
            post_run += [await checks.check(g) for g in case.graders if needs_workspace(g)]
            for payload in post_run:
                await runner.append(trial_id, payload)
            judged = await judge.grade(case, [e.payload for e in events], workspace)
            for name in ("raw.jsonl", "stderr.log"):
                if (raw_dir / name).exists():
                    ref = ArtifactRef(run=run.id, trial=trial_id, name=name)
                    await self.stores.artifacts.put(self.ws, ref, _file_chunks(raw_dir / name))
            raw_log = raw_dir / "raw.jsonl"
            raw = raw_log.read_text(errors="replace") if raw_log.exists() else None
        grades = merged(TrialGrader(case, self.support, raw).grade(events), judged)
        status, cost = _outcome(events)
        record = TrialRecord(
            id=trial_id,
            run=run.id,
            case=case.name,
            arm="with",
            repeat=repeat,
            driver=self.driver_name,
            status=status,
            cost_usd=cost,
            passed=TrialGrader.passed(events, grades),
        )
        self.stores.results.add_trial(self.ws, record)
        self.stores.results.add_grades(self.ws, trial_id, grades)
        skills = [e.payload.skill for e in events if isinstance(e.payload, SkillActivated)]
        return TrialSummary(case=case.name, record=record, grades=grades, skills=skills)
