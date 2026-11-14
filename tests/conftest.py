import json
import sys
from pathlib import Path

import pytest


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
