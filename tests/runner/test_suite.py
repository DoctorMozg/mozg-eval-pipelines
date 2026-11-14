import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import IngestStub
from pydantic_ai.models.function import FunctionModel
from typer.testing import CliRunner

from mozg_eval.cli import app
from mozg_eval.events.models import CheckResult, End, Event
from mozg_eval.judge.cache import JudgeCache
from mozg_eval.judge.grading import TrialJudge
from mozg_eval.judge.verdict import CriterionJudge
from mozg_eval.runner.rejudge import Rejudger
from mozg_eval.runner.suite import SuiteRunner
from mozg_eval.spec.loader import CaseLoader
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
        result = CliRunner().invoke(app, ["run", str(suite), "--executable", str(fake_claude)])

        assert result.exit_code == 0, result.output
        assert result.output.count("remember-decision #") == 2
        assert "ok, skills: memory-note, $0.0040" in result.output
        assert "routing over 2 trials:" in result.output
        assert "memory-note: precision 1.00, recall 1.00, F1 1.00" in result.output
        assert "spend: $0.0079 over 2 trials" in result.output
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
        CliRunner().invoke(
            app, ["run", str(suite), "--runs", "1", "--executable", str(fake_claude)]
        )
        record = json.loads((tmp_path / "record.json").read_text())

        assert record["files"] == [".git", "seed.txt"]
        assert f"--plugin-dir={tmp_path / 'plugins' / 'mz-memory'}" in record["argv"]
        assert not Path(record["env"]["HOME"]).exists()

    def test_failing_fixture_exits_2_with_its_output(self, suite: Path, fake_claude: Path) -> None:
        (suite / "fixture.sh").write_text("echo no-such-thing >&2; exit 3\n")

        result = CliRunner().invoke(app, ["run", str(suite), "--executable", str(fake_claude)])

        assert result.exit_code == 2
        assert "exited with 3: no-such-thing" in result.output

    def test_case_that_excludes_the_driver_is_skipped(self, suite: Path, fake_claude: Path) -> None:
        case = suite / "case.mzeval.yaml"
        case.write_text(case.read_text() + "drivers:\n  exclude: [claude]\n")

        result = CliRunner().invoke(app, ["run", str(suite), "--executable", str(fake_claude)])

        assert result.exit_code == 0
        assert "0 trials" in result.output

    def test_unusable_judge_stops_the_run_before_any_agent(
        self, suite: Path, fake_claude: Path, tmp_path: Path
    ) -> None:
        result = CliRunner().invoke(
            app, ["run", str(suite), "--judge", "nonsense", "--executable", str(fake_claude)]
        )

        assert result.exit_code == 2
        assert "judge: Unknown model: nonsense" in result.output
        assert not (tmp_path / "record.json").exists()

    def test_missing_agent_executable_stops_the_run_before_anything_is_stored(
        self, suite: Path, tmp_path: Path
    ) -> None:
        missing = tmp_path / "no-such-claude"

        result = CliRunner().invoke(app, ["run", str(suite), "--executable", str(missing)])

        assert result.exit_code == 2
        assert result.output == f"agent not found: {missing}; install it or pass --executable\n"
        assert not (tmp_path / ".mzeval").exists()

    def test_relative_executable_still_runs_from_the_trial_workspace(
        self, suite: Path, fake_claude: Path
    ) -> None:
        relative = f"./{fake_claude.name}"

        result = CliRunner().invoke(app, ["run", str(suite), "--executable", relative])

        assert result.exit_code == 0, result.output
        assert result.output.count("PASS remember-decision #") == 2

    def test_interrupt_exits_130_not_the_below_threshold_code(
        self, suite: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def interrupted(*_: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(SuiteRunner, "run", interrupted)

        result = CliRunner().invoke(app, ["run", str(suite), "--executable", str(fake_claude)])

        assert result.exit_code == 130
        assert "interrupted; trials that finished are stored" in result.output

    def test_workspace_checks_are_stored_after_end(
        self, suite: Path, fake_claude: Path, tmp_path: Path
    ) -> None:
        case = suite / "case.mzeval.yaml"
        case.write_text(
            case.read_text()
            + "graders:\n  - {name: seeded, type: command, run: test -f seed.txt}\n"
        )
        CliRunner().invoke(
            app, ["run", str(suite), "--runs", "1", "--executable", str(fake_claude)]
        )
        stores = open_stores(StorageConfig.load(tmp_path, env={}))
        (run,) = stores.results.list_runs("local")
        (trial,) = stores.results.list_trials("local", run.id)
        stores.results.close()

        async def read() -> list[Event]:
            return [e async for e in stores.events.read("local", run.id, trial.id)]

        *_, end, check = asyncio.run(read())
        assert isinstance(end.payload, End)
        assert check.payload == CheckResult(name="seeded", passed=True, detail="")

    def test_wrong_skill_fails_the_case_and_exits_1(
        self, suite: Path, fake_claude: Path, tmp_path: Path
    ) -> None:
        case = suite / "case.mzeval.yaml"
        case.write_text(case.read_text().replace("skill: memory-note", "skill: lessons-promote"))

        result = CliRunner().invoke(app, ["run", str(suite), "--executable", str(fake_claude)])

        assert result.exit_code == 1, result.output
        assert "FAIL remember-decision #1" in result.output
        assert "expect: fired: memory-note" in result.output
        assert "below threshold: remember-decision" in result.output
        stores = open_stores(StorageConfig.load(tmp_path, env={}))
        (run,) = stores.results.list_runs("local")
        grades = stores.results.list_grades("local", run.id)
        stores.results.close()
        assert [g.verdict for gs in grades.values() for g in gs] == ["fail", "fail"]

    def test_codex_driver_runs_the_case_with_inferred_skills(
        self, suite: Path, fake_codex: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "real-codex"))
        manifest = tmp_path / "plugins" / "mz-memory" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir()
        manifest.write_text('{"name": "mz-memory"}')
        case = suite / "case.mzeval.yaml"
        codex_fire = SKILL_FIRE.with_name("codex_skill_fire.jsonl")
        case.write_text(
            case.read_text()
            .replace("skill: memory-note", "skill: probe-skill")
            .replace(str(SKILL_FIRE), str(codex_fire))
        )

        result = CliRunner().invoke(
            app,
            [
                "run",
                str(suite),
                "--runs",
                "1",
                "--driver",
                "codex",
                "--executable",
                str(fake_codex),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "PASS remember-decision #1: ok, skills: probe-skill, n/a" in result.output
        stores = open_stores(StorageConfig.load(tmp_path, env={}))
        (run,) = stores.results.list_runs("local")
        (trial,) = stores.results.list_trials("local", run.id)
        stores.results.close()
        assert trial.driver == "codex"

    @pytest.mark.parametrize(("driver", "cost"), [("opencode", "$0.0011"), ("pi", "$0.0003")])
    def test_driver_without_a_sandbox_runs_a_skill_case(
        self, suite: Path, fake_agent: Path, tmp_path: Path, driver: str, cost: str
    ) -> None:
        manifest = tmp_path / "plugins" / "mz-memory" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir()
        manifest.write_text('{"name": "mz-memory"}')
        case = suite / "case.mzeval.yaml"
        fixture = SKILL_FIRE.with_name(f"{driver}_skill_fire.jsonl")
        case.write_text(
            case.read_text()
            .replace("skill: memory-note", "skill: probe-skill")
            .replace(str(SKILL_FIRE), str(fixture))
        )
        argv = ["run", str(suite), "--runs", "1", "--driver", driver]

        result = CliRunner().invoke(app, [*argv, "--executable", str(fake_agent)])

        assert result.exit_code == 0, result.output
        assert f"PASS remember-decision #1: ok, skills: probe-skill, {cost}" in result.output

    def test_shell_case_is_skipped_on_a_driver_without_a_sandbox(
        self, suite: Path, fake_agent: Path, tmp_path: Path
    ) -> None:
        case = suite / "case.mzeval.yaml"
        case.write_text(case.read_text().replace("tools: [skill]", "tools: [skill, shell]"))
        argv = ["run", str(suite), "--driver", "opencode", "--executable", str(fake_agent)]

        result = CliRunner().invoke(app, argv)

        assert result.exit_code == 0, result.output
        assert "0 trials" in result.output
        assert not (tmp_path / "record.json").exists()

    def test_junit_report_and_skip_reasons_are_written(
        self, suite: Path, fake_claude: Path, tmp_path: Path
    ) -> None:
        junit = tmp_path / "out" / "junit.xml"
        argv = ["run", str(suite), "--driver", "opencode", "--executable", str(fake_claude)]
        case = suite / "case.mzeval.yaml"
        case.write_text(case.read_text().replace("tools: [skill]", "tools: [skill, shell]"))

        result = CliRunner().invoke(app, [*argv, "--junit", str(junit)])

        assert result.exit_code == 0, result.output
        assert "SKIP remember-decision: opencode has no sandbox" in result.output
        assert 'skipped="1"' in junit.read_text()

    @pytest.mark.parametrize("fail", [None, "/v1/runs"])
    def test_push_failure_is_reported_but_the_eval_decides_the_exit_code(
        self, suite: Path, fake_claude: Path, ingest_server: IngestStub, fail: str | None
    ) -> None:
        ingest_server.fail = fail
        argv = ["run", str(suite), "--runs", "1", "--push", "--executable", str(fake_claude)]

        result = CliRunner().invoke(app, argv)

        assert result.exit_code == 0, result.output
        if fail is None:
            assert "pushed run" in result.output
            artifacts = [r.path.rsplit("/", 1)[1] for r in ingest_server.requests[-2:]]
            assert artifacts == ["raw.jsonl", "stderr.log"]
        else:
            assert "push failed: PUT /v1/runs/" in result.output
            assert "retry with: mzeval push" in result.output

    def test_case_with_nothing_codex_can_grade_is_skipped(
        self, suite: Path, fake_codex: Path, tmp_path: Path
    ) -> None:
        case = suite / "case.mzeval.yaml"
        case.write_text(
            case.read_text().replace(
                "kind: fire\nexpect:\n  skill: memory-note\n", "kind: behaviour\n"
            )
            + "graders:\n  - {name: spawned, type: subagent, subagent: researcher}\n"
        )

        result = CliRunner().invoke(
            app, ["run", str(suite), "--driver", "codex", "--executable", str(fake_codex)]
        )

        assert result.exit_code == 0, result.output
        assert "0 trials" in result.output
        assert not (tmp_path / "record.json").exists()


class TestPlanCommand:
    def test_estimate_comes_from_stored_runs_and_spends_nothing(
        self, suite: Path, fake_claude: Path, tmp_path: Path
    ) -> None:
        before = CliRunner().invoke(app, ["plan", str(suite)])
        CliRunner().invoke(app, ["run", str(suite), "--executable", str(fake_claude)])
        (tmp_path / "record.json").unlink()

        after = CliRunner().invoke(app, ["plan", str(suite), "--runs", "3"])

        assert before.exit_code == 0, before.output
        assert "remember-decision: 2 trials, no stored cost" in before.output
        assert "plan: 1 cases, 2 trials, no stored cost to estimate from" in before.output
        assert "remember-decision: 3 trials x $0.0040 = $0.0119" in after.output
        assert "plan: 1 cases, 3 trials, estimated $0.0119" in after.output
        assert not (tmp_path / "record.json").exists()

    def test_invalid_case_exits_2(self, suite: Path) -> None:
        case = suite / "case.mzeval.yaml"
        case.write_text(case.read_text() + "surprise: 1\n")

        result = CliRunner().invoke(app, ["plan", str(suite)])

        assert result.exit_code == 2
        assert "surprise" in result.output


class TestAblation:
    @pytest.fixture
    def plugin_case(self, suite: Path, tmp_path: Path) -> Path:
        """Adds a grader that passes only when the agent was started with the bundle."""
        case = suite / "case.mzeval.yaml"
        record = tmp_path / "record.json"
        case.write_text(
            case.read_text()
            + f"graders:\n  - {{name: loaded, type: command, run: grep -q plugin-dir {record}}}\n"
        )
        return suite

    def stored_run(self, tmp_path: Path) -> str:
        stores = open_stores(StorageConfig.load(tmp_path, env={}))
        *_, run = stores.results.list_runs("local")
        stores.results.close()
        return run.id

    def test_without_arm_drops_the_bundle_and_is_compared(
        self, plugin_case: Path, fake_claude: Path, tmp_path: Path
    ) -> None:
        argv = ["run", str(plugin_case), "--ablation", "--executable", str(fake_claude)]

        result = CliRunner().invoke(app, argv)

        assert result.exit_code == 0, result.output
        assert result.output.count("[without]") == 2
        assert "remember-decision: with 1.00, without 0.00, delta +1.00" in result.output
        assert "delta score +1.00 (95% CI +1.00 to +1.00) over 1 cases" in result.output
        assert "1 cases, 100% at or above" in result.output

    def test_compare_reads_the_arms_back_from_the_store(
        self, plugin_case: Path, fake_claude: Path, tmp_path: Path
    ) -> None:
        argv = ["run", str(plugin_case), "--ablation", "--runs", "1"]
        CliRunner().invoke(app, [*argv, "--executable", str(fake_claude)])

        result = CliRunner().invoke(app, ["compare", self.stored_run(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "over 1 cases, with vs without; exact McNemar p=1" in result.output

    def test_compare_against_a_baseline_run(
        self, plugin_case: Path, fake_claude: Path, tmp_path: Path
    ) -> None:
        argv = ["run", str(plugin_case), "--runs", "1", "--executable", str(fake_claude)]
        CliRunner().invoke(app, argv)
        baseline = self.stored_run(tmp_path)
        CliRunner().invoke(app, argv)

        result = CliRunner().invoke(app, ["compare", self.stored_run(tmp_path), baseline])
        missing = CliRunner().invoke(app, ["compare", "nope"])

        assert "delta score +0.00 (95% CI +0.00 to +0.00) over 1 cases" in result.output
        assert missing.exit_code == 2
        assert "no stored run 'nope'" in missing.output

    def test_case_graded_only_with_the_bundle_gets_no_without_arm(
        self, suite: Path, fake_claude: Path
    ) -> None:
        argv = ["run", str(suite), "--ablation", "--executable", str(fake_claude)]

        result = CliRunner().invoke(app, argv)

        assert result.exit_code == 0, result.output
        assert "[without]" not in result.output
        assert "no case was graded in both arms" in result.output


class TestRejudge:
    @pytest.fixture
    def stored_run(self, suite: Path, fake_claude: Path, tmp_path: Path) -> str:
        """One trial stored with an unjudged judge grader, since no --judge was given."""
        case = suite / "case.mzeval.yaml"
        case.write_text(
            case.read_text()
            + "graders:\n  - {name: listing, type: judge, checklist: [no twist], focus: final}\n"
        )
        result = CliRunner().invoke(
            app, ["run", str(suite), "--runs", "1", "--executable", str(fake_claude)]
        )
        assert result.exit_code == 0, result.output
        stores = open_stores(StorageConfig.load(tmp_path, env={}))
        (run,) = stores.results.list_runs("local")
        stores.results.close()
        return run.id

    def test_rejudge_replaces_only_the_judge_grade_and_the_pass_flag(
        self,
        stored_run: str,
        suite: Path,
        tmp_path: Path,
        quoting_judge: Callable[..., FunctionModel],
    ) -> None:
        stores = open_stores(StorageConfig.load(tmp_path, env={}))
        before = stores.results.list_grades("local", stored_run)
        judge = TrialJudge(CriterionJudge(quoting_judge("fail"), JudgeCache(tmp_path / "jc")), {})
        case = CaseLoader().load(suite / "case.mzeval.yaml")

        [summary] = asyncio.run(
            Rejudger(stores, "local", judge).rejudge(stored_run, {case.name: case})
        )

        after = stores.results.list_grades("local", stored_run)
        (trial,) = stores.results.list_trials("local", stored_run)
        stores.results.close()
        (old,) = before.values()
        (new,) = after.values()
        assert [g.verdict for g in old] == ["pass", "n/a"]
        assert [g.verdict for g in new] == ["pass", "fail"]
        assert new == summary.grades
        assert trial.passed is False

    def test_rejudge_spend_is_added_to_the_ledger(
        self,
        stored_run: str,
        suite: Path,
        tmp_path: Path,
        quoting_judge: Callable[..., FunctionModel],
    ) -> None:
        stores = open_stores(StorageConfig.load(tmp_path, env={}))
        priced = quoting_judge("pass", model_name="claude-haiku-4-5")
        judge = TrialJudge(CriterionJudge(priced, JudgeCache(tmp_path / "jc")), {})
        case = CaseLoader().load(suite / "case.mzeval.yaml")

        [summary] = asyncio.run(
            Rejudger(stores, "local", judge).rejudge(stored_run, {case.name: case})
        )

        ledger = stores.results.list_spend("local", stored_run)
        stores.results.close()
        assert list(ledger.values()) == [summary.judge_spend]
        (spend,) = summary.judge_spend
        assert (spend.cost_usd or 0) > 0

    def test_unknown_run_and_unusable_judge_exit_2(self, stored_run: str, suite: Path) -> None:
        no_run = CliRunner().invoke(app, ["rejudge", "nope", str(suite)])
        bad_judge = CliRunner().invoke(
            app, ["rejudge", stored_run, str(suite), "--judge", "nonsense"]
        )

        assert (no_run.exit_code, bad_judge.exit_code) == (2, 2)
        assert "no stored run 'nope'" in no_run.output
        assert "judge: Unknown model: nonsense" in bad_judge.output
