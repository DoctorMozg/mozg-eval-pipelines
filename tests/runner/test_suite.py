import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mozg_eval.cli import app
from mozg_eval.events.models import CheckResult, End, Event
from mozg_eval.storage.base import ArtifactRef
from mozg_eval.storage.config import StorageConfig, open_stores

SKILL_FIRE = Path(__file__).parents[1] / "drivers" / "fixtures" / "claude_skill_fire.jsonl"


@pytest.fixture
def suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A one-case suite whose fixture writes a file, run from tmp_path so stores land there."""
    case_dir = tmp_path / "evals" / "remember-decision"
    case_dir.mkdir(parents=True)
    (tmp_path / "plugins" / "mz-memory").mkdir(parents=True)
    (case_dir / "fixture.sh").write_text("echo seeded > seed.txt\n")
    (case_dir / "case.mzeval.yaml").write_text(
        f"""\
schema: mzeval/2
name: remember-decision
kind: fire
expect:
  skill: memory-note
bundle: [../../plugins/mz-memory]
fixture: fixture.sh
execution:
  prompt: Remember this decision.
  tools: [skill]
  runs: 2
  env:
    FAKE_FIXTURE: {SKILL_FIRE}
    FAKE_RECORD: {tmp_path / "record.json"}
"""
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "real-claude"))
    return case_dir


class TestRunCommand:
    def test_runs_each_repeat_and_stores_everything(
        self, suite: Path, fake_claude: Path, tmp_path: Path
    ) -> None:
        result = CliRunner().invoke(app, ["run", str(suite), "--claude", str(fake_claude)])

        assert result.exit_code == 0, result.output
        assert result.output.count("remember-decision #") == 2
        assert "ok, skills: memory-note, $0.0040" in result.output
        stores = open_stores(StorageConfig.load(tmp_path, env={}))
        (run,) = stores.results.list_runs("local")
        trials = stores.results.list_trials("local", run.id)
        assert sorted(t.repeat for t in trials) == [1, 2]
        raw = ArtifactRef(run=run.id, trial=trials[0].id, name="raw.jsonl")

        async def read_raw() -> bytes:
            return b"".join([chunk async for chunk in stores.artifacts.get("local", raw)])

        assert asyncio.run(read_raw()) == SKILL_FIRE.read_bytes()
        stores.results.close()

    def test_trial_gets_a_fresh_workspace_and_a_home_removed_after(
        self, suite: Path, fake_claude: Path, tmp_path: Path
    ) -> None:
        CliRunner().invoke(app, ["run", str(suite), "--runs", "1", "--claude", str(fake_claude)])
        record = json.loads((tmp_path / "record.json").read_text())

        assert record["files"] == [".git", "seed.txt"]
        assert f"--plugin-dir={tmp_path / 'plugins' / 'mz-memory'}" in record["argv"]
        assert not Path(record["env"]["HOME"]).exists()

    def test_failing_fixture_exits_2_with_its_output(self, suite: Path, fake_claude: Path) -> None:
        (suite / "fixture.sh").write_text("echo no-such-thing >&2; exit 3\n")

        result = CliRunner().invoke(app, ["run", str(suite), "--claude", str(fake_claude)])

        assert result.exit_code == 2
        assert "exited with 3: no-such-thing" in result.output

    def test_case_that_excludes_the_driver_is_skipped(self, suite: Path, fake_claude: Path) -> None:
        case = suite / "case.mzeval.yaml"
        case.write_text(case.read_text() + "drivers:\n  exclude: [claude]\n")

        result = CliRunner().invoke(app, ["run", str(suite), "--claude", str(fake_claude)])

        assert result.exit_code == 0
        assert "0 trials" in result.output

    def test_workspace_checks_are_stored_after_end(
        self, suite: Path, fake_claude: Path, tmp_path: Path
    ) -> None:
        case = suite / "case.mzeval.yaml"
        case.write_text(
            case.read_text()
            + "graders:\n  - {name: seeded, type: command, run: test -f seed.txt}\n"
        )
        CliRunner().invoke(app, ["run", str(suite), "--runs", "1", "--claude", str(fake_claude)])
        stores = open_stores(StorageConfig.load(tmp_path, env={}))
        (run,) = stores.results.list_runs("local")
        (trial,) = stores.results.list_trials("local", run.id)
        stores.results.close()

        async def read() -> list[Event]:
            return [e async for e in stores.events.read("local", run.id, trial.id)]

        *_, end, check = asyncio.run(read())
        assert isinstance(end.payload, End)
        assert check.payload == CheckResult(name="seeded", passed=True, detail="")
