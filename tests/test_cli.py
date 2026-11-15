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
        assert "9 of 9 case files valid" in result.stdout

    def test_invalid_case_fails_with_file_and_line(self, runner: CliRunner, tmp_path: Path) -> None:
        case = tmp_path / "case.mzeval.yaml"
        case.write_text("schema: mzeval/2\nname: x\nkind: gate\nexecution: {prompt: p}\nbogus: 1\n")

        result = runner.invoke(app, ["lint", str(tmp_path)])

        assert result.exit_code == 1
        assert f"{case}:5: unknown key 'bogus' in <case>" in result.stderr

    def test_warning_is_printed_but_does_not_fail(self, runner: CliRunner, tmp_path: Path) -> None:
        hooks = tmp_path / "plugin" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [], "SessionStart": []}}))
        (tmp_path / "case.mzeval.yaml").write_text(
            "schema: mzeval/2\nname: x\nkind: behaviour\nbundle: [plugin]\nexecution: {prompt: p}\n"
            "graders:\n  - {name: fired, type: hook_fired}\n"
        )

        result = runner.invoke(app, ["lint", str(tmp_path)])

        assert result.exit_code == 0
        assert "warning: " in result.stderr
        assert "hook_fired grader fired has no event" in result.stderr

    def test_directory_without_cases_is_a_usage_error(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(app, ["lint", str(tmp_path)])

        assert result.exit_code == 2

    def test_harnesses_are_linted_next_to_cases(self, runner: CliRunner, tmp_path: Path) -> None:
        good, bad = tmp_path / "good", tmp_path / "bad"
        for root, name in ((good, "good"), (bad, "bad")):
            root.mkdir()
            manifest = f"schema: mzeval-harness/1\nname: {name}\n"
            (root / "harness.mzeval.yaml").write_text(manifest)
        (bad / "settings.json").write_text("{}")

        result = runner.invoke(app, ["lint", str(tmp_path)])

        assert result.exit_code == 1
        assert "1 of 2 harnesses valid" in result.stdout
        assert f"{bad / 'settings.json'}: unexpected entry in a harness" in result.stderr


class TestSchema:
    def test_schema_is_json_with_the_case_keys(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["schema"])

        schema = json.loads(result.stdout)
        assert {"schema", "name", "kind", "execution", "graders"} <= schema["properties"].keys()
        assert schema["additionalProperties"] is False

    def test_harness_schema_lists_the_manifest_keys(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["schema", "--harness"])

        schema = json.loads(result.stdout)
        assert {"schema", "name", "plugins", "mcp"} <= schema["properties"].keys()
