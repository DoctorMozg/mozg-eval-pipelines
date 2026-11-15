"""Pi driver: `pi --mode json` in a temp HOME, with the case's plugin skills passed by path."""

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, JsonValue, ValidationError

from mozg_eval.drivers.claude_plugins import PluginCopies, PluginRenderError
from mozg_eval.drivers.pi_harness import PiHarness
from mozg_eval.drivers.process import ChildProcess
from mozg_eval.drivers.protocol import MAX_TURNS_REASON, AgentError, Capabilities, TrialRequest
from mozg_eval.events.models import (
    End,
    Error,
    Message,
    Payload,
    SkillActivated,
    ToolCall,
    ToolResult,
    Usage,
)
from mozg_eval.spec.harness import HarnessLoader
from mozg_eval.spec.models import ToolKind

log = logging.getLogger(__name__)

PI_TOOL_KINDS: dict[str, ToolKind] = {
    "read": "read",
    "write": "write",
    "edit": "edit",
    "bash": "shell",
    "grep": "grep",
    "find": "glob",
}
# Skills load through `read`, so a skill-only case still needs it.
_PI_TOOLS: dict[ToolKind, str] = {kind: name for name, kind in PI_TOOL_KINDS.items()}
_PI_TOOLS["skill"] = "read"
_SLASH = re.compile(r"^/[a-z0-9-]+:([a-z0-9-]+)(?=\s|$)")
PLUGINS_DIR = ".mzeval-plugins"


class _Loose(BaseModel, extra="ignore"):
    pass


class _Cost(_Loose):
    total: float | None = None


# Field names follow Pi's camelCase JSON.
class _Usage(_Loose):
    input: int = 0
    output: int = 0
    cacheRead: int = 0
    cacheWrite: int = 0
    cost: _Cost = _Cost()


class _Message(_Loose):
    role: str
    content: list[dict[str, JsonValue]] | str = []
    usage: _Usage | None = None
    stopReason: str = ""
    errorMessage: str = ""


class _Result(_Loose):
    content: list[dict[str, JsonValue]] = []


class PiLine(_Loose):
    type: str
    message: _Message | None = None
    toolCallId: str = ""
    toolName: str = ""
    args: dict[str, JsonValue] = {}
    result: _Result | None = None
    isError: bool = False
    aborted: bool = False
    success: bool = True
    finalError: str = ""


def pi_prompt(prompt: str) -> str:
    """`/plugin:skill args` names a Claude skill; Pi's own form is `/skill:name args`."""
    return _SLASH.sub(r"/skill:\1", prompt, count=1)


def isolated_env(base: Mapping[str, str], home: Path) -> dict[str, str]:
    """PI_CODING_AGENT_DIR would point Pi back at the user's config and saved logins."""
    env = {k: v for k, v in base.items() if not k.startswith(("PI_", "CLAUDE"))}
    env["HOME"] = str(home)
    return env


def _texts(blocks: list[dict[str, JsonValue]] | str) -> str:
    if isinstance(blocks, str):
        return blocks
    return "".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")


class PiStream:
    """Turns `--mode json` lines into events; reading a plugin's or user's SKILL.md fires it."""

    def __init__(self, max_turns: int, plugins: Path, user_skills: Path | None = None) -> None:
        self.max_turns = max_turns
        self.turns = 0
        roots = [rf"{re.escape(str(plugins))}/[^/]+/skills"]
        roots += [re.escape(str(user_skills))] if user_skills else []
        self._skill_read = re.compile(rf"^(?:{'|'.join(roots)})/([^/]+)/SKILL\.md$")
        self._fired: set[str] = set()

    def normalize(self, line: PiLine) -> list[Payload]:
        match line.type:
            case "message_end" if line.message is not None and line.message.role == "assistant":
                return self._assistant(line.message)
            case "tool_execution_start":
                return self._turn(self._call(line))
            case "tool_execution_end":
                output = _texts(line.result.content) if line.result else ""
                return [ToolResult(id=line.toolCallId, ok=not line.isError, output=output)]
            case "auto_retry_end" if not line.success:
                return [Error(message=line.finalError or "retries exhausted")]
            case "agent_settled":
                return [End(status="error", reason="aborted") if line.aborted else End(status="ok")]
        return []

    def _assistant(self, message: _Message) -> list[Payload]:
        payloads: list[Payload] = []
        if message.usage is not None:
            u = message.usage
            total_in = u.input + u.cacheRead + u.cacheWrite
            payloads.append(
                Usage(input_tokens=total_in, output_tokens=u.output, cost_usd=u.cost.total)
            )
        if message.stopReason == "error":
            payloads.append(Error(message=message.errorMessage or "the model call failed"))
        if text := _texts(message.content):
            payloads += self._turn([Message(role="assistant", text=text)])
        return payloads

    def _call(self, line: PiLine) -> list[Payload]:
        call = ToolCall(
            id=line.toolCallId,
            kind=PI_TOOL_KINDS.get(line.toolName, "other"),
            name=line.toolName,
            input=line.args,
        )
        path = line.args.get("path")
        found = (
            self._skill_read.match(path)
            if line.toolName == "read" and isinstance(path, str)
            else None
        )
        if found is None or found.group(1) in self._fired:
            return [call]
        self._fired.add(found.group(1))
        return [call, SkillActivated(skill=found.group(1), evidence="inferred")]

    def _turn(self, payloads: list[Payload]) -> list[Payload]:
        self.turns += 1
        if self.turns > self.max_turns:
            return [End(status="ok", reason=MAX_TURNS_REASON)]
        return payloads


