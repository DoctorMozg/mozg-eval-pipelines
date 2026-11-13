import sys
from pathlib import Path

import pytest


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    """An executable `claude` that replays a recorded stream; see drivers/fake_claude.py."""
    script = Path(__file__).parent / "drivers" / "fake_claude.py"
    executable = tmp_path / "claude"
    executable.write_text(f'#!/bin/sh\nexec {sys.executable} {script} "$@"\n')
    executable.chmod(0o755)
    return executable
