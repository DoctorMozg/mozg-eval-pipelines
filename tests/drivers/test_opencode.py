import asyncio
import json
from pathlib import Path
from typing import Any, Protocol

import pytest

from mozg_eval.drivers.opencode import OpenCodeDriver, OpenCodeLine, OpenCodeStream, opencode_prompt
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

SKILL_FIRE = Path(__file__).with_name("fixtures") / "opencode_skill_fire.jsonl"


def replay(max_turns: int = 10) -> list[Payload]:
    stream = OpenCodeStream(max_turns)
    lines = SKILL_FIRE.read_text().splitlines()
    return [p for line in lines for p in stream.normalize(OpenCodeLine.model_validate_json(line))]


class TestNormalize:
    def test_recorded_skill_run_maps_to_canonical_events(self) -> None:
        events = replay()

        assert [type(e) for e in events] == [
            ToolCall,
            SkillActivated,
            ToolResult,
            Usage,
            Message,
            Usage,
        ]
        assert events[0] == ToolCall(
            id="toolu_01Lo1z4sEA96PSikEdqKsB71",
            kind="skill",
            name="skill",
            input={"id": "probe-skill"},
        )
        assert events[1] == SkillActivated(skill="probe-skill", evidence="native")
        assert events[4] == Message(role="assistant", text="kestrel-east-7")
        assert isinstance(events[3], Usage)
        assert events[3].cost_usd == pytest.approx(0.000956225)

    def test_turns_past_max_turns_end_the_trial_as_ok(self) -> None:
        events = replay(max_turns=1)

        assert End(status="ok", reason="max_turns reached") in events
        assert not any(isinstance(e, Message) for e in events)

    def test_error_line_is_an_error_event(self) -> None:
        line = OpenCodeLine.model_validate(
            {"type": "error", "error": {"name": "ProviderAuthError"}}
        )

        assert OpenCodeStream(10).normalize(line) == [
            Error(message='{"name": "ProviderAuthError"}')
        ]


class TestPrompt:
    def test_slash_command_mentions_the_skill(self) -> None:
        assert opencode_prompt("/notes:note pin SQLite") == "@note pin SQLite"
        assert opencode_prompt("/notes:note") == "@note"

    def test_plain_prompt_is_unchanged(self) -> None:
        assert opencode_prompt("What is /etc/hosts for?") == "What is /etc/hosts for?"


class Run(Protocol):
    def __call__(self, tools: list[ToolKind] | None = None, **env: str) -> list[Event]: ...


@pytest.fixture
def run(
    tmp_path: Path, fake_agent: Path, claude_plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> Run:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/someone/.config")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/home/someone/oc")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def go(tools: list[ToolKind] | None = None, **env: str) -> list[Event]:
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
            limits=Limits(max_turns=10, timeout_s=30),
        )
        driver = OpenCodeDriver(tmp_path / "home", tmp_path / "raw", executable=str(fake_agent))
        store = DiskEventStore(tmp_path / "events")
        return asyncio.run(TrialRunner("local", "r1", driver, store, []).run(request, {}))

    return go


def recorded(tmp_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((tmp_path / "record.json").read_text())
    return result


def config(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "home" / ".config" / "opencode" / "opencode.json"
    result: dict[str, Any] = json.loads(path.read_text())
    return result


class TestOpenCodeDriver:
    def test_replayed_stream_lands_in_the_store_and_ends_ok(self, run: Run) -> None:
        events = [e.payload for e in run()]

        assert events[1:-1] == replay()
        assert events[-1] == End(status="ok")

    def test_runs_standalone_in_a_temp_home_without_the_users_xdg_dirs(
        self, run: Run, tmp_path: Path
    ) -> None:
        run()
        record = recorded(tmp_path)

        assert record["argv"] == [
            "run",
            "--standalone",
            "--format",
            "json",
            "--auto",
            "--archived",
            "--model",
            "openrouter/anthropic/claude-haiku-5.5",
            "@note pin SQLite",
        ]
        assert record["env"]["HOME"] == str(tmp_path / "home")
        assert "XDG_CONFIG_HOME" not in record["env"]
        assert "OPENCODE_CONFIG_DIR" not in record["env"]

    def test_plugin_skills_are_copied_with_their_root_resolved(
        self, run: Run, tmp_path: Path
    ) -> None:
        run()
        [skills] = config(tmp_path)["skills"]
        copy = Path(skills).parent

        assert copy == tmp_path / "home" / ".mzeval-plugins" / "notes"
        assert f"Run {copy}/scripts/save.sh" in (copy / "skills" / "note" / "SKILL.md").read_text()

    def test_case_tools_are_the_only_ones_allowed(self, run: Run, tmp_path: Path) -> None:
        run(tools=["skill", "write", "edit"])

        assert config(tmp_path)["permissions"] == [
            {"action": "*", "resource": "*", "effect": "deny"},
            {"action": "edit", "resource": "*", "effect": "allow"},
            {"action": "skill", "resource": "*", "effect": "allow"},
        ]

    def test_failed_exit_is_an_error_end(self, run: Run) -> None:
        events = [e.payload for e in run(FAKE_EXIT="1")]

        assert events[-2] == Error(message="opencode exited with code 1; see stderr.log")
        assert events[-1] == End(status="error", reason="opencode exited with code 1")

    def test_plugin_without_a_manifest_ends_before_opencode_starts(
        self, run: Run, tmp_path: Path, claude_plugin: Path
    ) -> None:
        (claude_plugin / ".claude-plugin" / "plugin.json").unlink()

        events = [e.payload for e in run()]

        assert events[-1] == End(status="error", reason="plugin copy failed")
        assert not (tmp_path / "record.json").exists()
