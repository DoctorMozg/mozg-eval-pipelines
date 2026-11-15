import asyncio
import sys
import time
from pathlib import Path
from typing import Protocol

import pytest

from mozg_eval.drivers import process
from mozg_eval.drivers.protocol import DriverManifest, Limits, StdioDriver, TrialRequest
from mozg_eval.events.models import (
    End,
    Error,
    Event,
    Start,
    ToolCall,
    ToolResult,
    Usage,
)
from mozg_eval.runner.settings import resolve_settings
from mozg_eval.runner.trial import (
    CostGuard,
    ForbiddenToolGuard,
    Guard,
    SpendCap,
    TrialRunner,
)
from mozg_eval.spec.models import Execution
from mozg_eval.storage.disk import DiskEventStore

FAKE = Path(__file__).with_name("fake_driver.py")


class Trial(Protocol):
    def __call__(self, mode: str, *guards: Guard, timeout_s: int = 30) -> list[Event]: ...


@pytest.fixture
def trial(tmp_path: Path) -> Trial:
    def run(mode: str, *guards: Guard, timeout_s: int = 30) -> list[Event]:
        manifest = DriverManifest(
            name="fake",
            command=[sys.executable, str(FAKE), mode],
            version_cmd=[sys.executable, str(FAKE), "--version"],
        )
        request = TrialRequest(
            trial="t1",
            prompt="hello",
            workspace=str(tmp_path),
            limits=Limits(max_turns=10, timeout_s=timeout_s),
        )
        store = DiskEventStore(tmp_path / "events")
        runner = TrialRunner("local", "r1", StdioDriver(manifest, tmp_path / "raw"), store, guards)
        return asyncio.run(runner.run(request, {}))

    return run


def end_of(events: list[Event]) -> End:
    end = events[-1].payload
    assert isinstance(end, End)
    return end


def stored(tmp_path: Path) -> list[Event]:
    async def read() -> list[Event]:
        return [e async for e in DiskEventStore(tmp_path / "events").read("local", "r1", "t1")]

    return asyncio.run(read())


def count(events: list[Event], kind: type) -> int:
    return sum(isinstance(e.payload, kind) for e in events)


class TestTrialRunner:
    def test_streams_every_event_into_the_log(self, trial: Trial, tmp_path: Path) -> None:
        events = trial("ok")

        assert stored(tmp_path) == events
        assert isinstance(start := events[0].payload, Start)
        assert start.driver_version == "fake 1.0"
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert count(events, Usage) == 5
        assert end_of(events).status == "ok"
        assert (tmp_path / "raw" / "raw.jsonl").read_text().count("\n") == 17

    def test_cost_guard_cancels_mid_run(self, trial: Trial) -> None:
        events = trial("ok", CostGuard(SpendCap(0.25)))

        assert end_of(events).status == "cancelled"
        assert "over the $0.25 cap" in end_of(events).reason
        assert count(events, Usage) == 3

    def test_replayed_log_trips_the_guard_at_the_same_event(
        self, trial: Trial, tmp_path: Path
    ) -> None:
        live = trial("ok", CostGuard(SpendCap(0.25)))
        tripped_live = max(e.seq for e in live if isinstance(e.payload, Usage))

        guard = CostGuard(SpendCap(0.25))
        replayed = stored(tmp_path)
        tripped = next(e.seq for e in replayed if guard.check(e.payload))

        assert replayed == live
        assert tripped == tripped_live

    def test_timeout_stops_a_slow_driver(self, trial: Trial) -> None:
        end = end_of(trial("slow", timeout_s=1))

        assert end.status == "timeout"
        assert end.reason == "no end after timeout_s 1"

    def test_driver_that_ignores_cancel_is_killed(
        self, trial: Trial, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(process, "CANCEL_GRACE_S", 0.2)
        began = time.monotonic()

        events = trial("stubborn", CostGuard(SpendCap(0.25)))

        assert time.monotonic() - began < 5
        assert end_of(events).status == "cancelled"

    def test_line_longer_than_the_default_stream_limit_is_read(self, trial: Trial) -> None:
        events = trial("long")

        assert count(events, ToolResult) == 1
        assert end_of(events).status == "ok"

    def test_invalid_event_is_logged_and_stops_the_driver(self, trial: Trial) -> None:
        events = trial("garbage")

        errors = [e.payload for e in events if isinstance(e.payload, Error)]
        assert len(errors) == 1
        assert "sent an invalid event" in errors[0].message
        assert end_of(events).status == "cancelled"


class TestGuards:
    def test_forbidden_tool_guard_names_the_tool(self) -> None:
        guard = ForbiddenToolGuard({"shell"})

        assert guard.check(ToolCall(id="1", kind="read", name="Read")) is None
        assert (
            guard.check(ToolCall(id="2", kind="shell", name="Bash"))
            == "forbidden tool Bash (shell)"
        )


class TestSettings:
    def test_each_setting_records_its_layer(self) -> None:
        execution = Execution(prompt="hi", max_turns=4)

        settings = resolve_settings(execution, {"runs": 1})

        assert (settings["max_turns"].value, settings["max_turns"].source) == (4, "case")
        assert (settings["timeout_s"].value, settings["timeout_s"].source) == (300, "default")
        assert (settings["runs"].value, settings["runs"].source) == (1, "cli")

    def test_unknown_cli_setting_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="turbo"):
            resolve_settings(Execution(prompt="hi"), {"turbo": True})


def test_manifest_loads_from_driver_yaml(tmp_path: Path) -> None:
    path = tmp_path / "driver.yaml"
    path.write_text("name: fake\ncommand: [fake]\nversion_cmd: [fake, --version]\n")

    manifest = DriverManifest.load(path)

    assert manifest.capabilities.skills == "none"
    assert manifest.command == ["fake"]
