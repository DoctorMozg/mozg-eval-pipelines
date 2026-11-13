"""Runs one trial: streams a driver's events into the run log and cancels it when a guard trips."""

import asyncio
import logging
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime
from typing import Protocol

from mozg_eval.drivers.protocol import Driver, TrialRequest
from mozg_eval.events.log import RunLog
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

log = logging.getLogger(__name__)


class Guard(Protocol):
    def check(self, payload: Payload) -> str | None:
        """Return why the trial must stop, or None to let it go on."""
        ...


class CostGuard:
    def __init__(self, max_usd: float) -> None:
        self.max_usd = max_usd
        self.spent = 0.0

    def check(self, payload: Payload) -> str | None:
        if isinstance(payload, Usage) and payload.cost_usd is not None:
            self.spent += payload.cost_usd
            if self.spent > self.max_usd:
                return f"cost ${self.spent:.2f} is over the ${self.max_usd:.2f} cap"
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
        run_id: str,
        driver: Driver,
        log: RunLog,
        guards: Sequence[Guard],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.run_id = run_id
        self.driver = driver
        self.log = log
        self.guards = guards
        self.clock = clock
        self.events: list[Event] = []
        self._stop: tuple[EndStatus, str] | None = None
        self._pending: set[asyncio.Task[None]] = set()

    async def run(self, request: TrialRequest, settings: dict[str, Setting]) -> list[Event]:
        """Return the events exactly as written to the log; the last one is always `end`."""
        start = Start(
            driver=self.driver.name,
            driver_version=await self.driver.version(),
            prompt=request.prompt,
            workspace=request.workspace,
            settings=settings,
        )
        self._emit(request.trial, start)
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
                self._emit(request.trial, payload)
                await self._check_guards(payload)
        finally:
            timer.cancel()
        self._emit(request.trial, self._end(driver_end))
        return self.events

    def _emit(self, trial: str, payload: Payload) -> None:
        event = Event(
            run=self.run_id, trial=trial, seq=len(self.events) + 1, t=self.clock(), payload=payload
        )
        self.log.append(event)
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
