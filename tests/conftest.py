import json
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel


def _wrap(script: str, executable: Path) -> Path:
    source = Path(__file__).parent / "drivers" / script
    executable.write_text(f'#!/bin/sh\nexec {sys.executable} {source} "$@"\n')
    executable.chmod(0o755)
    return executable


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    """An executable `claude` that replays a recorded stream; see drivers/fake_claude.py."""
    return _wrap("fake_claude.py", tmp_path / "claude")


@pytest.fixture
def fake_codex(tmp_path: Path) -> Path:
    """An executable `codex` that replays a recorded stream; see drivers/fake_codex.py."""
    return _wrap("fake_codex.py", tmp_path / "codex")


@pytest.fixture
def fake_agent(tmp_path: Path) -> Path:
    """An executable agent CLI that replays a recorded stream; see drivers/fake_agent.py."""
    return _wrap("fake_agent.py", tmp_path / "agent")


@pytest.fixture
def claude_plugin(tmp_path: Path) -> Path:
    """A Claude plugin with one skill that names its own root and three kinds of hook."""
    root = tmp_path / "src" / "notes"
    (root / ".claude-plugin").mkdir(parents=True)
    manifest = {
        "name": "notes",
        "version": "1.2.0",
        "description": "Notes",
        "author": {"name": "x"},
    }
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    (root / "skills" / "note").mkdir(parents=True)
    (root / "skills" / "note" / "SKILL.md").write_text(
        "---\nname: note\n---\nRun ${CLAUDE_PLUGIN_ROOT}/scripts/save.sh\n"
    )
    (root / "hooks").mkdir()
    hooks = {
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo started"}]}],
            "PostToolUse": [
                {
                    "matcher": "Write|Edit|Bash",
                    "hooks": [
                        {"type": "prompt", "prompt": "check it"},
                        {"type": "command", "command": "exit 3"},
                    ],
                }
            ],
        }
    }
    (root / "hooks" / "hooks.json").write_text(json.dumps(hooks))
    (root / ".git").mkdir()
    return root


@pytest.fixture
def quoting_judge() -> Callable[..., FunctionModel]:
    """A judge model giving the next of its verdicts per criterion; it quotes the evidence's end."""

    def make(*verdicts: str, model_name: str | None = None) -> FunctionModel:
        given: list[str] = []

        def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            first = messages[0]
            assert isinstance(first, ModelRequest)
            prompt = next(p.content for p in first.parts if isinstance(p, UserPromptPart))
            assert isinstance(prompt, str)
            if len(messages) == 1:
                given.append(verdicts[min(len(given), len(verdicts) - 1)])
            evidence = prompt.partition("<evidence>\n")[2].rpartition("\n</evidence>")[0]
            lines = [line for line in evidence.splitlines() if line.strip()]
            quote = lines[-1][-40:] if lines else ""
            args = {"reasoning": "read it", "verdict": given[-1], "evidence_quote": quote}
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

        return FunctionModel(answer, model_name=model_name)

    return make


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    auth: str | None
    body: bytes


@dataclass
class IngestStub:
    """Answers every request with 200, or 500 for paths under `fail`, and records them."""

    url: str = ""
    fail: str | None = None
    requests: list[Received] = field(default_factory=list)


@pytest.fixture
def ingest_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[IngestStub]:
    stub = IngestStub()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            auth = self.headers.get("Authorization")
            stub.requests.append(Received(self.command, self.path, auth, body))
            failing = stub.fail is not None and self.path.startswith(stub.fail)
            self.send_response(500 if failing else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_PUT = do_POST = _answer

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stub.url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("MZEVAL_SERVER", stub.url)
    monkeypatch.setenv("MZEVAL_TOKEN", "secret-token")
    yield stub
    server.shutdown()
    server.server_close()
