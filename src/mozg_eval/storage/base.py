"""The three stores between the core and storage; nothing above them builds paths or writes SQL."""

import re
from collections.abc import AsyncIterator, Sequence
from typing import Literal, Protocol

from pydantic import AwareDatetime, NonNegativeFloat, PositiveInt, field_validator

from mozg_eval.events.models import EndStatus, Event
from mozg_eval.spec.models import StrictModel

DEFAULT_WORKSPACE = "local"

# Ids become path segments on disk, so every backend accepts the same safe alphabet.
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def check_id(value: str, what: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError(f"{what} id {value!r} must match {_ID.pattern}")
    return value


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactRef(StrictModel):
    run: str
    name: str
    trial: str | None = None

    @field_validator("run", "trial")
    @classmethod
    def _safe(cls, value: str | None) -> str | None:
        return None if value is None else check_id(value, "artifact")

    @field_validator("name")
    @classmethod
    def _relative(cls, value: str) -> str:
        for part in value.split("/"):
            check_id(part, "artifact name segment")
        return value


class RunRecord(StrictModel):
    id: str
    started_at: AwareDatetime
    trigger: Literal["local", "ci"]
    git_sha: str | None = None


class TrialRecord(StrictModel):
    id: str
    run: str
    case: str
    arm: Literal["with", "without"]
    repeat: PositiveInt
    driver: str
    status: EndStatus
    cost_usd: NonNegativeFloat | None = None


class EventStore(Protocol):
    async def append(self, ws: str, run: str, trial: str, events: Sequence[Event]) -> None:
        """Store events in seq order; a resent (trial, seq) is skipped, a gap is an error."""
        ...

    def read(self, ws: str, run: str, trial: str, after_seq: int = 0) -> AsyncIterator[Event]: ...

    async def delete_run(self, ws: str, run: str) -> None: ...


class ArtifactStore(Protocol):
    async def put(self, ws: str, ref: ArtifactRef, data: AsyncIterator[bytes]) -> None: ...

    def get(self, ws: str, ref: ArtifactRef) -> AsyncIterator[bytes]: ...


class ResultStore(Protocol):
    def ensure_workspace(self, ws: str) -> None: ...

    def add_run(self, ws: str, run: RunRecord) -> None: ...

    def get_run(self, ws: str, run: str) -> RunRecord | None: ...

    def list_runs(self, ws: str) -> list[RunRecord]: ...

    def add_trial(self, ws: str, trial: TrialRecord) -> None: ...

    def list_trials(self, ws: str, run: str) -> list[TrialRecord]: ...

    def close(self) -> None: ...