class PiDriver:
    name = "pi"
    capabilities = Capabilities(
        skills="inferred",
        subagents="none",
        hooks="none",
        usage="usd",
        isolation="home",
        host_shell=False,
        tools=sorted(set(PI_TOOL_KINDS.values())),
        # Pi has no subagents and no hook config; native/pi extensions stand in for hooks.
        harness=["instructions", "skills", "mcp", "plugins"],
    )

    def __init__(self, home: Path, raw_dir: Path, executable: str = "pi") -> None:
        self.home = home
        self.raw_dir = raw_dir
        self.executable = executable
        self._child = ChildProcess("pi", raw_dir)

    async def version(self) -> str:
        process = await asyncio.create_subprocess_exec(
            self.executable, "--version", stdout=asyncio.subprocess.PIPE, env=self._env()
        )
        out, _ = await process.communicate()
        if process.returncode != 0:
            raise AgentError(f"{self.executable} --version exited with {process.returncode}")
        return out.decode().strip()

    def command(
        self, request: TrialRequest, skill_dirs: list[Path], harness_flags: Sequence[str] = ()
    ) -> list[str]:
        # -ne and -na keep the user's extensions and the workspace's .pi/ settings out.
        argv = [self.executable, "--mode", "json", "--no-session", "--no-skills", "-ne", "-na"]
        argv += harness_flags
        for skills in skill_dirs:
            argv += ["--skill", str(skills)]
        if request.model:
            provider, _, model = request.model.partition("/")
            # OpenRouter ids hold a slash of their own, so name the provider apart.
            argv += ["--provider", provider, "--model", model] if model else ["--model", provider]
        if request.tools == []:
            argv.append("--no-tools")
        elif request.tools:
            names = dict.fromkeys(_PI_TOOLS[k] for k in request.tools if k in _PI_TOOLS)
            argv += ["--tools", ",".join(names)]
        if request.append_system_prompt:
            argv += ["--append-system-prompt", request.append_system_prompt]
        return [*argv, pi_prompt(request.prompt)]

    async def run(self, request: TrialRequest) -> AsyncIterator[Payload]:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        copies = PluginCopies(self.home / PLUGINS_DIR)
        try:
            skill_dirs = [copies.add(Path(plugin)) / "skills" for plugin in request.plugins]
        except PluginRenderError as exc:
            yield Error(message=str(exc))
            yield End(status="error", reason="plugin copy failed")
            return
        env = self._env() | request.env
        # Pi's default agent dir, now inside the trial HOME.
        agent_dir = self.home / ".pi" / "agent"
        harness_flags: list[str] = []
        if request.harness:
            harness = PiHarness(HarnessLoader().load(Path(request.harness)))
            harness_flags = harness.materialize(agent_dir, env)
        stream = PiStream(request.limits.max_turns, copies.root, agent_dir / "skills")
        ended = False
        # Pi waits for stdin to close before it starts, since piped input joins the prompt.
        async for raw_line in self._child.lines(
            self.command(request, skill_dirs, harness_flags), cwd=request.workspace, env=env
        ):
            if ended:
                continue
            try:
                line = PiLine.model_validate_json(raw_line)
            except ValidationError:
                text = raw_line.decode(errors="replace").strip()[:200]
                log.warning("pi printed a non-event line: %s", text)
                continue
            for payload in stream.normalize(line):
                yield payload
                if isinstance(payload, End):
                    ended = True
                    if payload.reason == MAX_TURNS_REASON:
                        self._child.terminate(MAX_TURNS_REASON)
                    break
        code = self._child.returncode
        if not ended:
            yield Error(message=f"pi exited with code {code} before settling; see stderr.log")
            yield End(status="error", reason=f"pi exited with code {code}")

    async def cancel(self, reason: str) -> None:
        self._child.terminate(reason)

    def _env(self) -> dict[str, str]:
        return isolated_env(os.environ, self.home)
