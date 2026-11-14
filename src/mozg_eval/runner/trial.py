"""Runs one trial: streams a driver's events into the event store; a tripped guard cancels it."""

import asyncio
import logging
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime
from typing import Protocol

from mozg_eval.drivers.protocol import Driver, TrialRequest
from mozg_eval.events.models import (
    CheckResult,
    End,
    EndStatus,
    Error,
    Event,
    Message,
    Payload,
    Setting,
    Start,
    ToolCall,
    Usage,
)
from mozg_eval.spec.models import ToolKind
from mozg_eval.storage.base import EventStore

log = logging.getLogger(__name__)


class Guard(Protocol):
    def check(self, payload: Payload) -> str | None:
        """Return why the trial must stop, or None to let it go on."""
        ...


class SpendCap:
    """One spend cap for a whole run; trials running side by side all count into it."""

    def __init__(self, max_usd: float) -> None:
        self.max_usd = max_usd
        self.spent = 0.0
        self.tripped = False

    def add(self, usd: float) -> None:
        self.spent += usd

    def reached(self) -> bool:
        return self.spent >= self.max_usd


class CostGuard:
    """Counts a trial's agent cost into the run's cap and stops the trial that crosses it."""

    def __init__(self, cap: SpendCap) -> None:
        self.cap = cap

    def check(self, payload: Payload) -> str | None:
        if isinstance(payload, Usage) and payload.cost_usd is not None:
            self.cap.add(payload.cost_usd)
            if self.cap.spent > self.cap.max_usd:
                self.cap.tripped = True
                return f"cost ${self.cap.spent:.2f} is over the ${self.cap.max_usd:.2f} cap"
        return None


class TurnGuard:
    """Backs up the driver's own max_turns, in case the agent CLI ignores it."""

    def __init__(self, max_turns: int) -> None:
        self.max_turns = max_turns
        self.turns = 0

    def check(self, payload: Payload) -> str | None:
        if isinstance(payload, Message) and payload.role == "assistant":
            self.turns += 1
            if self.turns > self.max_turns:
                return f"turn {self.turns} is over max_turns {self.max_turns}"
        return None


class ForbiddenToolGuard:
    def __init__(self, kinds: Collection[ToolKind]) -> None:
        self.kinds = kinds

    def check(self, payload: Payload) -> str | None:
        if isinstance(payload, ToolCall) and payload.kind in self.kinds:
            return f"forbidden tool {payload.name} ({payload.kind})"
        return None


class TrialRunner:
    """One instance per trial: guards keep running totals."""

    def __init__(
        self,
        ws: str,
        run_id: str,
        driver: Driver,
        store: EventStore,
        guards: Sequence[Guard],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.ws = ws
        self.run_id = run_id
        self.driver = driver
        self.store = store
        self.guards = guards
        self.clock = clock
        self.events: list[Event] = []
        self._stop: tuple[EndStatus, str] | None = None
        self._pending: set[asyncio.Task[None]] = set()

    async def run(self, request: TrialRequest, settings: dict[str, Setting]) -> list[Event]:
        """Return the events exactly as stored, ending with `end`; see `append` for later ones."""
        start = Start(
            driver=self.driver.name,
            driver_version=await self.driver.version(),
            prompt=request.prompt,
            workspace=request.workspace,
            settings=settings,
        )
        await self._emit(request.trial, start)
        driver_end: End | None = None
        timer = asyncio.get_running_loop().call_later(
            request.limits.timeout_s, self._stop_on_timeout, request.limits.timeout_s
        )
        try:
            async for payload in self.driver.run(request):
                if isinstance(payload, End):
                    driver_end = payload
                    continue
                if isinstance(payload, Start | CheckResult):
                    payload = Error(
                        message=f"driver sent '{payload.type}', which only the core writes"
                    )
                await self._emit(request.trial, payload)
                await self._check_guards(payload)
        finally:
            timer.cancel()
        await self._emit(request.trial, self._end(driver_end))
        return self.events

    async def append(self, trial: str, payload: Payload) -> None:
        """Record a core-made event after `end`, such as a post-run workspace check."""
        await self._emit(trial, payload)

    async def _emit(self, trial: str, payload: Payload) -> None:
        event = Event(
            run=self.run_id, trial=trial, seq=len(self.events) + 1, t=self.clock(), payload=payload
        )
        await self.store.append(self.ws, self.run_id, trial, [event])
        self.events.append(event)

    async def _check_guards(self, payload: Payload) -> None:
        if self._stop is not None:
            return
        for guard in self.guards:
            if reason := guard.check(payload):
                self._stop = ("cancelled", reason)
                await self.driver.cancel(reason)
                return

    def _stop_on_timeout(self, timeout_s: int) -> None:
        if self._stop is not None:
            return
        reason = f"no end after timeout_s {timeout_s}"
        self._stop = ("timeout", reason)
        task = asyncio.create_task(self.driver.cancel(reason))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _end(self, driver_end: End | None) -> End:
        # The core's reason to stop outranks whatever status the driver reported after the cancel.
        if self._stop is not None:
            status, reason = self._stop
            log.info("trial stopped: %s", reason)
            return End(status=status, reason=reason)
        if driver_end is not None:
            return driver_end
        return End(status="error", reason=f"driver {self.driver.name} exited without an end event")
