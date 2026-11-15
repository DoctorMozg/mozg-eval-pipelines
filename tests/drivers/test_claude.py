import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

import pytest

from mozg_eval.drivers import process
from mozg_eval.drivers.claude import ClaudeDriver, StreamLine, isolated_env, normalize
from mozg_eval.drivers.protocol import Limits, TrialRequest
from mozg_eval.events.models import (
    End,
    Error,
    Event,
    HookFired,
    Log,
    Message,
    Payload,
    SkillActivated,
    SubagentSpawned,
    ToolCall,
    ToolResult,
    Usage,
)
from mozg_eval.runner.trial import TrialRunner
from mozg_eval.spec.models import ToolKind
from mozg_eval.storage.disk import DiskEventStore

SKILL_FIRE = Path(__file__).with_name("fixtures") / "claude_skill_fire.jsonl"
# A real haiku run whose Agent call went to the background, so the stream ends in two results.
BACKGROUND_AGENT = SKILL_FIRE.with_name("claude_background_agent.jsonl")


def replay(path: Path) -> list[Payload]:
    lines = path.read_text().splitlines()
    return [p for line in lines for p in normalize(StreamLine.model_validate_json(line))]


def line(**fields: object) -> list[Payload]:
    return normalize(StreamLine.model_validate(fields))


class TestNormalize:
    def test_recorded_skill_run_maps_to_canonical_events(self) -> None:
        events = replay(SKILL_FIRE)

        assert [type(e) for e in events] == [
            HookFired,
            Log,
            ToolCall,
            SkillActivated,
            ToolResult,
            ToolCall,
            ToolResult,
            Message,
            Usage,
            End,
        ]
        assert events[1] == Log(
            level="info", text="claude 2.1.325, model claude-haiku-5-5, tools [Skill]"
        )
        assert events[3] == SkillActivated(skill="memory-note")
        tool = events[5]
        assert isinstance(tool, ToolCall)
        assert (tool.kind, tool.name) == ("ask_user", "AskUserQuestion")
        denied = events[6]
        assert isinstance(denied, ToolResult)
        assert (denied.id, denied.ok) == (tool.id, False)
        assert "No such tool available: AskUserQuestion" in denied.output
        assert events[8] == Usage(input_tokens=43944, output_tokens=674, cost_usd=0.00396366)
        assert events[9] == End(status="ok")

    def test_subagent_call_is_spawned_with_its_bare_name(self) -> None:
        block = {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "Agent",
            "input": {"subagent_type": "mz-base:pipeline-researcher", "prompt": "look"},
        }
        events = line(type="assistant", message={"content": [block]})

        assert events[1] == SubagentSpawned(subagent="pipeline-researcher")

    def test_shell_result_with_block_content(self) -> None:
        call = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
        result = {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [{"type": "text", "text": "README.md"}],
        }

        assert line(type="assistant", message={"content": [call]}) == [
            ToolCall(id="t1", kind="shell", name="Bash", input={"command": "ls"})
        ]
        assert line(type="user", message={"content": [result]}) == [
            ToolResult(id="t1", ok=True, output="README.md")
        ]

    def test_reaching_max_turns_is_a_normal_stop(self) -> None:
        # Routing cases cap turns on purpose: the skill call is all they grade.
        events = line(type="result", subtype="error_max_turns", is_error=True, total_cost_usd=0.02)

        assert events[-1] == End(status="ok", reason="max_turns reached")

    def test_failed_run_ends_as_error_with_the_subtype(self) -> None:
        events = line(type="result", subtype="error_during_execution", is_error=True)

        assert events[-1] == End(status="error", reason="error_during_execution")

    @pytest.mark.parametrize(
        "fields",
        [
            {"type": "rate_limit_event"},
            {"type": "system", "subtype": "thinking_tokens"},
            {"type": "user", "message": {"content": "the skill body as plain text"}},
            {"type": "some_future_event", "subtype": "x"},
        ],
    )
    def test_lines_with_nothing_to_grade_map_to_no_events(self, fields: dict[str, Any]) -> None:
        assert line(**fields) == []


