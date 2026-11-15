"""Stands in for `claude`: records how it was started, then replays a stream-json fixture.

FAKE_MODE: replay (default) prints FAKE_FIXTURE; hang ignores SIGTERM and never finishes.
FAKE_DELAY_S waits before the replay; FAKE_SPANS gets a "start end" line per run.
FAKE_EXIT is the exit code after the replay.
"""

import json
import os
import signal
import sys
import time


def main() -> None:
    if sys.argv[1:] == ["--version"]:
        print("2.1.325 (Claude Code)")
        return
    record = {
        "argv": sys.argv[1:],
        "env": dict(os.environ),
        "stdin": sys.stdin.read(),
        "files": sorted(os.listdir(".")),
    }
    with open(os.environ["FAKE_RECORD"], "w") as out:
        json.dump(record, out)
    if os.environ.get("FAKE_MODE") == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    started = time.time()
    time.sleep(float(os.environ.get("FAKE_DELAY_S", "0")))
    with open(os.environ["FAKE_FIXTURE"]) as fixture:
        sys.stdout.write(fixture.read())
    if spans := os.environ.get("FAKE_SPANS"):
        with open(spans, "a") as out:
            out.write(f"{started} {time.time()}\n")
    sys.exit(int(os.environ.get("FAKE_EXIT", "0")))


main()
