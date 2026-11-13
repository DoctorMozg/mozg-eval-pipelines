import json
from pathlib import Path
from typing import Annotated

import typer

from mozg_eval import __version__
from mozg_eval.importers.claude_plugin_eval import (
    CaseImportError,
    ClaudePluginEvalImporter,
    render_report,
)
from mozg_eval.spec.loader import CASE_FILENAME, CaseLoader, CaseLoadError
from mozg_eval.spec.models import Case

app = typer.Typer(
    name="mzeval",
    help="Run evals for coding-agent plugins and skills on Claude Code, Codex, Pi and OpenCode.",
    no_args_is_help=True,
)
import_app = typer.Typer(help="Convert cases from other eval tools.", no_args_is_help=True)
app.add_typer(import_app, name="import")


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
