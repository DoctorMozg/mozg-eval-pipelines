import asyncio
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import JsonValue

from mozg_eval import __version__
from mozg_eval.drivers.claude import ClaudeDriver
from mozg_eval.drivers.codex import CodexDriver
from mozg_eval.drivers.opencode import OpenCodeDriver
from mozg_eval.drivers.pi import PiDriver
from mozg_eval.drivers.protocol import AgentError
from mozg_eval.grading.compare import Comparison, arm_scores
from mozg_eval.grading.graders import EXPECT
from mozg_eval.grading.models import GradeResult
from mozg_eval.grading.routing import RoutedTrial, Routing
from mozg_eval.grading.stats import SuiteStats
from mozg_eval.grading.support import DriverSupport
from mozg_eval.importers.claude_harness import ClaudeHarnessImporter, ClaudePart
from mozg_eval.importers.claude_plugin_eval import (
    ClaudePluginEvalImporter,
    render_report,
)
from mozg_eval.judge.cache import JudgeCache
from mozg_eval.judge.grading import JudgeSetupError, TrialJudge
from mozg_eval.push import SERVER_ENV, TOKEN_ENV, PushError, PushTarget, RunPusher
from mozg_eval.report.html import ReportBuilder
from mozg_eval.report.junit import JUnitReport
from mozg_eval.runner.plan import CostHistory, Planner
from mozg_eval.runner.rejudge import Rejudger
from mozg_eval.runner.suite import (
    CaseFilter,
    FixtureError,
    NoCaseSelected,
    RunOptions,
    SuiteRunner,
    TrialSummary,
)
from mozg_eval.spec.harness import (
    HARNESS_FILENAME,
    Harness,
    HarnessLoader,
    HarnessLoadError,
    HarnessManifest,
)
from mozg_eval.spec.loader import CASE_FILENAME, CaseLoader, CaseLoadError
from mozg_eval.spec.models import Case
from mozg_eval.storage.base import DEFAULT_WORKSPACE, TrialRecord
from mozg_eval.storage.config import StorageConfig, open_stores

app = typer.Typer(
    name="mzeval",
    help="Run evals for coding-agent plugins and skills on Claude Code, Codex, Pi and OpenCode.",
    no_args_is_help=True,
)


class DriverName(StrEnum):
    claude = "claude"
    codex = "codex"
    opencode = "opencode"
    pi = "pi"


DRIVERS: dict[DriverName, type[ClaudeDriver | CodexDriver | OpenCodeDriver | PiDriver]] = {
    DriverName.claude: ClaudeDriver,
    DriverName.codex: CodexDriver,
    DriverName.opencode: OpenCodeDriver,
    DriverName.pi: PiDriver,
}

import_app = typer.Typer(help="Convert cases from other eval tools.", no_args_is_help=True)
app.add_typer(import_app, name="import")
harness_app = typer.Typer(
    help="Work with harnesses, the setup an agent runs with.", no_args_is_help=True
)
app.add_typer(harness_app, name="harness")
harness_import_app = typer.Typer(
    help="Make a harness from an agent's own config.", no_args_is_help=True
)
harness_app.add_typer(harness_import_app, name="import")

# `--harness none` stands for the agent's own setup, so it can be the baseline of an A/B run.
NO_HARNESS = "none"
HarnessOption = Annotated[
    list[str] | None,
    typer.Option(
        "--harness",
        help="A harness directory, or none for the agent's own setup; repeatable. Every case "
        "runs once per harness, and the first is the baseline the others are compared against.",
    ),
]

JudgeOption = Annotated[
    str | None,
    typer.Option(
        "--judge",
        help="Model for judge graders that name none, e.g. anthropic:claude-sonnet-5-5; "
        "its API key comes from the environment.",
    ),
]


