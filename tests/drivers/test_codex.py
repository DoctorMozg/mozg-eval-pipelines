import asyncio
import json
import time
from pathlib import Path
from typing import Any, Protocol

import pytest

from mozg_eval.drivers import process
from mozg_eval.drivers.codex import CodexDriver, CodexLine, CodexStream, HookLog, codex_prompt
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
    ToolCall,
    ToolResult,
    Usage,
)
from mozg_eval.runner.trial import TrialRunner
from mozg_eval.storage.disk import DiskEventStore

SKILL_FIRE = Path(__file__).with_name("fixtures") / "codex_skill_fire.jsonl"
SKILL_MD = (
    "/tmp/mzeval-trial/home/.codex/plugins/cache/mzeval/probe/0.1.0/skills/probe-skill/SKILL.md"
)


def replay(path: Path, max_turns: int = 10) -> list[Payload]:
    stream = CodexStream(max_turns)
    lines = path.read_text().splitlines()
    return [p for line in lines for p in stream.normalize(CodexLine.model_validate_json(line))]


def completed(stream: CodexStream, **item: object) -> list[Payload]:
    return stream.normalize(CodexLine.model_validate({"type": "item.completed", "item": item}))


class TestNormalize:
    def test_recorded_skill_run_maps_to_canonical_events(self) -> None:
        events = replay(SKILL_FIRE)

        assert [type(e) for e in events] == [
            Message,
            ToolCall,
            SkillActivated,
            ToolResult,
            Message,
            Usage,
            End,
        ]
        assert events[1] == ToolCall(
            id="item_3",
            kind="shell",
            name="shell",
            input={"command": f"/usr/bin/zsh -lc 'cat {SKILL_MD}'"},
        )
        assert events[2] == SkillActivated(skill="probe-skill", evidence="inferred")
        assert isinstance(events[3], ToolResult)
        assert events[3].ok
        assert events[5] == Usage(input_tokens=30282, output_tokens=145, cost_usd=None)
        assert events[6] == End(status="ok")

    def test_messages_past_max_turns_end_the_trial_as_ok(self) -> None:
        events = replay(SKILL_FIRE, max_turns=1)
        # The driver stops reading at the first end.
        events = events[: next(i for i, e in enumerate(events) if isinstance(e, End)) + 1]

        assert events[-1] == End(status="ok", reason="max_turns reached")
        assert sum(isinstance(e, Message) for e in events) == 1
        assert SkillActivated(skill="probe-skill", evidence="inferred") in events

    def test_tool_calls_past_max_turns_end_the_trial_as_ok(self) -> None:
        stream = CodexStream(1)

        def start(item_id: str) -> list[Payload]:
            item = {"id": item_id, "type": "command_execution", "command": "ls"}
            return stream.normalize(
                CodexLine.model_validate({"type": "item.started", "item": item})
            )

        assert isinstance(start("a")[0], ToolCall)
        assert start("b") == [End(status="ok", reason="max_turns reached")]

    def test_item_seen_only_when_completed_still_gets_its_call(self) -> None:
        events = completed(
            CodexStream(10), id="i1", type="command_execution", command="false", exit_code=1
        )

        assert events == [
            ToolCall(id="i1", kind="shell", name="shell", input={"command": "false"}),
            ToolResult(id="i1", ok=False, output=""),
        ]

    def test_second_read_of_a_skill_is_not_a_second_activation(self) -> None:
        stream = CodexStream(10)
        events = completed(
            stream, id="a", type="command_execution", command=f"cat {SKILL_MD}", exit_code=0
        )
        events += completed(
            stream, id="b", type="command_execution", command=f"sed -n 1,9p {SKILL_MD}", exit_code=0
        )

        assert sum(isinstance(e, SkillActivated) for e in events) == 1

    def test_reading_a_user_skill_activates_it(self) -> None:
        user_skill = "/tmp/mzeval-trial/home/.agents/skills/changelog/SKILL.md"
        events = completed(
            CodexStream(10), id="a", type="command_execution", command=f"cat {user_skill}"
        )

        assert SkillActivated(skill="changelog", evidence="inferred") in events

    def test_hook_trust_notice_is_dropped_and_other_item_errors_are_warnings(self) -> None:
        stream = CodexStream(10)
        notice = "`--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run"

        assert completed(stream, id="e", type="error", message=notice) == []
        assert completed(stream, id="f", type="error", message="model fell back") == [
            Log(level="warning", text="model fell back")
        ]

    def test_failed_turn_is_an_error_end(self) -> None:
        line = CodexLine.model_validate({"type": "turn.failed", "error": {"message": "quota"}})

        assert CodexStream(10).normalize(line) == [
            Error(message="quota"),
            End(status="error", reason="quota"),
        ]

    def test_spawn_agent_is_a_subagent_tool_call_without_a_name(self) -> None:
        events = completed(
            CodexStream(10),
            id="c",
            type="collab_tool_call",
            tool="spawn_agent",
            prompt="go",
            status="completed",
        )

        assert events[0] == ToolCall(
            id="c", kind="subagent", name="spawn_agent", input={"prompt": "go"}
        )


class TestPrompt:
    def test_slash_command_names_the_skill(self) -> None:
        assert codex_prompt("/mz-memory:memory-note pin SQLite") == (
            "Use the $memory-note skill from mz-memory.\n"
            "Use the arguments below wherever the skill refers to arguments or the user's "
            "request.\n<skill_arguments>\npin SQLite\n</skill_arguments>"
        )

    def test_plain_prompt_is_unchanged(self) -> None:
        assert codex_prompt("What is /etc/hosts for?") == "What is /etc/hosts for?"


