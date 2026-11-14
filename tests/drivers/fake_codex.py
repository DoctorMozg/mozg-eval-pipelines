"""Stands in for `codex`: records how it was started, then replays a `codex exec --json` fixture.

`plugin ...` calls are appended to FAKE_RECORD.plugins and exit with FAKE_PLUGIN_EXIT.
FAKE_MODE: replay (default); hang ignores SIGTERM; linger replays, then waits to be stopped;
late_hook replays, then logs a SessionEnd hook a moment later, as Codex does on exit.
FAKE_HOOK names a hook event to log to MZEVAL_HOOK_LOG before replaying, as the capture would.
"""

import json
import os
import signal
import sys
import time


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print("codex-cli 0.167.2")
        return 0
    if sys.argv[1] == "plugin":
        with open(os.environ["FAKE_RECORD"] + ".plugins", "a") as out:
            out.write(json.dumps(sys.argv[1:]) + "\n")
        return int(os.environ.get("FAKE_PLUGIN_EXIT", "0"))
    record = {
        "argv": sys.argv[1:],
        "env": dict(os.environ),
        "stdin": sys.stdin.read(),
        "cwd": os.getcwd(),
    }
    with open(os.environ["FAKE_RECORD"], "w") as out:
        json.dump(record, out)
    if hook := os.environ.get("FAKE_HOOK"):
        with open(os.environ["MZEVAL_HOOK_LOG"], "a") as log:
            log.write(
                json.dumps({"plugin": "p", "event": hook, "key": "k", "stdout": "ctx"}) + "\n"
            )
    mode = os.environ.get("FAKE_MODE", "replay")
    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    with open(os.environ["FAKE_FIXTURE"]) as fixture:
        sys.stdout.write(fixture.read())
    sys.stdout.flush()
    if mode == "linger":
        time.sleep(60)
    if mode == "late_hook":
        time.sleep(0.3)
        log_hook("SessionEnd")
    return 0


def log_hook(event: str) -> None:
    with open(os.environ["MZEVAL_HOOK_LOG"], "a") as log:
        log.write(json.dumps({"plugin": "p", "event": event, "key": "k", "stdout": "ctx"}) + "\n")


sys.exit(main())
