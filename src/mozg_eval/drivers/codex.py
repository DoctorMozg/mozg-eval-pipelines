"""Codex driver: runs `codex exec --json` in a temp CODEX_HOME with the case's plugins installed."""

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, JsonValue, ValidationError

from mozg_eval.drivers import process
from mozg_eval.drivers.codex_plugins import MARKETPLACE, CodexMarketplace, PluginRenderError
from mozg_eval.drivers.process import ChildProcess
from mozg_eval.drivers.protocol import Capabilities, TrialRequest
from mozg_eval.events.models import (
    End,
    Error,
    HookFired,
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

# Codex shows skills to the model as files; reading one from the installed plugin is the only
# trace of a skill being used.
_SKILL_READ = re.compile(
    rf"/plugins/cache/{MARKETPLACE}/[^/\s]+/[^/\s]+/skills/([^/\s'\"]+)/SKILL\.md"
)
# Codex repeats this notice on every run that passes the flag; it is not a failure.
_HOOK_TRUST_NOTICE = "--dangerously-bypass-hook-trust"
_KEEP_ENV = {"CODEX_API_KEY"}
MAX_TURNS_REASON = "max_turns reached"
_MAX_TURNS_END = End(status="ok", reason=MAX_TURNS_REASON)
_CONFIG = """\
project_doc_fallback_filenames = ["CLAUDE.md"]

[skills.bundled]
enabled = false
"""
_TOOL_ITEMS = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
    "collab_tool_call",
}
_SLASH = re.compile(r"^/([a-z0-9-]+):([a-z0-9-]+)(?:\s+|$)")


class _Loose(BaseModel):
    """Reads only the fields we map; raw.jsonl keeps the rest."""


class _Item(_Loose):
    id: str = ""
    type: str
    text: str = ""
    message: str = ""
    command: str = ""
    aggregated_output: str = ""
    exit_code: int | None = None
    status: str = ""
    changes: list[JsonValue] = []
    server: str = ""
    tool: str = ""
    arguments: JsonValue = None
    result: JsonValue = None
    error: JsonValue = None
    query: str = ""
    prompt: str | None = None


class _Usage(_Loose):
    input_tokens: int = 0
    output_tokens: int = 0


class _Failure(_Loose):
    message: str = ""


class CodexLine(_Loose):
    type: str
    item: _Item | None = None
    usage: _Usage | None = None
    message: str = ""
    error: _Failure | None = None


class _HookRecord(_Loose):
    event: str
    stdout: str = ""


def codex_prompt(prompt: str) -> str:
    """Codex has no plugin slash commands, so `/plugin:skill args` names the skill instead."""
    direct = _SLASH.match(prompt)
    if direct is None:
        return prompt
    plugin, skill = direct.group(1, 2)
    return (
        f"Use the ${skill} skill from {plugin}.\n"
        "Use the arguments below wherever the skill refers to arguments or the user's request.\n"
        f"<skill_arguments>\n{prompt[direct.end() :]}\n</skill_arguments>"
    )


