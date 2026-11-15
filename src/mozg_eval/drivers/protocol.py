"""The driver contract, and the adapter for drivers that speak JSONL over stdio."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal, Protocol, Self

import yaml
from pydantic import Field, ValidationError

from mozg_eval.drivers.process import ChildProcess
from mozg_eval.events.models import PAYLOAD, Error, Payload
from mozg_eval.spec.models import StrictModel, ToolKind

log = logging.getLogger(__name__)

type Support = Literal["native", "inferred", "none"]


class AgentError(RuntimeError):
    """The agent can't start at all, such as a failed `--version` check."""


class Capabilities(StrictModel):
    skills: Support = "none"
    subagents: Support = "none"
    hooks: Support = "none"
    usage: Literal["usd", "tokens", "none"] = "none"
    isolation: Literal["none", "home", "container"] = "none"
    host_shell: bool = Field(
        default=True,
        description="cases that grant shell may run it on the host, outside any sandbox",
    )
    tools: list[ToolKind] = Field(
        default_factory=list, description="tool kinds whose calls the driver reports"
    )
    tool_input: list[ToolKind] = Field(
        default_factory=list,
        description="kinds whose call input uses Claude Code's field names, so `match` applies",
    )


class DriverManifest(StrictModel):
    """A `driver.yaml`: how to start an external driver and what it can report."""

    name: str
    command: list[str] = Field(min_length=1)
    version_cmd: list[str] = Field(min_length=1)
    capabilities: Capabilities = Capabilities()

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# Agents without a turn cap of their own are stopped by the driver and still end ok.
MAX_TURNS_REASON = "max_turns reached"


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
    tools: list[ToolKind] | None = Field(
        default=None,
        description="the only tools offered; null means the agent's default set, empty means none",
    )
    model: str | None = None
    append_system_prompt: str | None = None
    limits: Limits


class Driver(Protocol):
    name: str
    capabilities: Capabilities

    async def version(self) -> str: ...

    def run(self, request: TrialRequest) -> AsyncIterator[Payload]: ...

    async def cancel(self, reason: str) -> None: ...


class StdioDriver:
    """Runs an executable that reads the request on stdin and writes one event per stdout line."""

    def __init__(self, manifest: DriverManifest, raw_dir: Path) -> None:
        self.name = manifest.name
        self.capabilities = manifest.capabilities
        self.manifest = manifest
        self._child = ChildProcess(f"driver {manifest.name}", raw_dir)

    async def version(self) -> str:
        process = await asyncio.create_subprocess_exec(
            *self.manifest.version_cmd, stdout=asyncio.subprocess.PIPE
        )
        out, _ = await process.communicate()
        if process.returncode != 0:
            raise AgentError(f"{self.manifest.version_cmd} exited with {process.returncode}")
        return out.decode().strip()

    async def run(self, request: TrialRequest) -> AsyncIterator[Payload]:
        lines = self._child.lines(
            self.manifest.command,
            cwd=request.workspace,
            stdin=request.model_dump_json().encode() + b"\n",
            keep_stdin=True,
        )
        async for line in lines:
            try:
                yield PAYLOAD.validate_json(line)
            except ValidationError as exc:
                reason = exc.errors()[0]["msg"]
                yield Error(message=f"driver {self.name} sent an invalid event: {reason}")
                await self.cancel("invalid event")
        code = self._child.returncode
        if code is not None and code > 0:
            yield Error(message=f"driver {self.name} exited with code {code}; see stderr.log")

    async def cancel(self, reason: str) -> None:
        if not self._child.running() or self._child.cancelling:
            return
        log.info("cancelling driver %s: %s", self.name, reason)
        self._child.kill_later()
        message = json.dumps({"type": "cancel", "reason": reason})
        try:
            await self._child.send(message.encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError):
            log.debug("driver %s closed stdin before the cancel; the kill timer stands", self.name)
