"""A stdio driver for tests: each step is one assistant turn, one shell call and $0.10 of usage.

Modes (argv[1]): ok, slow (1 s a step), garbage (an invalid line first), stubborn (ignores cancel).
"""

import json
import sys
import threading


def emit(event: dict[str, object]) -> None:
    print(json.dumps(event), flush=True)


def main() -> None:
    if sys.argv[1] == "--version":
        print("fake 1.0")
        return
    mode = sys.argv[1]
    request = json.loads(sys.stdin.readline())
    cancelled = threading.Event()

    def watch_stdin() -> None:
        for line in sys.stdin:
            if json.loads(line)["type"] == "cancel" and mode != "stubborn":
                cancelled.set()

    threading.Thread(target=watch_stdin, daemon=True).start()
    emit({"type": "message", "role": "user", "text": request["prompt"]})
    if mode == "garbage":
        print('{"type": "teleport"}', flush=True)
    steps = 10**6 if mode == "stubborn" else 5
    for step in range(steps):
        if cancelled.wait(1.0 if mode == "slow" else 0.02):
            emit({"type": "end", "status": "cancelled", "reason": "asked to stop"})
            return
        emit({"type": "message", "role": "assistant", "text": f"step {step}"})
        emit({"type": "tool_call", "id": f"t{step}", "kind": "shell", "name": "Bash"})
        emit({"type": "usage", "input_tokens": 100, "output_tokens": 10, "cost_usd": 0.1})
    emit({"type": "end", "status": "ok"})


main()