class CodexStream:
    """Maps `codex exec --json` lines onto events; one per trial, since items span lines."""

    def __init__(self, max_turns: int) -> None:
        self.max_turns = max_turns
        self.messages = 0
        self.calls = 0
        self._started: set[str] = set()
        self._skills: set[str] = set()

    def normalize(self, line: CodexLine) -> list[Payload]:
        item = line.item
        match line.type, item:
            case "item.started", _Item():
                self._started.add(item.id)
                return self._begin(item)
            case "item.completed", _Item():
                return self._completed(item)
            case "turn.completed", _:
                usage = line.usage or _Usage()
                # input_tokens already counts the cached part; Codex reports no price.
                return [
                    Usage(
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cost_usd=None,
                    ),
                    End(status="ok"),
                ]
            case "turn.failed", _:
                reason = line.error.message if line.error else "turn failed"
                return [Error(message=reason), End(status="error", reason=reason)]
            case "error", _:
                return [Error(message=line.message)]
        return []

    def _completed(self, item: _Item) -> list[Payload]:
        match item.type:
            case "agent_message":
                self.messages += 1
                if self.messages > self.max_turns:
                    return [_MAX_TURNS_END]
                return [Message(role="assistant", text=item.text)]
            case "error":
                if _HOOK_TRUST_NOTICE in item.message:
                    return []
                return [Log(level="warning", text=item.message)]
        if item.type not in _TOOL_ITEMS:
            return []
        events = [] if item.id in self._started else self._begin(item)
        if _MAX_TURNS_END in events:
            return events
        events.append(ToolResult(id=item.id, ok=self._ok(item), output=self._output(item)))
        return events

    def _begin(self, item: _Item) -> list[Payload]:
        """Codex has no turn cap. A Claude turn ends at each tool call, so the calls count too."""
        if item.type not in _TOOL_ITEMS:
            return []
        self.calls += 1
        if self.calls > self.max_turns:
            return [_MAX_TURNS_END]
        return self._call(item)

    def _call(self, item: _Item) -> list[Payload]:
        kind: ToolKind | Literal["other"] = "other"
        input: dict[str, JsonValue]
        match item.type:
            case "command_execution":
                kind, name, input = "shell", "shell", {"command": item.command}
            case "file_change":
                kind, name, input = "edit", "apply_patch", {"changes": item.changes}
            case "mcp_tool_call":
                name, input = f"{item.server}.{item.tool}", {"arguments": item.arguments}
            case "web_search":
                name, input = "web_search", {"query": item.query}
            case "collab_tool_call":
                # spawn_agent names no agent type, so there is no subagent to report by name.
                kind = "subagent" if item.tool == "spawn_agent" else "other"
                name, input = item.tool, {"prompt": item.prompt}
            case _:
                return []
        call = ToolCall(id=item.id, kind=kind, name=name, input=input)
        events: list[Payload] = [call]
        for skill in _SKILL_READ.findall(item.command):
            if skill not in self._skills:
                self._skills.add(skill)
                events.append(SkillActivated(skill=skill, evidence="inferred"))
        return events

    @staticmethod
    def _ok(item: _Item) -> bool:
        if item.type == "command_execution":
            return item.exit_code == 0
        return item.status in ("completed", "")

    @staticmethod
    def _output(item: _Item) -> str:
        if item.type == "command_execution":
            return item.aggregated_output
        found = item.error if item.error is not None else item.result
        return "" if found is None else json.dumps(found)