def _judge_cache() -> JudgeCache:
    return JudgeCache(Path.cwd() / ".mzeval" / "judge-cache")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"mzeval {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    """Run evals for coding-agent plugins and skills."""


@app.command()
def lint(
    paths: Annotated[
        list[Path],
        typer.Argument(
            exists=True,
            help=f"Case files, or folders to search for {CASE_FILENAME} and {HARNESS_FILENAME}.",
        ),
    ],
) -> None:
    """Validate case files and harnesses; exit 1 if any is invalid."""
    cases = CaseLoader()
    harnesses = HarnessLoader()
    case_files = cases.discover([p for p in paths if p.name != HARNESS_FILENAME])
    harness_dirs = harnesses.discover(paths)
    if not case_files and not harness_dirs:
        found = f"no {CASE_FILENAME} or {HARNESS_FILENAME} found under"
        typer.echo(f"{found}: {', '.join(map(str, paths))}", err=True)
        raise typer.Exit(2)

    invalid = 0
    for path in case_files:
        try:
            cases.load(path)
        except CaseLoadError as exc:
            invalid += 1
            for issue in exc.issues:
                typer.echo(str(issue), err=True)
    bad_harnesses = 0
    for root in harness_dirs:
        try:
            harnesses.load(root)
        except HarnessLoadError as exc:
            bad_harnesses += 1
            for issue in exc.issues:
                typer.echo(str(issue), err=True)
    if case_files:
        typer.echo(f"{len(case_files) - invalid} of {len(case_files)} case files valid")
    if harness_dirs:
        typer.echo(f"{len(harness_dirs) - bad_harnesses} of {len(harness_dirs)} harnesses valid")
    if invalid or bad_harnesses:
        raise typer.Exit(1)


@app.command()
def run(
    paths: Annotated[
        list[Path],
        typer.Argument(
            exists=True, help=f"Case files, or directories to search for {CASE_FILENAME}."
        ),
    ],
    runs: Annotated[int | None, typer.Option(min=1, help="Repeats per case.")] = None,
    model: Annotated[str | None, typer.Option(help="Model for every case.")] = None,
    threshold: Annotated[
        float, typer.Option(min=0.01, max=1.0, help="Pass rate each case needs; 1.0 is every run.")
    ] = 1.0,
    driver: Annotated[DriverName, typer.Option(help="The agent to run.")] = DriverName.claude,
    executable: Annotated[
        str | None, typer.Option(help="The agent's executable; defaults to the driver name.")
    ] = None,
    judge: JudgeOption = None,
    ablation: Annotated[
        bool,
        typer.Option(help="Also run each case without its bundle and compare the two arms."),
    ] = False,
    junit: Annotated[
        Path | None, typer.Option(help="Write a JUnit XML report here, one testcase per case.")
    ] = None,
    case: Annotated[
        list[str] | None,
        typer.Option("--case", help="Run only cases whose name matches this glob; repeatable."),
    ] = None,
    tag: Annotated[
        list[str] | None, typer.Option("--tag", help="Run only cases with this tag; repeatable.")
    ] = None,
    jobs: Annotated[
        int, typer.Option(min=1, max=16, help="Trials to run side by side; they share one login.")
    ] = 1,
    max_cost_usd: Annotated[
        float | None,
        typer.Option(
            min=0.0, help="Agent and judge spend after which no trial starts; exit 2 if reached."
        ),
    ] = None,
    push: Annotated[
        bool,
        typer.Option(
            help=f"Upload the run to ${SERVER_ENV} with ${TOKEN_ENV}; a failure is reported."
        ),
    ] = False,
    harness: HarnessOption = None,
) -> None:
    """Run and grade cases on one agent.

    Exit 0 when every case meets the threshold, 1 when one doesn't, 2 when a trial errored
    or the spend cap stopped the run, 130 when interrupted.
    With --ablation only the with-bundle arm counts toward the threshold, and with several
    --harness the baseline doesn't count. A failed --push never changes the exit code;
    `mzeval push RUN` retries it.
    """
    target = _push_target() if push else None
    setups = _load_setups(harness)
    files = CaseLoader.discover(paths)
    if not files:
        typer.echo(f"no {CASE_FILENAME} found under: {', '.join(map(str, paths))}", err=True)
        raise typer.Exit(2)
    wanted = executable or driver.value
    # Absolute, because each trial starts the agent from its own temp workspace.
    found = shutil.which(wanted)
    if found is None:
        typer.echo(f"agent not found: {wanted}; install it or pass --executable", err=True)
        raise typer.Exit(2)
    command = str(Path(found).absolute())
    overrides: dict[str, JsonValue] = {
        key: value for key, value in {"runs": runs, "model": model}.items() if value is not None
    }
    stores = open_stores(StorageConfig.load(Path.cwd()))
    suite = SuiteRunner(
        stores,
        DriverSupport(driver.value, DRIVERS[driver].capabilities),
        lambda home, raw_dir: DRIVERS[driver](home, raw_dir, executable=command),
        DEFAULT_WORKSPACE,
        overrides,
        lambda cases: TrialJudge.for_cases(cases, judge, _judge_cache()),
    )
    try:
        select = CaseFilter(tuple(case or ()), tuple(tag or ()))
        options = RunOptions(ablation, select, jobs, max_cost_usd, setups)
        result = asyncio.run(suite.run(files, options))
    except JudgeSetupError as exc:
        typer.echo(f"judge: {exc}", err=True)
        raise typer.Exit(2) from exc
    except CaseLoadError as exc:
        for issue in exc.issues:
            typer.echo(str(issue), err=True)
        raise typer.Exit(2) from exc
    except FixtureError as exc:
        typer.echo(f"fixture failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    except AgentError as exc:
        typer.echo(f"agent check failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    except NoCaseSelected as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    except KeyboardInterrupt as exc:
        # Click would exit 1, which CI reads as a regression; 130 is the shell's SIGINT code.
        typer.echo("interrupted; trials that finished are stored", err=True)
        raise typer.Exit(130) from exc
    finally:
        stores.results.close()
    record, trials = result.run, result.trials
    names = [_setup_name(setup) for setup in setups] or [NO_HARNESS]
    several = len(names) > 1
    for name, reason in sorted(result.skipped.items()):
        typer.echo(f"SKIP {name}: {reason}")
    for trial in trials:
        cost = "n/a" if trial.record.cost_usd is None else f"${trial.record.cost_usd:.4f}"
        skills = ", ".join(trial.skills) or "none"
        verdict = "PASS" if trial.record.passed else "FAIL"
        tags = [_harness_of(trial.record)] if several else []
        tags += [trial.record.arm] if ablation else []
        label = f" [{', '.join(tags)}]" if tags else ""
        typer.echo(
            f"{verdict} {trial.case} #{trial.record.repeat}{label}: {trial.record.status}, "
            f"skills: {skills}, {cost}"
        )
        for grade in trial.grades:
            if grade.verdict == "fail":
                typer.echo(f"    {grade.name}: {grade.detail}")
    grades = grades_of(trials)
    if ablation:
        for name in names:
            mine = [t.record for t in trials if _harness_of(t.record) == name]
            with_arm, without_arm = (
                arm_scores([r for r in mine if r.arm == arm], grades, True)
                for arm in ("with", "without")
            )
            if several:
                typer.echo(f"harness {name}:")
            for line in Comparison.of(with_arm, without_arm).summary("with", "without"):
                typer.echo(line)
    with_bundle = [t for t in trials if t.record.arm == "with"]
    if several:
        _echo_harness_comparison([t.record for t in with_bundle], grades, names)
    # The baseline harness is a reference point, like the without arm, so it gates nothing.
    gated_trials = [t for t in with_bundle if not several or _harness_of(t.record) != names[0]]
    _echo_routing(files, gated_trials)
    _echo_spend(trials, agents=True)
    gated = [t.record for t in gated_trials]
    if junit is not None:
        report = JUnitReport(f"mzeval.{driver.value}", threshold)
        junit.parent.mkdir(parents=True, exist_ok=True)
        junit.write_text(report.build(gated, grades_of(trials), result.skipped), encoding="utf-8")
    stats = SuiteStats.of(gated, threshold)
    rate, low, high = stats.pass_rate()
    typer.echo(
        f"run {record.id}: {len(trials)} trials, {len(stats.cases)} cases, "
        f"{rate:.0%} at or above {threshold:.0%} (95% CI {low:.0%}-{high:.0%})"
    )
    if target is not None and not _push_run(target, record.id):
        typer.echo(f"retry with: mzeval push {record.id}", err=True)
    if result.capped:
        typer.echo(
            f"spend cap ${max_cost_usd} reached: {result.not_started} trials not started", err=True
        )
        raise typer.Exit(2)
    if any(trial.record.status in ("error", "timeout") for trial in trials):
        raise typer.Exit(2)
    if stats.failing():
        typer.echo("below threshold: " + ", ".join(c.case for c in stats.failing()))
        raise typer.Exit(1)


def _push_target() -> PushTarget:
    try:
        return PushTarget.from_env(os.environ)
    except PushError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


def _push_run(target: PushTarget, run_id: str) -> bool:
    """Push a stored run; a failure is reported and returned, so callers pick the exit code."""
    stores = open_stores(StorageConfig.load(Path.cwd()))

    async def push() -> int:
        async with target.client() as client:
            return await RunPusher(stores, DEFAULT_WORKSPACE, client).push(run_id)

    try:
        count = asyncio.run(push())
    except (PushError, LookupError) as exc:
        typer.echo(f"push failed: {exc}", err=True)
        return False
    finally:
        stores.results.close()
    typer.echo(f"pushed run {run_id}: {count} trials to {target.server}")
    return True


def _setup_name(setup: Harness | None) -> str:
    return NO_HARNESS if setup is None else setup.name


def _harness_of(trial: TrialRecord) -> str:
    return trial.harness or NO_HARNESS


def _load_setups(values: list[str] | None) -> tuple[Harness | None, ...]:
    """The --harness values loaded in order; a broken harness or a repeated name exits 2."""
    loader = HarnessLoader()
    setups: list[Harness | None] = []
    for value in values or ():
        if value == NO_HARNESS:
            setups.append(None)
            continue
        try:
            setups.append(loader.load(Path(value)))
        except HarnessLoadError as exc:
            for issue in exc.issues:
                typer.echo(str(issue), err=True)
            raise typer.Exit(2) from exc
    names = [_setup_name(setup) for setup in setups]
    if repeated := sorted({name for name in names if names.count(name) > 1}):
        typer.echo(f"--harness names a harness twice: {', '.join(repeated)}", err=True)
        raise typer.Exit(2)
    return tuple(setups)


def _echo_harness_comparison(
    trials: list[TrialRecord], grades: Mapping[str, Sequence[GradeResult]], names: list[str]
) -> None:
    """Each harness against the first, case by case, on the with-bundle arm."""
    scores = {
        name: arm_scores([t for t in trials if _harness_of(t) == name], grades, False)
        for name in names
    }
    for name in names[1:]:
        for line in Comparison.of(scores[name], scores[names[0]]).summary(name, names[0]):
            typer.echo(line)


def grades_of(trials: list[TrialSummary]) -> dict[str, list[GradeResult]]:
    return {t.record.id: t.grades for t in trials}


def _total(costs: list[float | None], what: str) -> str:
    """An unknown price is reported as such, never added in as $0."""
    known = [c for c in costs if c is not None]
    text = f"${sum(known):.4f} over {len(costs)} {what}"
    if unpriced := len(costs) - len(known):
        text += f", {unpriced} unpriced"
    return text


def _echo_spend(trials: list[TrialSummary], agents: bool) -> None:
    parts = [_total([t.record.cost_usd for t in trials], "trials")] if agents else []
    judge_costs = [s.cost_usd for t in trials for s in t.judge_spend]
    if judge_costs:
        parts.append("judges " + _total(judge_costs, "calls"))
    if parts:
        typer.echo("spend: " + "; ".join(parts))


def _echo_routing(files: list[Path], trials: list[TrialSummary]) -> None:
    """Only trials whose driver could report skills count, so silence isn't read as a miss."""
    cases = {case.name: case for case in map(CaseLoader().load, files)}
    routed = [
        RoutedTrial.of(cases[t.case], t.skills)
        for t in trials
        if any(g.name == EXPECT and g.verdict != "n/a" for g in t.grades)
    ]
    labelled = [r for r in routed if r is not None]
    if labelled:
        typer.echo(f"routing over {len(labelled)} trials:")
        for line in Routing.of(labelled).summary():
            typer.echo(f"  {line}")