class TestIsolatedEnv:
    def test_strips_session_state_and_real_plugin_bins(self, tmp_path: Path) -> None:
        real = tmp_path / "real" / ".claude"
        base = {
            "PATH": os.pathsep.join([f"{real}/plugins/cache/x/bin", "/usr/bin"]),
            "CLAUDE_EFFORT": "max",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_OAUTH_TOKEN": "kept",
            "LANG": "C.UTF-8",
        }

        env = isolated_env(base, tmp_path / "home", real)

        assert env["PATH"] == "/usr/bin"
        assert "CLAUDE_EFFORT" not in env
        assert "CLAUDECODE" not in env
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "kept"
        assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "home" / ".claude")
        assert env["LANG"] == "C.UTF-8"

    def test_turns_off_the_accounts_claude_ai_connectors(self, tmp_path: Path) -> None:
        # The linked login would otherwise load the user's own claude.ai MCP connectors.
        base = {"ENABLE_CLAUDEAI_MCP_SERVERS": "true"}

        env = isolated_env(base, tmp_path / "home", tmp_path / "real")

        assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"


class Run(Protocol):
    def __call__(
        self,
        mode: str = "replay",
        tools: list[ToolKind] | None = None,
        timeout_s: int = 30,
        fixture: Path = SKILL_FIRE,
        harness: Path | None = None,
    ) -> list[Event]: ...


