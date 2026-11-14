import asyncio
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import JsonValue

from mozg_eval import __version__
from mozg_eval.drivers.claude import ClaudeDriver
from mozg_eval.drivers.codex import CodexDriver
from mozg_eval.drivers.opencode import OpenCodeDriver
from mozg_eval.grading.stats import SuiteStats
from mozg_eval.grading.support import DriverSupport
from mozg_eval.importers.claude_plugin_eval import (
    CaseImportError,
    ClaudePluginEvalImporter,
    render_report,
)
from mozg_eval.judge.cache import JudgeCache
from mozg_eval.judge.grading import JudgeSetupError, TrialJudge
from mozg_eval.report.html import ReportBuilder
from mozg_eval.runner.rejudge import Rejudger
from mozg_eval.runner.suite import FixtureError, SuiteRunner
from mozg_eval.spec.loader import CASE_FILENAME, CaseLoader, CaseLoadError
from mozg_eval.spec.models import Case
from mozg_eval.storage.base import DEFAULT_WORKSPACE
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


DRIVERS: dict[DriverName, type[ClaudeDriver | CodexDriver | OpenCodeDriver]] = {
    DriverName.claude: ClaudeDriver,
    DriverName.codex: CodexDriver,
    DriverName.opencode: OpenCodeDriver,
}

import_app = typer.Typer(help="Convert cases from other eval tools.", no_args_is_help=True)
app.add_typer(import_app, name="import")

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
            exists=True, help=f"Case files, or directories to search for {CASE_FILENAME}."
        ),
    ],
) -> None:
    """Validate case files; exit 1 if any is invalid."""
    loader = CaseLoader()
    files = loader.discover(paths)
    if not files:
        typer.echo(f"no {CASE_FILENAME} found under: {', '.join(map(str, paths))}", err=True)
        raise typer.Exit(2)

    invalid = 0
    for path in files:
        try:
            loader.load(path)
        except CaseLoadError as exc:
            invalid += 1
            for issue in exc.issues:
                typer.echo(str(issue), err=True)
    typer.echo(f"{len(files) - invalid} of {len(files)} case files valid")
    if invalid:
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
) -> None:
    """Run and grade cases on one agent.

    Exit 0 when every case meets the threshold, 1 when one doesn't, 2 when a trial errored.
    """
    files = CaseLoader.discover(paths)
    if not files:
        typer.echo(f"no {CASE_FILENAME} found under: {', '.join(map(str, paths))}", err=True)
        raise typer.Exit(2)
    overrides: dict[str, JsonValue] = {
        key: value for key, value in {"runs": runs, "model": model}.items() if value is not None
    }
    stores = open_stores(StorageConfig.load(Path.cwd()))
    command = executable or driver.value
    suite = SuiteRunner(
        stores,
        DriverSupport(driver.value, DRIVERS[driver].capabilities),
        lambda home, raw_dir: DRIVERS[driver](home, raw_dir, executable=command),
        DEFAULT_WORKSPACE,
        overrides,
        lambda cases: TrialJudge.for_cases(cases, judge, _judge_cache()),
    )
    try:
        record, trials = asyncio.run(suite.run(files))
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
    finally:
        stores.results.close()
    for trial in trials:
        cost = "n/a" if trial.record.cost_usd is None else f"${trial.record.cost_usd:.4f}"
        skills = ", ".join(trial.skills) or "none"
        verdict = "PASS" if trial.record.passed else "FAIL"
        typer.echo(
            f"{verdict} {trial.case} #{trial.record.repeat}: {trial.record.status}, "
            f"skills: {skills}, {cost}"
        )
        for grade in trial.grades:
            if grade.verdict == "fail":
                typer.echo(f"    {grade.name}: {grade.detail}")
    stats = SuiteStats.of([t.record for t in trials], threshold)
    rate, low, high = stats.pass_rate()
    typer.echo(
        f"run {record.id}: {len(trials)} trials, {len(stats.cases)} cases, "
        f"{rate:.0%} at or above {threshold:.0%} (95% CI {low:.0%}-{high:.0%})"
    )
    if any(trial.record.status in ("error", "timeout") for trial in trials):
        raise typer.Exit(2)
    if stats.failing():
        typer.echo("below threshold: " + ", ".join(c.case for c in stats.failing()))
        raise typer.Exit(1)


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
    typer.echo(f"run {run_id}: rejudged {len(trials)} trials")


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
def schema() -> None:
    """Print the JSON Schema of the case format, for editors and other tools."""
    typer.echo(json.dumps(Case.model_json_schema(by_alias=True), indent=2))


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
    try:
        summary = ClaudePluginEvalImporter().import_tree(roots, out, force=force)
    except CaseImportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    report_path = report or (out or Path.cwd()) / "migration-report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(summary), encoding="utf-8")
    typer.echo(
        f"{len(summary.written)} cases written, {len(summary.existing)} skipped (already exist), "
        f"{len(summary.flags)} graders flagged; report: {report_path}"
    )