@app.command()
def rejudge(
    run_id: Annotated[str, typer.Argument(help="The stored run to regrade.")],
    paths: Annotated[
        list[Path],
        typer.Argument(
            exists=True, help=f"The run's case files, or directories of {CASE_FILENAME}."
        ),
    ],
    judge: JudgeOption = None,
) -> None:
    """Regrade a stored run's judge graders from its stored events, without running any agent."""
    loader = CaseLoader()
    try:
        cases = {c.name: c for c in map(loader.load, CaseLoader.discover(paths))}
        trial_judge = TrialJudge.for_cases(cases.values(), judge, _judge_cache())
    except CaseLoadError as exc:
        for issue in exc.issues:
            typer.echo(str(issue), err=True)
        raise typer.Exit(2) from exc
    except JudgeSetupError as exc:
        typer.echo(f"judge: {exc}", err=True)
        raise typer.Exit(2) from exc
    stores = open_stores(StorageConfig.load(Path.cwd()))
    try:
        trials = asyncio.run(
            Rejudger(stores, DEFAULT_WORKSPACE, trial_judge).rejudge(run_id, cases)
        )
    except LookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    finally:
        stores.results.close()
    for trial in trials:
        verdict = "PASS" if trial.record.passed else "FAIL"
        typer.echo(f"{verdict} {trial.case} #{trial.record.repeat}")
        judged = {g.name for g in cases[trial.case].graders if g.type == "judge"}
        for grade in trial.grades:
            if grade.name in judged:
                typer.echo(f"    {grade.name}: {grade.verdict}, {grade.detail}")
    _echo_spend(trials, agents=False)
    typer.echo(f"run {run_id}: rejudged {len(trials)} trials")


