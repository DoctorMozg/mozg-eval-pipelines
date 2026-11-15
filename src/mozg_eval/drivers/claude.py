"""Claude Code driver: runs `claude -p` in a temp HOME and maps its stream-json onto events."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from pydantic import BaseModel, JsonValue, ValidationError

from mozg_eval.drivers.process import ChildProcess
from mozg_eval.drivers.protocol import AgentError, Capabilities, TrialRequest
from mozg_eval.events.models import (
    End,
    Error,
    HookFired,
    Log,
    Message,
    Payload,
    SkillActivated,
    SubagentSpawned,
    ToolCall,
    ToolResult,
    Usage,
    bare_name,
)
from mozg_eval.spec.models import ToolKind

log = logging.getLogger(__name__)

CLAUDE_TOOLS: dict[ToolKind, str] = {
    "skill": "Skill",
    "subagent": "Agent",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "shell": "Bash",
    "ask_user": "AskUserQuestion",
}
CLAUDE_TOOL_KINDS: dict[str, ToolKind] = {name: kind for kind, name in CLAUDE_TOOLS.items()}

# Auth variables a run may legitimately pass through; every other CLAUDE* variable is the
# parent session's state (effort, subagent model, session ids) and would leak into the trial.
_KEEP_ENV = {"CLAUDE_CODE_OAUTH_TOKEN"}


class _Loose(BaseModel):
    """Reads only the fields we map; Claude adds stream fields freely, raw.jsonl keeps the rest."""


class _Block(_Loose):
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, JsonValue] = {}
    tool_use_id: str = ""
    content: str | list[dict[str, JsonValue]] = ""
    is_error: bool = False


class _Message(_Loose):
    content: str | list[_Block] = ""


class _Usage(_Loose):
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0


class StreamLine(_Loose):
    type: str
    subtype: str | None = None
    parent_tool_use_id: str | None = None
    message: _Message | None = None
    claude_code_version: str = ""
    model: str = ""
    tools: list[str] = []
    hook_event: str = ""
    stdout: str = ""
    is_error: bool = False
    usage: _Usage | None = None
    total_cost_usd: float | None = None


def _text(content: str | list[dict[str, JsonValue]]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(str(block.get("text", "")) for block in content if block.get("type") == "text")


def _tool_call(block: _Block) -> list[Payload]:
    events: list[Payload] = [
        ToolCall(
            id=block.id,
            kind=CLAUDE_TOOL_KINDS.get(block.name, "other"),
            name=block.name,
            input=block.input,
        )
    ]
    target = block.input.get("skill" if block.name == "Skill" else "subagent_type")
    if block.name == "Skill" and isinstance(target, str):
        events.append(SkillActivated(skill=bare_name(target)))
    elif block.name == "Agent" and isinstance(target, str):
        events.append(SubagentSpawned(subagent=bare_name(target)))
    return events


def normalize(line: StreamLine) -> list[Payload]:
    """One stream-json line -> canonical events; lines with nothing to grade map to none."""
    blocks = (
        []
        if line.message is None or isinstance(line.message.content, str)
        else line.message.content
    )
    match line.type, line.subtype:
        case "system", "init":
            tools = ", ".join(line.tools)
            text = f"claude {line.claude_code_version}, model {line.model}, tools [{tools}]"
            return [Log(level="info", text=text)]
        case "system", "hook_response":
            return [HookFired(event=line.hook_event, output=line.stdout)]
        case "assistant", _:
            events: list[Payload] = []
            for block in blocks:
                if block.type == "text" and block.text:
                    parent = line.parent_tool_use_id
                    events.append(Message(role="assistant", text=block.text, parent=parent))
                elif block.type == "tool_use":
                    events.extend(_tool_call(block))
            return events
        case "user", _:
            return [
                ToolResult(id=block.tool_use_id, ok=not block.is_error, output=_text(block.content))
                for block in blocks
                if block.type == "tool_result"
            ]
        case "result", subtype:
            usage = line.usage or _Usage()
            total_in = (
                usage.input_tokens
                + usage.cache_creation_input_tokens
                + usage.cache_read_input_tokens
            )
            if subtype == "error_max_turns":
                # The case's own turn cap: a normal stop that graders still grade.
                end = End(status="ok", reason="max_turns reached")
            elif subtype == "success" and not line.is_error:
                end = End(status="ok")
            else:
                end = End(status="error", reason=str(subtype))
            return [
                Usage(
                    input_tokens=total_in,
                    output_tokens=usage.output_tokens,
                    cost_usd=line.total_cost_usd,
                ),
                end,
            ]
    return []


def isolated_env(base: Mapping[str, str], home: Path, real_config: Path) -> dict[str, str]:
    """The parent env minus Claude session state and plugin bin dirs from the real config dir."""
    env = {
        key: value
        for key, value in base.items()
        if not key.startswith("CLAUDE") or key in _KEEP_ENV
    }
    path = base.get("PATH", "").split(os.pathsep)
    env["PATH"] = os.pathsep.join(p for p in path if not Path(p).is_relative_to(real_config))
    env["HOME"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    return env


class ClaudeDriver:
    name = "claude"
    capabilities = Capabilities(
        skills="native",
        subagents="native",
        hooks="native",
        usage="usd",
        isolation="home",
        tools=list(CLAUDE_TOOLS),
        tool_input=list(CLAUDE_TOOLS),
    )

    def __init__(self, home: Path, raw_dir: Path, executable: str = "claude") -> None:
        self.home = home
        self.executable = executable
        self._child = ChildProcess("claude", raw_dir)

    async def version(self) -> str:
        process = await asyncio.create_subprocess_exec(
            self.executable, "--version", stdout=asyncio.subprocess.PIPE
        )
        out, _ = await process.communicate()
        if process.returncode != 0:
            raise AgentError(f"{self.executable} --version exited with {process.returncode}")
        return out.decode().strip()

    def command(self, request: TrialRequest) -> list[str]:
        argv = [
            self.executable,
            "-p",
            "--output-format=stream-json",
            "--verbose",
            "--include-hook-events",
            "--no-session-persistence",
            "--permission-mode=bypassPermissions",
            f"--max-turns={request.limits.max_turns}",
        ]
        if request.tools is not None:
            # The available set, not an approval list: extra tools change which skill fires.
            argv.append("--tools=" + ",".join(CLAUDE_TOOLS[kind] for kind in request.tools))
        argv += [f"--plugin-dir={plugin}" for plugin in request.plugins]
        if request.model:
            argv.append(f"--model={request.model}")
        if request.append_system_prompt:
            argv.append(f"--append-system-prompt={request.append_system_prompt}")
        return argv

    def _prepare_home(self) -> dict[str, str]:
        real_config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        config = self.home / ".claude"
        config.mkdir(parents=True, exist_ok=True)
        credentials = real_config / ".credentials.json"
        # A link, not a copy: a token refresh inside the trial must not strand the real login.
        if credentials.exists() and not (config / credentials.name).exists():
            (config / credentials.name).symlink_to(credentials)
        return isolated_env(os.environ, self.home, real_config)

    async def run(self, request: TrialRequest) -> AsyncIterator[Payload]:
        env = self._prepare_home() | request.env
        # On stdin, so no prompt text can be mistaken for an option value.
        lines = self._child.lines(
            self.command(request), cwd=request.workspace, env=env, stdin=request.prompt.encode()
        )
        async for raw_line in lines:
            try:
                line = StreamLine.model_validate_json(raw_line)
            except ValidationError:
                text = raw_line.decode(errors="replace").strip()[:200]
                yield Log(level="warning", text=f"claude printed a non-event line: {text}")
                continue
            for payload in normalize(line):
                yield payload
        code = self._child.returncode
        if code is not None and code > 0:
            yield Error(message=f"claude exited with code {code}; see stderr.log")

    async def cancel(self, reason: str) -> None:
        self._child.terminate(reason)
