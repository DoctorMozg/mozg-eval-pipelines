"""The driver contract, and the adapter for drivers that speak JSONL over stdio."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal, Protocol, Self

import yaml
from pydantic import Field, ValidationError

from mozg_eval.events.models import PAYLOAD, Error, Payload
from mozg_eval.spec.models import StrictModel, ToolKind

log = logging.getLogger(__name__)

# How long a driver has to exit after a cancel before it is killed.
CANCEL_GRACE_S = 5.0
# A line can hold a whole skill body or tool output; asyncio's default limit is 64 KiB.
LINE_LIMIT = 16 << 20

type Support = Literal["native", "inferred", "none"]


class Capabilities(StrictModel):
    skills: Support = "none"
    subagents: Support = "none"
    hooks: Support = "none"
    usage: Literal["usd", "tokens", "none"] = "none"
    isolation: Literal["none", "home", "container"] = "none"


class DriverManifest(StrictModel):
    """A `driver.yaml`: how to start an external driver and what it can report."""

    name: str
    command: list[str] = Field(min_length=1)
    version_cmd: list[str] = Field(min_length=1)
    capabilities: Capabilities = Capabilities()

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class Limits(StrictModel):
    max_turns: int
    timeout_s: int


class TrialRequest(StrictModel):
    """The first line a driver reads on stdin: everything it needs to run one trial."""

    type: Literal["start"] = "start"
    trial: str
    prompt: str
    workspace: str
    env: dict[str, str] = Field(default_factory=dict)
    plugins: list[str] = Field(
        default_factory=list,
        description="plugin directories the case loads; each driver installs them its own way",
    )
    tools: list[ToolKind] = Field(
        default_factory=list,
        description="the only tools offered; empty means the agent's default set",
    )
    model: str | None = None
    append_system_prompt: str | None = None
    limits: Limits


class Driver(Protocol):
    name: str

    async def version(self) -> str: ...

    def run(self, request: TrialRequest) -> AsyncIterator[Payload]: ...

    async def cancel(self, reason: str) -> None: ...


class StdioDriver:
    """Runs an executable that reads the request on stdin and writes one event per stdout line."""

    def __init__(self, manifest: DriverManifest, raw_dir: Path) -> None:
        self.name = manifest.name
        self.manifest = manifest
        self.raw_dir = raw_dir
        self._process: asyncio.subprocess.Process | None = None
        self._kill_timer: asyncio.TimerHandle | None = None

    async def version(self) -> str:
        process = await asyncio.create_subprocess_exec(
            *self.manifest.version_cmd, stdout=asyncio.subprocess.PIPE
        )
        out, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"{self.manifest.version_cmd} exited with {process.returncode}")
        return out.decode().strip()

    async def run(self, request: TrialRequest) -> AsyncIterator[Payload]:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        with (
            (self.raw_dir / "stderr.log").open("wb") as stderr,
            (self.raw_dir / "raw.jsonl").open("ab") as raw,
        ):
            process = self._process = await asyncio.create_subprocess_exec(
                *self.manifest.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                cwd=request.workspace,
                limit=LINE_LIMIT,
            )
            await self._send(request.model_dump_json())
            assert process.stdout is not None
            # Read to EOF even after a cancel, so a chatty driver never blocks on a full pipe.
            async for line in process.stdout:
                raw.write(line)
                if not line.strip():
                    continue
                try:
                    yield PAYLOAD.validate_json(line)
                except ValidationError as exc:
                    reason = exc.errors()[0]["msg"]
                    yield Error(message=f"driver {self.name} sent an invalid event: {reason}")
                    await self.cancel("invalid event")
            code = await process.wait()
        if self._kill_timer is not None:
            self._kill_timer.cancel()
        log.debug("driver %s exited with %s", self.name, code)
        if code > 0:
            yield Error(message=f"driver {self.name} exited with code {code}; see stderr.log")

    async def cancel(self, reason: str) -> None:
        process = self._process
        if process is None or process.returncode is not None or self._kill_timer is not None:
            return
        log.info("cancelling driver %s: %s", self.name, reason)
        loop = asyncio.get_running_loop()
        self._kill_timer = loop.call_later(CANCEL_GRACE_S, self._kill, process)
        try:
            await self._send(json.dumps({"type": "cancel", "reason": reason}))
        except (BrokenPipeError, ConnectionResetError):
            log.debug("driver %s closed stdin before the cancel; the kill timer stands", self.name)

    def _kill(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            log.warning("driver %s ignored cancel for %ss; killing it", self.name, CANCEL_GRACE_S)
            process.kill()

    async def _send(self, line: str) -> None:
        assert self._process is not None
        stdin = self._process.stdin
        assert stdin is not None
        stdin.write(line.encode() + b"\n")
        await stdin.drain()