@app.command()
def plan(
    paths: Annotated[
        list[Path],
        typer.Argument(
            exists=True, help=f"Case files, or directories to search for {CASE_FILENAME}."
        ),
    ],
    runs: Annotated[int | None, typer.Option(min=1, help="Repeats per case.")] = None,
    driver: Annotated[DriverName, typer.Option(help="The agent to plan for.")] = DriverName.claude,
    ablation: Annotated[
        bool, typer.Option(help="Plan the without-bundle arm too, as run --ablation would.")
    ] = False,
    harness: HarnessOption = None,
) -> None:
    """Validate cases and estimate a run's cost from stored runs; no agent starts."""
    setups = _load_setups(harness)
    support = DriverSupport(driver.value, DRIVERS[driver].capabilities)
    for setup in filter(None, setups):
        if gap := support.harness_gap(setup):
            typer.echo(gap, err=True)
            raise typer.Exit(2)
    files = CaseLoader.discover(paths)
    if not files:
        typer.echo(f"no {CASE_FILENAME} found under: {', '.join(map(str, paths))}", err=True)
        raise typer.Exit(2)
    loader = CaseLoader()
    try:
        cases = [loader.load(path) for path in files]
    except CaseLoadError as exc:
        for issue in exc.issues:
            typer.echo(str(issue), err=True)
        raise typer.Exit(2) from exc
    stores = open_stores(StorageConfig.load(Path.cwd()))
    try:
        history = CostHistory.load(stores.results, DEFAULT_WORKSPACE, driver.value)
    finally:
        stores.results.close()
    planner = Planner(support, history)
    for line in Planner.summary(planner.estimate(cases, runs, ablation, len(setups))):
        typer.echo(line)


