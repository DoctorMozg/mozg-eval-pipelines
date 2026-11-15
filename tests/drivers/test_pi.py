import asyncio
import json
from pathlib import Path
from typing import Any, Protocol

import pytest

from mozg_eval.drivers.pi import PiDriver, PiLine, PiStream, pi_prompt
from mozg_eval.drivers.protocol import Limits, TrialRequest
from mozg_eval.events.models import (
    End,
    Error,
    Event,
    Message,
    Payload,
    SkillActivated,
    ToolCall,
    ToolResult,
    Usage,
)
from mozg_eval.runner.trial import TrialRunner
from mozg_eval.spec.models import ToolKind
from mozg_eval.storage.disk import DiskEventStore

SKILL_FIRE = Path(__file__).with_name("fixtures") / "pi_skill_fire.jsonl"
RECORDED_PLUGINS = Path("/tmp/mzeval-trial/home/.mzeval-plugins")


def replay(max_turns: int = 10, plugins: Path = RECORDED_PLUGINS) -> list[Payload]:
    stream = PiStream(max_turns, plugins)
    lines = SKILL_FIRE.read_text().splitlines()
    return [p for line in lines for p in stream.normalize(PiLine.model_validate_json(line))]


def line(**fields: object) -> PiLine:
    return PiLine.model_validate(fields)


class TestNormalize:
    def test_recorded_skill_run_maps_to_canonical_events(self) -> None:
        events = replay()
        skill_md = RECORDED_PLUGINS / "probe" / "skills" / "probe-skill" / "SKILL.md"

        assert [type(e) for e in events] == [
            Usage,
            ToolCall,
            SkillActivated,
            ToolResult,
            Usage,
            Message,
            End,
        ]
        assert events[1] == ToolCall(
            id="toolu_01SnSif6frJHQAjcQXQkRoUb",
            kind="read",
            name="read",
            input={"path": str(skill_md)},
        )
        assert events[2] == SkillActivated(skill="probe-skill", evidence="inferred")
        assert events[5] == Message(
            role="assistant", text="Kestrel is deployed in the region `kestrel-east-7`."
        )
        assert events[-1] == End(status="ok")

    def test_usage_counts_cache_tokens_as_input(self) -> None:
        first = replay()[0]

        assert first == Usage(input_tokens=4 + 1546, output_tokens=174, cost_usd=0.00028065)

    def test_read_outside_the_plugin_copies_is_not_a_skill(self, tmp_path: Path) -> None:
        events = replay(plugins=tmp_path)

        assert not any(isinstance(e, SkillActivated) for e in events)

    def test_second_read_of_a_skill_fires_it_once(self) -> None:
        stream = PiStream(10, RECORDED_PLUGINS)
        path = str(RECORDED_PLUGINS / "p" / "skills" / "s" / "SKILL.md")
        read = {"type": "tool_execution_start", "toolName": "read", "args": {"path": path}}

        events = [*stream.normalize(line(toolCallId="a", **read))]
        events += stream.normalize(line(toolCallId="b", **read))

        assert [e for e in events if isinstance(e, SkillActivated)] == [
            SkillActivated(skill="s", evidence="inferred")
        ]

    def test_reading_a_user_skill_fires_it(self, tmp_path: Path) -> None:
        user_skills = tmp_path / ".pi" / "agent" / "skills"
        path = str(user_skills / "changelog" / "SKILL.md")
        read = {"type": "tool_execution_start", "toolName": "read", "args": {"path": path}}

        events = PiStream(10, RECORDED_PLUGINS, user_skills).normalize(line(toolCallId="a", **read))

        assert SkillActivated(skill="changelog", evidence="inferred") in events

    def test_turns_past_max_turns_end_the_trial_as_ok(self) -> None:
        events = replay(max_turns=1)

        assert End(status="ok", reason="max_turns reached") in events
        assert not any(isinstance(e, Message) for e in events)

    def test_failed_model_call_is_an_error(self) -> None:
        message = {"role": "assistant", "stopReason": "error", "errorMessage": "401 no key"}

        events = PiStream(10, RECORDED_PLUGINS).normalize(line(type="message_end", message=message))

        assert events == [Error(message="401 no key")]

    def test_aborted_run_ends_in_error(self) -> None:
        events = PiStream(10, RECORDED_PLUGINS).normalize(line(type="agent_settled", aborted=True))

        assert events == [End(status="error", reason="aborted")]


class TestPrompt:
    def test_slash_command_becomes_a_pi_skill_command(self) -> None:
        assert pi_prompt("/notes:note pin SQLite") == "/skill:note pin SQLite"
        assert pi_prompt("/notes:note") == "/skill:note"

    def test_plain_prompt_is_unchanged(self) -> None:
        assert pi_prompt("What is /etc/hosts for?") == "What is /etc/hosts for?"


