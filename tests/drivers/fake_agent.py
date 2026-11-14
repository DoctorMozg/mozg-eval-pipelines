"""Stands in for an agent CLI (opencode): records how it was started, then replays a fixture.

`--version` prints FAKE_VERSION. FAKE_EXIT sets the exit code after replaying.
FAKE_MODE: replay (default); hang ignores SIGTERM and never writes.
"""

import json
import os
import signal
import sys
import time


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print(os.environ.get("FAKE_VERSION", "fake 0.0.0"))
        return 0
    record = {
        "argv": sys.argv[1:],
        "env": dict(os.environ),
        "stdin": sys.stdin.read(),
        "cwd": os.getcwd(),
    }
    with open(os.environ["FAKE_RECORD"], "w") as out:
        json.dump(record, out)
    if os.environ.get("FAKE_MODE") == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    with open(os.environ["FAKE_FIXTURE"]) as fixture:
        sys.stdout.write(fixture.read())
    sys.stdout.flush()
    return int(os.environ.get("FAKE_EXIT", "0"))


sys.exit(main())
