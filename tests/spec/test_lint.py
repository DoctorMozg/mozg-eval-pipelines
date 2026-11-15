import json
from collections.abc import Callable
from pathlib import Path

import pytest

from mozg_eval.spec.lint import CaseLinter
from mozg_eval.spec.loader import CASE_FILENAME, CaseLoader

type WriteCase = Callable[[list[str], str], Path]

CASE = """schema: mzeval/2
name: hooks
kind: behaviour
bundle: [plugin]
execution: {{prompt: p}}
graders:
  - name: nudge-shown
    type: hook_fired
{extra}"""


@pytest.fixture
def write_case(tmp_path: Path) -> WriteCase:
    """A case bundling one plugin whose hooks.json hooks `events`."""

    def write(events: list[str], extra: str = "") -> Path:
        hooks = tmp_path / "plugin" / "hooks"
        hooks.mkdir(parents=True)
        config = {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": "true"}]}] for event in events
            }
        }
        (hooks / "hooks.json").write_text(json.dumps(config))
        path = tmp_path / CASE_FILENAME
        path.write_text(CASE.format(extra=extra))
        return path

    return write


def warnings_of(path: Path) -> list[str]:
    return [str(issue) for issue in CaseLinter().warnings(path, CaseLoader().load(path))]


class TestHookFiredWithoutEvent:
    def test_warns_when_the_bundle_hooks_several_events(self, write_case: WriteCase) -> None:
        path = write_case(["SessionStart", "UserPromptSubmit"], "")

        assert warnings_of(path) == [
            f"{path}:7: hook_fired grader nudge-shown has no event, so any of the bundle's hooks"
            " (SessionStart, UserPromptSubmit) passes it"
        ]

    def test_quiet_when_the_grader_names_its_event(self, write_case: WriteCase) -> None:
        path = write_case(["SessionStart", "UserPromptSubmit"], "    event: UserPromptSubmit\n")

        assert warnings_of(path) == []

    def test_quiet_when_the_bundle_hooks_one_event(self, write_case: WriteCase) -> None:
        path = write_case(["UserPromptSubmit"], "")

        assert warnings_of(path) == []

    def test_unreadable_hooks_json_is_reported(self, write_case: WriteCase) -> None:
        path = write_case(["SessionStart"], "")
        (path.parent / "plugin" / "hooks" / "hooks.json").write_text("{not json")

        [warning] = warnings_of(path)

        assert "plugin/hooks/hooks.json isn't valid JSON" in warning
