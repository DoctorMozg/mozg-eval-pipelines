"""OpenCode V2 driver: `opencode run --standalone --format json` in a temp HOME."""

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from pydantic import BaseModel, JsonValue, ValidationError

from mozg_eval.drivers.claude_plugins import PluginCopies, PluginRenderError
from mozg_eval.drivers.process import ChildProcess
from mozg_eval.drivers.protocol import MAX_TURNS_REASON, Capabilities, TrialRequest
from mozg_eval.events.models import (
    End,
    Error,
    Log,
    Message,
    Payload,
    SkillActivated,
    ToolCall,
    ToolResult,
    Usage,
)
from mozg_eval.spec.models import ToolKind

log = logging.getLogger(__name__)

OPENCODE_TOOLS: dict[str, ToolKind] = {
    "read": "read",
    "glob": "glob",
    "grep": "grep",
    "edit": "edit",
    "write": "write",
    "patch": "edit",
    "shell": "shell",
    "skill": "skill",
    "subagent": "subagent",
    "question": "ask_user",
}
# The permission action that gates each tool kind; write and patch are edits.
_ACTIONS: dict[ToolKind, str] = {
    "skill": "skill",
    "subagent": "subagent",
    "read": "read",
    "write": "edit",
    "edit": "edit",
    "glob": "glob",
    "grep": "grep",
    "shell": "shell",
    "ask_user": "question",
}
_SLASH = re.compile(r"^/[a-z0-9-]+:([a-z0-9-]+)(?:\s+|$)")


class _Loose(BaseModel, extra="ignore"):
    pass


class _State(_Loose):
    status: str
    input: dict[str, JsonValue] = {}
    output: str = ""
    error: str = ""


class _Cache(_Loose):
    read: int = 0
    write: int = 0


class _Tokens(_Loose):
    input: int = 0
    output: int = 0
    cache: _Cache = _Cache()


class _Part(_Loose):
    id: str = ""
    text: str = ""
    tool: str = ""
    state: _State | None = None
    cost: float | None = None
    tokens: _Tokens | None = None


class OpenCodeLine(_Loose):
    type: str
    part: _Part | None = None
    error: JsonValue = None


def opencode_prompt(prompt: str) -> str:
    """`/plugin:skill args` names a Claude skill; OpenCode loads one when the prompt says `@id`."""
    if match := _SLASH.match(prompt):
        return f"@{match.group(1)} {prompt[match.end() :]}".rstrip()
    return prompt


def isolated_env(base: Mapping[str, str], home: Path) -> dict[str, str]:
    """XDG and OPENCODE_* variables would point OpenCode back at the user's data and server."""
    env = {k: v for k, v in base.items() if not k.startswith(("XDG_", "OPENCODE_", "CLAUDE"))}
    env["HOME"] = str(home)
    return env


class OpenCodeStream:
    """Turns `run --format json` lines into events; it emits no end, so the driver adds one."""

    def __init__(self, max_turns: int) -> None:
        self.max_turns = max_turns
        self.turns = 0

    def normalize(self, line: OpenCodeLine) -> list[Payload]:
        part = line.part
        if line.type == "error":
            return [Error(message=json.dumps(line.error) if line.error is not None else "error")]
        if part is None:
            return []
        if line.type == "text" and part.text:
            return self._turn([Message(role="assistant", text=part.text)])
        if line.type == "tool_use" and part.state is not None:
            return self._turn(self._tool(part, part.state))
        if line.type == "step_finish" and part.tokens is not None:
            tokens = part.tokens
            total_in = tokens.input + tokens.cache.read + tokens.cache.write
            return [Usage(input_tokens=total_in, output_tokens=tokens.output, cost_usd=part.cost)]
        return []

    def _turn(self, payloads: list[Payload]) -> list[Payload]:
        self.turns += 1
        if self.turns > self.max_turns:
            return [End(status="ok", reason=MAX_TURNS_REASON)]
        return payloads

    @staticmethod
    def _tool(part: _Part, state: _State) -> list[Payload]:
        ok = state.status == "completed"
        payloads: list[Payload] = [
            ToolCall(
                id=part.id,
                kind=OPENCODE_TOOLS.get(part.tool, "other"),
                name=part.tool,
                input=state.input,
            )
        ]
        if part.tool == "skill" and ok and isinstance(skill := state.input.get("id"), str):
            payloads.append(SkillActivated(skill=skill, evidence="native"))
        payloads.append(ToolResult(id=part.id, ok=ok, output=state.output or state.error))
        return payloads


