"""The three stores between the core and storage; nothing above them builds paths or writes SQL."""

import os
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, NonNegativeFloat, PositiveInt, field_validator

from mozg_eval.events.models import EndStatus, Event
from mozg_eval.grading.models import GradeResult, JudgeSpend
from mozg_eval.spec.models import StrictModel

DEFAULT_WORKSPACE = "local"

type TrialArm = Literal["with", "without"]

# Ids become path segments on disk, so every backend accepts the same safe alphabet.
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


# Within one millisecond the 74 low bits count up from a random start (RFC 9562 6.2, method 1),
# so stores that order by id keep creation order; the start leaves 2**72 steps of headroom.
_last_id = (0, 0)
_id_lock = threading.Lock()


def new_id() -> str:
    """A UUIDv7 (RFC 9562): increasing within the process, and never clashing across databases."""
    global _last_id
    with _id_lock:
        millis = time.time_ns() // 1_000_000
        last_millis, last_counter = _last_id
        if millis <= last_millis:
            millis, counter = last_millis, last_counter + 1
        else:
            counter = int.from_bytes(os.urandom(9))
        _last_id = (millis, counter)
    rand_a, rand_b = counter >> 62, counter & (2**62 - 1)
    value = (millis << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return str(uuid.UUID(int=value))


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
    arm: TrialArm
    repeat: PositiveInt
    driver: str
    status: EndStatus
    cost_usd: NonNegativeFloat | None = None
    passed: bool | None = Field(default=None, description="None until the trial is graded")


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

    def add_grades(self, ws: str, trial: str, results: Sequence[GradeResult]) -> None: ...

    def list_grades(self, ws: str, run: str) -> dict[str, list[GradeResult]]: ...

    def regrade(self, ws: str, trial: str, results: Sequence[GradeResult], passed: bool) -> None:
        """Replace the trial's grades of the same names and its pass flag, all or nothing."""
        ...

    def add_spend(self, ws: str, trial: str, spend: Sequence[JudgeSpend]) -> None:
        """Append ledger rows; a rejudge adds to them, it never replaces them."""
        ...

    def list_spend(self, ws: str, run: str) -> dict[str, list[JudgeSpend]]: ...

    def close(self) -> None: ...
