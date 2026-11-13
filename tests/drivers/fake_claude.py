"""Stands in for `claude`: records how it was started, then replays a stream-json fixture.

FAKE_MODE: replay (default) prints FAKE_FIXTURE; hang ignores SIGTERM and never finishes.
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
    with open(os.environ["FAKE_FIXTURE"]) as fixture:
        sys.stdout.write(fixture.read())


main()