class OpenCodeDriver:
    name = "opencode"
    # Subagent calls are reported, but which agent ran isn't recorded yet.
    capabilities = Capabilities(
        skills="native",
        subagents="none",
        hooks="none",
        usage="usd",
        isolation="home",
        host_shell=False,
        tools=sorted(set(OPENCODE_TOOLS.values())),
    )

    def __init__(self, home: Path, raw_dir: Path, executable: str = "opencode") -> None:
        self.home = home
        self.raw_dir = raw_dir
        self.executable = executable
        self._child = ChildProcess("opencode", raw_dir)

    async def version(self) -> str:
        process = await asyncio.create_subprocess_exec(
            self.executable, "--version", stdout=asyncio.subprocess.PIPE, env=self._env()
        )
        out, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"{self.executable} --version exited with {process.returncode}")
        return out.decode().strip()

    def command(self, request: TrialRequest) -> list[str]:
        # --standalone keeps the server in this process, so nothing outlives the trial.
        argv = [self.executable, "run", "--standalone", "--format", "json", "--auto", "--archived"]
        if request.model:
            argv += ["--model", request.model]
        return [*argv, opencode_prompt(request.prompt)]

    async def run(self, request: TrialRequest) -> AsyncIterator[Payload]:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._write_config(request)
        except PluginRenderError as exc:
            yield Error(message=str(exc))
            yield End(status="error", reason="plugin copy failed")
            return
        if request.append_system_prompt:
            yield Log(level="warning", text="opencode driver ignores append_system_prompt")
        stream = OpenCodeStream(request.limits.max_turns)
        ended = False
        async for raw_line in self._child.lines(
            self.command(request), cwd=request.workspace, env=self._env() | request.env
        ):
            if ended:
                continue
            try:
                line = OpenCodeLine.model_validate_json(raw_line)
            except ValidationError:
                text = raw_line.decode(errors="replace").strip()[:200]
                yield Log(level="warning", text=f"opencode printed a non-event line: {text}")
                continue
            for payload in stream.normalize(line):
                yield payload
                if isinstance(payload, End):
                    ended = True
                    self._child.terminate(MAX_TURNS_REASON)
                    break
        if ended:
            return
        code = self._child.returncode
        if code == 0:
            yield End(status="ok")
        else:
            yield Error(message=f"opencode exited with code {code}; see stderr.log")
            yield End(status="error", reason=f"opencode exited with code {code}")

    async def cancel(self, reason: str) -> None:
        self._child.terminate(reason)

    def _env(self) -> dict[str, str]:
        return isolated_env(os.environ, self.home)

    def _write_config(self, request: TrialRequest) -> None:
        copies = PluginCopies(self.home / ".mzeval-plugins")
        skills: list[JsonValue] = [
            str(copies.add(Path(plugin)) / "skills") for plugin in request.plugins
        ]
        config: dict[str, JsonValue] = {"skills": skills}
        if request.tools:
            # Last match wins: deny everything, then allow the case's own tools.
            allowed = sorted({_ACTIONS[kind] for kind in request.tools})
            rules: list[JsonValue] = [{"action": "*", "resource": "*", "effect": "deny"}]
            rules += [{"action": a, "resource": "*", "effect": "allow"} for a in allowed]
            config["permissions"] = rules
        path = self.home / ".config" / "opencode" / "opencode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config))
        log.debug("opencode config for %d plugins at %s", len(skills), path)
