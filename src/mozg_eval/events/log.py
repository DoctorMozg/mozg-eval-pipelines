"""The run log: one append-only `events.jsonl` per trial, which graders read after the run."""

from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import ValidationError

from mozg_eval.events.models import Event


class RunLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a", encoding="utf-8")

    def append(self, event: Event) -> None:
        self._file.write(event.model_dump_json() + "\n")
        self._file.flush()  # a crash mid-run still leaves every event up to it on disk

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def read(path: Path) -> list[Event]:
        events = []
        with path.open(encoding="utf-8") as lines:
            for number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    events.append(Event.model_validate_json(line))
                except ValidationError as exc:
                    raise ValueError(f"{path}:{number}: not a valid event: {exc}") from exc
        return events