@pytest.fixture
def real_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    real = tmp_path / "real-claude"
    real.mkdir()
    (real / ".credentials.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(real))
    monkeypatch.setenv("CLAUDE_EFFORT", "max")
    return real


@pytest.fixture
def run(tmp_path: Path, real_config: Path, fake_claude: Path) -> Run:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def go(
        mode: str = "replay",
        tools: list[ToolKind] | None = None,
        timeout_s: int = 30,
        fixture: Path = SKILL_FIRE,
        harness: Path | None = None,
    ) -> list[Event]:
        request = TrialRequest(
            trial="t1",
            prompt="Remember this decision: we use SQLite for storage.",
            workspace=str(workspace),
            env={
                "FAKE_MODE": mode,
                "FAKE_FIXTURE": str(fixture),
                "FAKE_RECORD": str(tmp_path / "record.json"),
            },
            plugins=["/repo/plugins/mz-memory"],
            tools=tools,
            model="haiku",
            harness=str(harness) if harness else None,
            limits=Limits(max_turns=4, timeout_s=timeout_s),
        )
        driver = ClaudeDriver(tmp_path / "home", tmp_path / "raw", executable=str(fake_claude))
        store = DiskEventStore(tmp_path / "events")
        return asyncio.run(TrialRunner("local", "r1", driver, store, []).run(request, {}))

    return go


def demo_harness(tmp_path: Path) -> Path:
    root = tmp_path / "harness"
    (root / "instructions").mkdir(parents=True)
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "harness.mzeval.yaml").write_text("schema: mzeval-harness/1\nname: demo\n")
    (root / "instructions" / "style.md").write_text("Comments say why.\n")
    (root / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\n")
    return root


def with_init_skills(tmp_path: Path, skills: list[str]) -> Path:
    """SKILL_FIRE with its init line listing `skills` as well."""
    lines = []
    for raw in SKILL_FIRE.read_text().splitlines():
        line = json.loads(raw)
        if line.get("subtype") == "init":
            line["skills"] = [*line["skills"], *skills]
        lines.append(json.dumps(line))
    stream = tmp_path / "init_skills.jsonl"
    stream.write_text("\n".join(lines) + "\n")
    return stream


def recorded(tmp_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((tmp_path / "record.json").read_text())
    return result


class TestClaudeDriver:
    def test_replayed_stream_lands_in_the_store(self, run: Run, tmp_path: Path) -> None:
        events = run(tools=["skill"])

        assert [e.payload for e in events[1:]] == replay(SKILL_FIRE)
        assert (tmp_path / "raw" / "raw.jsonl").read_bytes() == SKILL_FIRE.read_bytes()

    def test_skill_only_case_offers_only_the_skill_tool(self, run: Run, tmp_path: Path) -> None:
        run(tools=["skill"])
        argv = recorded(tmp_path)["argv"]

        assert [a for a in argv if a.startswith("--tools")] == ["--tools=Skill"]
        assert "--plugin-dir=/repo/plugins/mz-memory" in argv
        assert "--model=haiku" in argv

    def test_background_agent_bills_the_session_total_once(self, run: Run) -> None:
        events = [e.payload for e in run(fixture=BACKGROUND_AGENT)]

        costs = [p.cost_usd for p in events if isinstance(p, Usage) and p.cost_usd is not None]
        assert sum(costs) == pytest.approx(0.032088295)
        assert events[-1] == End(status="ok")
        replies = [p.text for p in events if isinstance(p, Message)]
        assert replies[-1].startswith("The subagent returned three distinct facts")

    def test_max_turns_stop_with_exit_code_1_reports_no_error(
        self, run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # claude -p exits 1 whenever its result line is an error, max_turns included.
        lines = SKILL_FIRE.read_text().splitlines()
        result = json.loads(lines[-1]) | {"subtype": "error_max_turns", "is_error": True}
        stream = tmp_path / "max_turns.jsonl"
        stream.write_text("\n".join([*lines[:-1], json.dumps(result)]) + "\n")
        monkeypatch.setenv("FAKE_EXIT", "1")

        events = [e.payload for e in run(fixture=stream)]

        assert not any(isinstance(p, Error) for p in events)
        assert events[-1] == End(status="ok", reason="max_turns reached")

    def test_crash_before_a_result_reports_the_exit_code(
        self, run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = tmp_path / "crash.jsonl"
        stream.write_text("\n".join(SKILL_FIRE.read_text().splitlines()[:-1]) + "\n")
        monkeypatch.setenv("FAKE_EXIT", "1")

        events = [e.payload for e in run(fixture=stream)]

        assert Error(message="claude exited with code 1; see stderr.log") in events
        assert isinstance(events[-1], End)
        assert events[-1].status == "error"

    def test_harness_is_written_into_the_trial_home(self, run: Run, tmp_path: Path) -> None:
        harness = demo_harness(tmp_path)
        stream = with_init_skills(tmp_path, ["demo"])

        events = [e.payload for e in run(fixture=stream, harness=harness)]

        rules = tmp_path / "home" / ".claude" / "rules" / "style.md"
        assert rules.read_text() == "Comments say why.\n"
        assert not any(isinstance(p, Error) for p in events)
        assert events[-1] == End(status="ok")

    def test_harness_skill_missing_from_init_stops_the_trial(
        self, run: Run, tmp_path: Path
    ) -> None:
        events = [e.payload for e in run(harness=demo_harness(tmp_path))]

        assert Error(message="harness demo didn't load: skill demo") in events
        assert events[-1] == End(status="error", reason="harness didn't load")
        assert not any(isinstance(p, SkillActivated) for p in events)

    def test_omitted_tools_mean_the_default_set(self, run: Run, tmp_path: Path) -> None:
        run()

        assert not any(a.startswith("--tools") for a in recorded(tmp_path)["argv"])

    def test_empty_tools_turn_every_tool_off(self, run: Run, tmp_path: Path) -> None:
        run(tools=[])

        assert [a for a in recorded(tmp_path)["argv"] if a.startswith("--tools")] == ["--tools="]

    def test_runs_in_a_temp_home_with_linked_credentials(
        self, run: Run, tmp_path: Path, real_config: Path
    ) -> None:
        run()
        record = recorded(tmp_path)
        linked = tmp_path / "home" / ".claude" / ".credentials.json"

        assert record["env"]["HOME"] == str(tmp_path / "home")
        assert record["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "home" / ".claude")
        assert "CLAUDE_EFFORT" not in record["env"]
        assert linked.is_symlink()
        assert linked.resolve() == real_config / ".credentials.json"
        assert record["stdin"] == "Remember this decision: we use SQLite for storage."

    def test_ci_runner_with_no_login_on_disk_authenticates_from_env(
        self, run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-such-dir"))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test")

        events = run()

        env = recorded(tmp_path)["env"]
        assert isinstance(events[-1].payload, End)
        assert events[-1].payload.status == "ok"
        assert (env["ANTHROPIC_API_KEY"], env["CLAUDE_CODE_OAUTH_TOKEN"]) == (
            "sk-test",
            "oauth-test",
        )
        assert not (tmp_path / "home" / ".claude" / ".credentials.json").exists()

    def test_claude_that_ignores_sigterm_is_killed(
        self, run: Run, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(process, "CANCEL_GRACE_S", 0.2)
        began = time.monotonic()

        events = run(mode="hang", timeout_s=1)

        assert time.monotonic() - began < 5
        end = events[-1].payload
        assert isinstance(end, End)
        assert end.status == "timeout"