class TestHookLog:
    def test_reads_only_complete_lines_and_each_once(self, tmp_path: Path) -> None:
        path = tmp_path / "hooks.jsonl"
        hooks = HookLog(path)
        assert hooks.new_events() == []
        path.write_text('{"event": "SessionStart", "stdout": "ctx"}\n{"event": "Stop"')

        assert hooks.new_events() == [HookFired(event="SessionStart", output="ctx")]
        with path.open("a") as log:
            log.write(', "stdout": ""}\n')
        assert hooks.new_events() == [HookFired(event="Stop", output="")]
        assert hooks.new_events() == []


class Run(Protocol):
    def __call__(
        self, mode: str = "replay", timeout_s: int = 30, harness: Path | None = None, **env: str
    ) -> list[Event]: ...


@pytest.fixture
def real_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    real = tmp_path / "real-codex"
    real.mkdir()
    (real / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(real))
    monkeypatch.setenv("CLAUDE_EFFORT", "max")
    return real


@pytest.fixture
def run(tmp_path: Path, real_codex: Path, fake_codex: Path, claude_plugin: Path) -> Run:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def go(
        mode: str = "replay", timeout_s: int = 30, harness: Path | None = None, **env: str
    ) -> list[Event]:
        request = TrialRequest(
            trial="t1",
            prompt="/notes:note pin SQLite",
            workspace=str(workspace),
            env={
                "FAKE_MODE": mode,
                "FAKE_FIXTURE": str(SKILL_FIRE),
                "FAKE_RECORD": str(tmp_path / "record.json"),
                **env,
            },
            plugins=[str(claude_plugin)],
            tools=["skill"],
            model="gpt-6.1-sol",
            harness=str(harness) if harness else None,
            limits=Limits(max_turns=4, timeout_s=timeout_s),
        )
        driver = CodexDriver(tmp_path / "home", tmp_path / "raw", executable=str(fake_codex))
        store = DiskEventStore(tmp_path / "events")
        return asyncio.run(TrialRunner("local", "r1", driver, store, []).run(request, {}))

    return go


def recorded(tmp_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((tmp_path / "record.json").read_text())
    return result


class TestCodexDriver:
    def test_replayed_stream_lands_in_the_store(self, run: Run, tmp_path: Path) -> None:
        events = [e.payload for e in run()]

        assert events[1] == Log(
            level="info", text="codex offers its own tools; [skill] is not enforced"
        )
        assert events[2:] == replay(SKILL_FIRE)
        assert (tmp_path / "raw" / "raw.jsonl").read_bytes() == SKILL_FIRE.read_bytes()

    def test_runs_in_a_temp_codex_home_with_linked_auth(
        self, run: Run, tmp_path: Path, real_codex: Path
    ) -> None:
        run()
        record = recorded(tmp_path)
        codex_home = tmp_path / "home" / ".codex"

        assert record["env"]["HOME"] == str(tmp_path / "home")
        assert record["env"]["CODEX_HOME"] == str(codex_home)
        assert "CLAUDE_EFFORT" not in record["env"]
        assert (codex_home / "auth.json").resolve() == real_codex / "auth.json"
        assert "[skills.bundled]\nenabled = false" in (codex_home / "config.toml").read_text()
        assert record["stdin"].startswith("Use the $note skill from notes.")
        assert "--model=gpt-6.1-sol" in record["argv"]
        assert record["argv"][-1] == "-"

    def test_harness_is_written_into_the_trial_home(self, run: Run, tmp_path: Path) -> None:
        root = tmp_path / "harness"
        (root / "instructions").mkdir(parents=True)
        (root / "harness.mzeval.yaml").write_text("schema: mzeval-harness/1\nname: demo\n")
        (root / "instructions" / "style.md").write_text("Comments say why.\n")

        run(harness=root)

        codex_home = tmp_path / "home" / ".codex"
        assert (codex_home / "AGENTS.md").read_text() == "Comments say why.\n"
        assert "[skills.bundled]\nenabled = false" in (codex_home / "config.toml").read_text()

    def test_plugins_are_installed_before_the_run(self, run: Run, tmp_path: Path) -> None:
        run()
        calls = [
            json.loads(line)
            for line in Path(f"{tmp_path}/record.json.plugins").read_text().splitlines()
        ]

        assert calls == [
            ["plugin", "marketplace", "add", str(tmp_path / "home" / ".mzeval-market")],
            ["plugin", "add", "notes@mzeval"],
        ]

    def test_failed_install_ends_the_trial_without_running_codex(
        self, run: Run, tmp_path: Path
    ) -> None:
        events = [e.payload for e in run(FAKE_PLUGIN_EXIT="1")]

        assert events[-1] == End(status="error", reason="plugin install failed")
        assert not (tmp_path / "record.json").exists()

    def test_logged_hooks_become_hook_events(self, run: Run) -> None:
        events = [e.payload for e in run(FAKE_HOOK="SessionStart")]

        assert HookFired(event="SessionStart", output="ctx") in events

    def test_hook_that_fires_after_the_turn_is_still_recorded(self, run: Run) -> None:
        events = [e.payload for e in run(mode="late_hook")]

        assert events[-2] == HookFired(event="SessionEnd", output="ctx")
        assert events[-1] == End(status="ok")

    def test_codex_left_running_after_its_end_is_stopped(
        self, run: Run, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(process, "CANCEL_GRACE_S", 0.2)
        began = time.monotonic()

        events = run(mode="linger")

        assert time.monotonic() - began < 5
        assert events[-1].payload == End(status="ok")

    def test_codex_that_ignores_sigterm_is_killed(
        self, run: Run, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(process, "CANCEL_GRACE_S", 0.2)
        began = time.monotonic()

        events = run(mode="hang", timeout_s=1)

        assert time.monotonic() - began < 5
        end = events[-1].payload
        assert isinstance(end, End)
        assert end.status == "timeout"
