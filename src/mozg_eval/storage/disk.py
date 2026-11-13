"""Disk backends: `{workspace}/{run}/{trial}/events.jsonl` and artifact files under a root dir."""

import logging
import shutil
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from pydantic import ValidationError

from mozg_eval.events.models import Event
from mozg_eval.storage.base import ArtifactNotFoundError, ArtifactRef, check_id

log = logging.getLogger(__name__)

_CHUNK = 1 << 16


class DiskEventStore:
    """Assumes one writer per trial, so the last seq of each log can be cached in memory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._last_seq: dict[Path, int] = {}

    def _path(self, ws: str, run: str, trial: str) -> Path:
        return (
            self.root
            / check_id(ws, "workspace")
            / check_id(run, "run")
            / check_id(trial, "trial")
            / "events.jsonl"
        )

    async def append(self, ws: str, run: str, trial: str, events: Sequence[Event]) -> None:
        path = self._path(ws, run, trial)
        last = self._last_seq.get(path)
        if last is None:
            last = 0
            async for event in self.read(ws, run, trial):
                last = event.seq
        fresh = []
        for event in events:
            if (event.run, event.trial) != (run, trial):
                raise ValueError(f"event {event.run}/{event.trial} appended to {run}/{trial}")
            if event.seq <= last:
                continue  # a resumed push resends the tail
            if event.seq != last + 1:
                raise ValueError(f"{path}: seq {event.seq} follows {last}, events are missing")
            fresh.append(event)
            last = event.seq
        if skipped := len(events) - len(fresh):
            log.debug("%s: skipped %d events already stored", path, skipped)
        if fresh:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as out:
                out.writelines(event.model_dump_json() + "\n" for event in fresh)
        self._last_seq[path] = last

    async def read(self, ws: str, run: str, trial: str, after_seq: int = 0) -> AsyncIterator[Event]:
        path = self._path(ws, run, trial)
        if not path.exists():
            return
        with path.open(encoding="utf-8") as lines:
            for number, line in enumerate(lines, start=1):
                try:
                    event = Event.model_validate_json(line)
                except ValidationError as exc:
                    raise ValueError(f"{path}:{number}: not a valid event: {exc}") from exc
                if event.seq > after_seq:
                    yield event

    async def delete_run(self, ws: str, run: str) -> None:
        run_dir = self.root / check_id(ws, "workspace") / check_id(run, "run")
        self._last_seq = {p: s for p, s in self._last_seq.items() if not p.is_relative_to(run_dir)}
        if run_dir.exists():
            shutil.rmtree(run_dir)
            log.info("deleted events of run %s/%s", ws, run)


class DiskArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, ws: str, ref: ArtifactRef) -> Path:
        path = self.root / check_id(ws, "workspace") / ref.run
        if ref.trial is not None:
            path /= ref.trial
        return path / ref.name

    async def put(self, ws: str, ref: ArtifactRef, data: AsyncIterator[bytes]) -> None:
        path = self._path(ws, ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Readers never see a half-written file: write aside, then rename over the target.
        part = path.with_name(path.name + ".part")
        with part.open("wb") as out:
            async for chunk in data:
                out.write(chunk)
        part.replace(path)

    async def get(self, ws: str, ref: ArtifactRef) -> AsyncIterator[bytes]:
        path = self._path(ws, ref)
        if not path.is_file():
            raise ArtifactNotFoundError(f"no artifact {ref.model_dump()} in workspace {ws}")
        with path.open("rb") as data:
            while chunk := data.read(_CHUNK):
                yield chunk
