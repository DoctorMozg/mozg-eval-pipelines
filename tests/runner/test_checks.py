import asyncio
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from mozg_eval.events.models import CheckResult, FileChange
from mozg_eval.runner.checks import WorkspaceChecks, needs_workspace, snapshot
from mozg_eval.spec.models import Grader

GRADER = TypeAdapter[Grader](Grader)


def grader(**fields: object) -> Grader:
    return GRADER.validate_python({"name": "g", **fields})


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    (ws / "kept.txt").write_text("same")
    (ws / "edited.txt").write_text("before")
    (ws / "gone.txt").write_text("bye")
    (ws / "notes.md").write_text("pre-existing note")
    return ws


@pytest.fixture
def checks(workspace: Path) -> WorkspaceChecks:
    """The agent's run: one file edited, one deleted, two created, git internals touched."""
    before = snapshot(workspace)
    (workspace / "edited.txt").write_text("after")
    (workspace / "gone.txt").unlink()
    (workspace / "docs").mkdir()
    (workspace / "docs" / "plan.md").write_text("TODO one\nTODO two\n")
    (workspace / "out.json").write_text("{}")
    (workspace / ".git" / "index").write_text("x")
    return WorkspaceChecks(workspace, before, {"PATH": "/usr/bin:/bin", "HOME": "/nowhere"})


def check(checks: WorkspaceChecks, **fields: object) -> CheckResult:
    return asyncio.run(checks.check(grader(**fields)))


class TestChanges:
    def test_diff_against_the_snapshot_ignores_git(
        self, checks: WorkspaceChecks, workspace: Path
    ) -> None:
        assert checks.changes() == [
            FileChange(path=str(workspace / "docs/plan.md"), change="created"),
            FileChange(path=str(workspace / "edited.txt"), change="modified"),
            FileChange(path=str(workspace / "gone.txt"), change="deleted"),
            FileChange(path=str(workspace / "out.json"), change="created"),
        ]


class TestFileExists:
    def test_globs_only_over_created_files(self, checks: WorkspaceChecks) -> None:
        assert check(checks, type="file_exists", path="**/*.md").detail == "docs/plan.md"
        assert check(checks, type="file_exists", path="kept.txt").passed is False

    def test_exists_false_passes_when_nothing_matches(self, checks: WorkspaceChecks) -> None:
        assert check(checks, type="file_exists", path="*.py", exists=False).passed


class TestRegex:
    def test_file_target_counts_matches(self, checks: WorkspaceChecks) -> None:
        result = check(
            checks,
            type="regex",
            pattern="todo",
            flags="i",
            target={"file": "docs/plan.md"},
            min=2,
            max=2,
        )

        assert (result.passed, result.detail) == (True, "2 matches")

    def test_missing_file_passes_only_a_zero_count(
        self, checks: WorkspaceChecks, workspace: Path
    ) -> None:
        absent = {"file": "nope.md"}

        assert check(checks, type="regex", pattern="x", target=absent, min=0, max=0).passed
        assert not check(checks, type="regex", pattern="x", target=absent).passed

    def test_created_files_target_skips_files_that_existed(self, checks: WorkspaceChecks) -> None:
        assert not check(
            checks, type="regex", pattern="pre-existing", target="created_files"
        ).passed
        assert check(checks, type="regex", pattern="TODO", target="created_files", min=2).passed


class TestCommand:
    def test_passes_on_exit_zero_in_the_workspace(self, checks: WorkspaceChecks) -> None:
        result = check(checks, type="command", run='test -f out.json && echo "home=$HOME"')

        assert (result.passed, result.detail) == (True, "home=/nowhere\n")

    def test_fails_on_nonzero_exit_and_timeout(self, checks: WorkspaceChecks) -> None:
        assert not check(checks, type="command", run="exit 1").passed
        slow = check(checks, type="command", run="sleep 5", timeout_s=1)
        assert (slow.passed, slow.detail) == (False, "timed out after 1s")


@pytest.mark.parametrize(
    ("fields", "live"),
    [
        ({"type": "command", "run": "true"}, True),
        ({"type": "file_exists", "path": "*.md"}, True),
        ({"type": "regex", "pattern": "x", "target": {"file": "a"}}, True),
        ({"type": "regex", "pattern": "x", "target": "created_files"}, True),
        ({"type": "regex", "pattern": "x", "target": "final"}, False),
        ({"type": "file_changed"}, False),
    ],
)
def test_only_workspace_graders_run_live(fields: dict[str, object], live: bool) -> None:
    assert needs_workspace(grader(**fields)) is live