@app.command()
def compare(
    run_id: Annotated[str, typer.Argument(help="The stored run to compare.")],
    baseline: Annotated[
        str | None,
        typer.Argument(help="A second stored run to compare against; default: the run's own arms."),
    ] = None,
    harness: Annotated[
        list[str] | None,
        typer.Option(
            "--harness",
            help="Two harnesses of the run, baseline first, to compare with each other.",
        ),
    ] = None,
) -> None:
    """Compare scores per case: a run's arms or harnesses, or a run against a baseline run."""
    if harness and baseline:
        typer.echo("--harness compares within one run; leave out the baseline run", err=True)
        raise typer.Exit(2)
    stores = open_stores(StorageConfig.load(Path.cwd()))
    try:
        loaded = {}
        for rid in filter(None, (run_id, baseline)):
            if stores.results.get_run(DEFAULT_WORKSPACE, rid) is None:
                typer.echo(f"no stored run {rid!r}", err=True)
                raise typer.Exit(2)
            loaded[rid] = (
                stores.results.list_trials(DEFAULT_WORKSPACE, rid),
                stores.results.list_grades(DEFAULT_WORKSPACE, rid),
            )
    finally:
        stores.results.close()
    trials, grades = loaded[run_id]
    names = sorted({_harness_of(t) for t in trials})
    if baseline is None and (harness or len(names) > 1):
        if len(harness or []) != 2 or not set(harness or []) <= set(names):
            typer.echo(
                f"run {run_id} holds harnesses {', '.join(names)}; "
                "name two with --harness BASELINE --harness OTHER",
                err=True,
            )
            raise typer.Exit(2)
        assert harness is not None
        with_trials = [t for t in trials if t.arm == "with"]
        _echo_harness_comparison(with_trials, grades, harness)
        return
    if baseline is None:
        with_arm = arm_scores([t for t in trials if t.arm == "with"], grades, True)
        without_arm = arm_scores([t for t in trials if t.arm == "without"], grades, True)
        lines = Comparison.of(with_arm, without_arm).summary("with", "without")
    else:
        base_trials, base_grades = loaded[baseline]
        current = arm_scores([t for t in trials if t.arm == "with"], grades, False)
        previous = arm_scores([t for t in base_trials if t.arm == "with"], base_grades, False)
        lines = Comparison.of(current, previous).summary(run_id[-8:], baseline[-8:])
    for line in lines:
        typer.echo(line)


