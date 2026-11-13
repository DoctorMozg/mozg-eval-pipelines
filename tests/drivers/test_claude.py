import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

import pytest

from mozg_eval.drivers import protocol
from mozg_eval.drivers.claude import ClaudeDriver, StreamLine, isolated_env, normalize
from mozg_eval.drivers.protocol import Limits, TrialRequest
from mozg_eval.events.models import (
    End,
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


class Run(Protocol):
    def __call__(
        self, mode: str = "replay", tools: list[ToolKind] | None = None, timeout_s: int = 30
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
        mode: str = "replay", tools: list[ToolKind] | None = None, timeout_s: int = 30
    ) -> list[Event]:
        request = TrialRequest(
            trial="t1",
            prompt="Remember this decision: we use SQLite for storage.",
            workspace=str(workspace),
            env={
                "FAKE_MODE": mode,
                "FAKE_FIXTURE": str(SKILL_FIRE),
                "FAKE_RECORD": str(tmp_path / "record.json"),
            },
            plugins=["/repo/plugins/mz-memory"],
            tools=tools or [],
            model="haiku",
            limits=Limits(max_turns=4, timeout_s=timeout_s),
        )
        driver = ClaudeDriver(tmp_path / "home", tmp_path / "raw", executable=str(fake_claude))
        store = DiskEventStore(tmp_path / "events")
        return asyncio.run(TrialRunner("local", "r1", driver, store, []).run(request, {}))

    return go


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

    def test_no_tools_means_the_default_set(self, run: Run, tmp_path: Path) -> None:
        run()

        assert not any(a.startswith("--tools") for a in recorded(tmp_path)["argv"])

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

    def test_claude_that_ignores_sigterm_is_killed(
        self, run: Run, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(protocol, "CANCEL_GRACE_S", 0.2)
        began = time.monotonic()

        events = run(mode="hang", timeout_s=1)

        assert time.monotonic() - began < 5
        end = events[-1].payload
        assert isinstance(end, End)
        assert end.status == "timeout"
