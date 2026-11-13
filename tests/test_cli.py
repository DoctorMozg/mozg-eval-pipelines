import json
from importlib.metadata import version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mozg_eval.cli import app

EXAMPLES = Path(__file__).parents[1] / "examples"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestCli:
    def test_version_matches_package_metadata(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--version"])

        assert result.exit_code == 0
        assert result.stdout.strip() == f"mzeval {version('mozg-eval')}"

    def test_help_names_the_program(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        assert "mzeval" in result.stdout

    def test_no_arguments_shows_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, [])

        assert "Usage" in result.stdout


class TestLint:
    def test_examples_are_valid(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["lint", str(EXAMPLES)])

        assert result.exit_code == 0
        assert "3 of 3 case files valid" in result.stdout

    def test_invalid_case_fails_with_file_and_line(self, runner: CliRunner, tmp_path: Path) -> None:
        case = tmp_path / "case.mzeval.yaml"
        case.write_text("schema: mzeval/2\nname: x\nkind: gate\nexecution: {prompt: p}\nbogus: 1\n")

        result = runner.invoke(app, ["lint", str(tmp_path)])

        assert result.exit_code == 1
        assert f"{case}:5: unknown key 'bogus' in <case>" in result.stderr

    def test_directory_without_cases_is_a_usage_error(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(app, ["lint", str(tmp_path)])

        assert result.exit_code == 2


class TestSchema:
    def test_schema_is_json_with_the_case_keys(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["schema"])

        schema = json.loads(result.stdout)
        assert {"schema", "name", "kind", "execution", "graders"} <= schema["properties"].keys()
        assert schema["additionalProperties"] is False