@app.command("push")
def push_command(
    run_id: Annotated[str, typer.Argument(help="The stored run to upload.")],
) -> None:
    """Upload a stored run to $MZEVAL_SERVER with $MZEVAL_TOKEN; resending a run is safe."""
    if not _push_run(_push_target(), run_id):
        raise typer.Exit(2)


@app.command()
def report(
    run_id: Annotated[
        str | None, typer.Option("--run", help="Run id (default: the latest stored run).")
    ] = None,
    out: Annotated[Path, typer.Option(help="Where to write the HTML.")] = Path("report.html"),
    threshold: Annotated[
        float, typer.Option(min=0.01, max=1.0, help="Pass rate each case needs; 1.0 is every run.")
    ] = 1.0,
) -> None:
    """Write a static HTML report of a stored run, with the trend across stored runs."""
    stores = open_stores(StorageConfig.load(Path.cwd()))
    try:
        html = ReportBuilder(stores.results, DEFAULT_WORKSPACE, threshold).build(run_id)
    except LookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    finally:
        stores.results.close()
    out.write_text(html, encoding="utf-8")
    typer.echo(f"wrote {out}")


@app.command()
def schema(
    harness: Annotated[
        bool, typer.Option("--harness", help=f"Print the schema of {HARNESS_FILENAME} instead.")
    ] = False,
) -> None:
    """Print the JSON Schema of the case format, for editors and other tools."""
    model = HarnessManifest if harness else Case
    typer.echo(json.dumps(model.model_json_schema(by_alias=True), indent=2))


@import_app.command("claude-plugin-eval")
def import_claude_plugin_eval(
    roots: Annotated[
        list[Path],
        typer.Argument(
            exists=True, file_okay=False, help="Directories holding 1.1 case.yaml files."
        ),
    ],
    out: Annotated[
        Path | None,
        typer.Option(help="Write cases under this directory instead of next to the originals."),
    ] = None,
    report: Annotated[
        Path | None,
        typer.Option(help="Report path (default: migration-report.md in --out or here)."),
    ] = None,
    force: Annotated[bool, typer.Option(help="Overwrite existing v2 case files.")] = False,
) -> None:
    """Convert claude plugin eval 1.1 cases to mzeval/2; the originals are left in place."""
    summary = ClaudePluginEvalImporter().import_tree(roots, out, force=force)
    report_path = report or (out or Path.cwd()) / "migration-report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(summary), encoding="utf-8")
    typer.echo(
        f"{len(summary.written)} cases written, {len(summary.existing)} skipped (already exist), "
        f"{len(summary.flags)} graders flagged; report: {report_path}"
    )
    for error in summary.errors:
        typer.echo(error, err=True)
    if summary.errors:
        typer.echo(f"{len(summary.errors)} problems; those cases were not converted", err=True)
        raise typer.Exit(1)


@harness_import_app.command("claude")
def import_claude_harness(
    dest: Annotated[Path, typer.Argument(help="New or empty directory for the harness.")],
    name: Annotated[
        str | None, typer.Option(help="Harness name; defaults to the directory name.")
    ] = None,
    source: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            file_okay=False,
            help="Claude config dir; default $CLAUDE_CONFIG_DIR or ~/.claude.",
        ),
    ] = None,
    part: Annotated[
        list[ClaudePart] | None,
        typer.Option("--part", help="Copy only this part; repeatable. Default: all of them."),
    ] = None,
) -> None:
    """Copy CLAUDE.md, rules, skills, agents and hooks into a harness.

    Logins, history, projects/, .claude.json and settings other than hooks are never copied;
    MCP servers and plugins go into harness.mzeval.yaml by hand.
    """
    config = source or Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    importer = ClaudeHarnessImporter(config, Path.home())
    try:
        summary = importer.run(dest, name or dest.name, part or list(ClaudePart))
    except FileExistsError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    except HarnessLoadError as exc:
        for issue in exc.issues:
            typer.echo(str(issue), err=True)
        raise typer.Exit(2) from exc
    for warning in summary.warnings:
        typer.echo(f"warning: {warning}", err=True)
    copied = ", ".join(f"{count} {kind}" for kind, count in summary.copied.items())
    typer.echo(f"harness {name or dest.name} at {dest}: {copied}")
    typer.echo("MCP servers and plugins aren't imported; add them to " + HARNESS_FILENAME)