class Run(Protocol):
    def __call__(
        self, tools: list[ToolKind] | None = None, harness: Path | None = None, **env: str
    ) -> list[Event]: ...


@pytest.fixture
def run(
    tmp_path: Path, fake_agent: Path, claude_plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> Run:
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/home/someone/.pi/agent")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def go(
        tools: list[ToolKind] | None = None, harness: Path | None = None, **env: str
    ) -> list[Event]:
        request = TrialRequest(
            trial="t1",
            prompt="/notes:note pin SQLite",
            workspace=str(workspace),
            env={
                "FAKE_FIXTURE": str(SKILL_FIRE),
                "FAKE_RECORD": str(tmp_path / "record.json"),
                **env,
            },
            plugins=[str(claude_plugin)],
            tools=["skill"] if tools is None else tools,
            model="openrouter/anthropic/claude-haiku-5.5",
            append_system_prompt="Be brief.",
            harness=str(harness) if harness else None,
            limits=Limits(max_turns=10, timeout_s=30),
        )
        driver = PiDriver(tmp_path / "home", tmp_path / "raw", executable=str(fake_agent))
        store = DiskEventStore(tmp_path / "events")
        return asyncio.run(TrialRunner("local", "r1", driver, store, []).run(request, {}))

    return go


def recorded(tmp_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((tmp_path / "record.json").read_text())
    return result


class TestPiDriver:
    def test_replayed_stream_fires_the_skill_under_the_temp_home(self, run: Run) -> None:
        events = [e.payload for e in run()]

        assert SkillActivated(skill="probe-skill", evidence="inferred") in events
        assert events[-1] == End(status="ok")

    def test_runs_in_a_temp_home_with_only_the_case_skills(self, run: Run, tmp_path: Path) -> None:
        run()
        record = recorded(tmp_path)
        skills = tmp_path / "home" / ".mzeval-plugins" / "notes" / "skills"

        assert record["argv"] == [
            "--mode",
            "json",
            "--no-session",
            "--no-skills",
            "-ne",
            "-na",
            "--skill",
            str(skills),
            "--provider",
            "openrouter",
            "--model",
            "anthropic/claude-haiku-5.5",
            "--tools",
            "read",
            "--append-system-prompt",
            "Be brief.",
            "/skill:note pin SQLite",
        ]
        assert record["env"]["HOME"] == str(tmp_path / "home")
        assert "PI_CODING_AGENT_DIR" not in record["env"]
        assert record["stdin"] == ""

    def test_harness_flags_follow_the_isolation_flags(self, run: Run, tmp_path: Path) -> None:
        root = tmp_path / "harness"
        (root / "skills" / "changelog").mkdir(parents=True)
        (root / "harness.mzeval.yaml").write_text("schema: mzeval-harness/1\nname: demo\n")
        (root / "skills" / "changelog" / "SKILL.md").write_text("---\nname: changelog\n---\n")

        run(harness=root)

        argv = recorded(tmp_path)["argv"]
        user_skills = tmp_path / "home" / ".pi" / "agent" / "skills"
        assert argv[5:9] == ["-na", "--skill", str(user_skills), "--skill"]
        assert (user_skills / "changelog" / "SKILL.md").is_file()

    def test_case_tools_map_to_pi_tool_names(self, run: Run, tmp_path: Path) -> None:
        run(tools=["skill", "read", "glob", "edit"])
        argv = recorded(tmp_path)["argv"]

        assert argv[argv.index("--tools") + 1] == "read,find,edit"

    def test_empty_tools_turn_every_tool_off(self, run: Run, tmp_path: Path) -> None:
        run(tools=[])
        argv = recorded(tmp_path)["argv"]

        assert "--no-tools" in argv
        assert "--tools" not in argv

    def test_exit_before_settling_is_an_error_end(self, run: Run, tmp_path: Path) -> None:
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")

        events = [e.payload for e in run(FAKE_FIXTURE=str(empty), FAKE_EXIT="1")]

        assert events[-1] == End(status="error", reason="pi exited with code 1")

    def test_plugin_without_a_manifest_ends_before_pi_starts(
        self, run: Run, tmp_path: Path, claude_plugin: Path
    ) -> None:
        (claude_plugin / ".claude-plugin" / "plugin.json").unlink()

        events = [e.payload for e in run()]

        assert events[-1] == End(status="error", reason="plugin copy failed")
        assert not (tmp_path / "record.json").exists()
