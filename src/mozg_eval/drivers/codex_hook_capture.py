"""Runs one plugin hook for Codex and logs it, since `codex exec --json` records no hook events.

Usage: codex_hook_capture.py <owner> <event> <key> [commands.json]. Without the commands file
it is read from the plugin under PLUGIN_ROOT, which Codex sets for plugin hooks only. The driver
sets MZEVAL_HOOK_LOG. The hook's stdout, stderr and exit code pass through unchanged.
"""

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    plugin, event, key = sys.argv[1:4]
    if len(sys.argv) > 4:
        commands = Path(sys.argv[4])
    else:
        commands = Path(os.environ["PLUGIN_ROOT"]) / "hooks" / ".mzeval-hook-commands.json"
    command = json.loads(commands.read_text())[key]
    stdin = sys.stdin.read()
    result = subprocess.run(command, shell=True, input=stdin, capture_output=True, text=True)
    record = {
        "plugin": plugin,
        "event": event,
        "key": key,
        "stdin": stdin,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }
    with open(os.environ["MZEVAL_HOOK_LOG"], "a", encoding="utf-8") as log:
        fcntl.flock(log, fcntl.LOCK_EX)  # hooks of one event may run in parallel
        log.write(json.dumps(record) + "\n")
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
