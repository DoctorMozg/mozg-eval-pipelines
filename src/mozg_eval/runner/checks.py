"""Post-run checks that need the live workspace; everything else grades from stored events."""

import asyncio
import hashlib
import logging
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from mozg_eval.events.models import ChangeKind, CheckResult, FileChange
from mozg_eval.spec.models import (
    CommandGrader,
    Count,
    FileChangedGrader,
    FileExistsGrader,
    FileTarget,
    Grader,
    RegexGrader,
    regex_flags,
)

log = logging.getLogger(__name__)

_DETAIL_CHARS = 2000

type Snapshot = dict[str, str]  # workspace-relative POSIX path -> sha256 of the content


def in_range(count: int, bounds: Count) -> bool:
    return count >= bounds.min and (bounds.max is None or count <= bounds.max)


def needs_workspace(grader: Grader) -> bool:
    if isinstance(grader, RegexGrader):
        return grader.target == "created_files" or isinstance(grader.target, FileTarget)
    if isinstance(grader, FileChangedGrader):
        return grader.content_match is not None
    return isinstance(grader, CommandGrader | FileExistsGrader)


# Running the code under test writes these; they are never the agent's own edits.
_UNTRACKED_DIRS = {".git", "__pycache__", ".pytest_cache"}


def snapshot(workspace: Path) -> Snapshot:
    files = (
        p
        for p in workspace.rglob("*")
        if p.is_file() and _UNTRACKED_DIRS.isdisjoint(p.relative_to(workspace).parts)
    )
    return {
        p.relative_to(workspace).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in files
    }


class WorkspaceChecks:
    """Compares the workspace with its pre-run snapshot and runs the live-workspace graders."""

    def __init__(self, workspace: Path, before: Snapshot, env: Mapping[str, str]) -> None:
        self.workspace = workspace
        self.before = before
        self.after = snapshot(workspace)
        self.env = env

    def changes(self) -> list[FileChange]:
        paths = sorted(self.before.keys() | self.after.keys())
        changes = []
        for path in paths:
            old, new = self.before.get(path), self.after.get(path)
            if old == new:
                continue
            change: ChangeKind = (
                "created" if old is None else "deleted" if new is None else "modified"
            )
            changes.append(FileChange(path=str(self.workspace / path), change=change))
        return changes

    def created(self) -> list[str]:
        return sorted(self.after.keys() - self.before.keys())

    async def check(self, grader: Grader) -> CheckResult:
        if isinstance(grader, CommandGrader):
            return await self._command(grader)
        if isinstance(grader, FileExistsGrader):
            matches = [p for p in self.created() if PurePosixPath(p).full_match(grader.path)]
            passed = bool(matches) == grader.exists
            detail = ", ".join(matches) or f"no created file matches {grader.path}"
            return CheckResult(name=grader.name, passed=passed, detail=detail)
        if isinstance(grader, RegexGrader):
            return self._regex(grader)
        if isinstance(grader, FileChangedGrader):
            return self._file_changed(grader)
        raise ValueError(f"grader {grader.name} ({grader.type}) doesn't need the workspace")

    def _regex(self, grader: RegexGrader) -> CheckResult:
        if isinstance(grader.target, FileTarget):
            path = self.workspace / grader.target.file
            if not path.is_file():
                passed = in_range(0, grader)
                return CheckResult(name=grader.name, passed=passed, detail=f"{path} is missing")
            texts = [path.read_text(errors="replace")]
        else:
            texts = [(self.workspace / p).read_text(errors="replace") for p in self.created()]
        pattern = re.compile(grader.pattern, regex_flags(grader.flags))
        count = sum(len(pattern.findall(text)) for text in texts)
        return CheckResult(
            name=grader.name, passed=in_range(count, grader), detail=f"{count} matches"
        )

    def _file_changed(self, grader: FileChangedGrader) -> CheckResult:
        assert grader.content_match is not None
        content = re.compile(grader.content_match)
        matched = [
            change.path
            for change in self.changes()
            if change.change != "deleted"
            and grader.change in (None, change.change)
            and (grader.path_match is None or re.search(grader.path_match, change.path))
            and content.search(Path(change.path).read_text(errors="replace"))
        ]
        detail = ", ".join(matched) or "no changed file matches"
        return CheckResult(name=grader.name, passed=in_range(len(matched), grader), detail=detail)

    async def _command(self, grader: CommandGrader) -> CheckResult:
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            grader.run,
            cwd=self.workspace,
            env=dict(self.env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), grader.timeout_s)
        except TimeoutError:
            process.kill()
            await process.wait()
            log.warning("check %s timed out after %ss", grader.name, grader.timeout_s)
            return CheckResult(
                name=grader.name, passed=False, detail=f"timed out after {grader.timeout_s}s"
            )
        detail = out.decode(errors="replace")[-_DETAIL_CHARS:]
        return CheckResult(name=grader.name, passed=process.returncode == 0, detail=detail)