class HookLog:
    """Reads the capture script's log as it grows, so hooks land near where they fired."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0

    def new_events(self) -> list[Payload]:
        if not self.path.exists():
            return []
        with self.path.open("rb") as log_file:
            log_file.seek(self._offset)
            data = log_file.read()
        complete = data[: data.rfind(b"\n") + 1]
        self._offset += len(complete)
        records = [_HookRecord.model_validate_json(line) for line in complete.splitlines()]
        return [HookFired(event=r.event, output=r.stdout) for r in records]


def isolated_env(base: Mapping[str, str], home: Path, real_home: Path) -> dict[str, str]:
    """The parent env minus Codex and Claude session state, with HOME and CODEX_HOME in `home`."""
    env = {
        key: value
        for key, value in base.items()
        if not key.startswith(("CODEX_", "CLAUDE")) or key in _KEEP_ENV
    }
    path = base.get("PATH", "").split(os.pathsep)
    env["PATH"] = os.pathsep.join(p for p in path if not Path(p).is_relative_to(real_home))
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(home / ".codex")
    return env


class CodexDriver:
    name = "codex"
    # Reads of files go through the shell, and spawn_agent names no agent.
    capabilities = Capabilities(
        skills="inferred",
        subagents="none",
        hooks="native",
        usage="tokens",
        isolation="home",
        tools=["shell", "edit", "subagent"],
        tool_input=["shell"],
    )

    def __init__(self, home: Path, raw_dir: Path, executable: str = "codex") -> None:
        self.home = home
        self.raw_dir = raw_dir
        self.executable = executable
        self._child = ChildProcess("codex", raw_dir)

    async def version(self) -> str:
        process = await asyncio.create_subprocess_exec(
            self.executable, "--version", stdout=asyncio.subprocess.PIPE
        )
        out, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"{self.executable} --version exited with {process.returncode}")
        return out.decode().strip()

    def command(self, request: TrialRequest) -> list[str]:
        argv = [
            self.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--dangerously-bypass-hook-trust",
            "--sandbox=workspace-write",
            f"--cd={request.workspace}",
        ]
        if request.model:
            argv.append(f"--model={request.model}")
        if request.append_system_prompt:
            argv += ["-c", f"developer_instructions={json.dumps(request.append_system_prompt)}"]
        argv.append("-")
        return argv

    async def run(self, request: TrialRequest) -> AsyncIterator[Payload]:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        env = self._prepare_home() | request.env
        env["MZEVAL_HOOK_LOG"] = str(self.raw_dir / "hooks.jsonl")
        if request.tools:
            tools = ", ".join(request.tools)
            yield Log(level="info", text=f"codex offers its own tools; [{tools}] is not enforced")
        try:
            await self._install(request.plugins, env)
        except PluginRenderError as exc:
            yield Error(message=str(exc))
            yield End(status="error", reason="plugin install failed")
            return
        stream = CodexStream(request.limits.max_turns)
        hooks = HookLog(self.raw_dir / "hooks.jsonl")
        ended = False
        exit_timer: asyncio.TimerHandle | None = None
        prompt = codex_prompt(request.prompt).encode()
        async for raw_line in self._child.lines(
            self.command(request), cwd=request.workspace, env=env, stdin=prompt
        ):
            for payload in hooks.new_events():
                yield payload
            if ended:
                continue
            try:
                line = CodexLine.model_validate_json(raw_line)
            except ValidationError:
                text = raw_line.decode(errors="replace").strip()[:200]
                yield Log(level="warning", text=f"codex printed a non-event line: {text}")
                continue
            for payload in stream.normalize(line):
                yield payload
                if isinstance(payload, End):
                    ended = True
                    if payload.reason == MAX_TURNS_REASON:
                        self._child.terminate(MAX_TURNS_REASON)
                    else:
                        # Stop and SessionEnd hooks run after the turn; give them time to finish.
                        exit_timer = asyncio.get_running_loop().call_later(
                            process.CANCEL_GRACE_S, self._child.terminate, "no exit after its end"
                        )
                    break
        if exit_timer is not None:
            exit_timer.cancel()
        for payload in hooks.new_events():
            yield payload
        code = self._child.returncode
        if code is not None and code > 0 and not ended:
            yield Error(message=f"codex exited with code {code}; see stderr.log")

    async def cancel(self, reason: str) -> None:
        self._child.terminate(reason)

    def _prepare_home(self) -> dict[str, str]:
        real_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        codex_home = self.home / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "config.toml").write_text(_CONFIG)
        auth = real_home / "auth.json"
        # A link, not a copy: a token refresh inside the trial must not strand the real login.
        if auth.exists() and not (codex_home / auth.name).exists():
            (codex_home / auth.name).symlink_to(auth)
        return isolated_env(os.environ, self.home, real_home)

    async def _install(self, plugins: list[str], env: dict[str, str]) -> None:
        if not plugins:
            return
        market = CodexMarketplace(self.home / ".mzeval-market", self.home / ".codex")
        for plugin in plugins:
            market.render(Path(plugin))
        market.write_index()
        with (self.raw_dir / "stderr.log").open("ab") as stderr:
            for argv in market.install_commands(self.executable):
                process = await asyncio.create_subprocess_exec(
                    *argv, stdout=stderr, stderr=stderr, env=env, cwd=self.home
                )
                if await process.wait() != 0:
                    raise PluginRenderError(
                        f"{' '.join(argv[1:])} exited with {process.returncode}"
                    )
        log.info("installed %d plugins into %s", len(market.plugins), self.home / ".codex")
