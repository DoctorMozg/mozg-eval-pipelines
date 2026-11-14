import asyncio
import os
import time
from pathlib import Path

from mozg_eval.drivers.process import ChildProcess


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_soon(pid: int) -> bool:
    deadline = time.monotonic() + 3
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not alive(pid)


class TestChildProcess:
    def test_terminate_reaches_processes_the_child_started(self, tmp_path: Path) -> None:
        child = ChildProcess("sh", tmp_path)

        async def go() -> int:
            async for line in child.lines(
                ["sh", "-c", "sleep 60 & echo $!; wait"], cwd=str(tmp_path)
            ):
                grandchild = int(line)
                child.terminate("test")
            return grandchild

        grandchild = asyncio.run(go())

        assert gone_soon(grandchild)

    def test_reader_that_stops_early_leaves_nothing_running(self, tmp_path: Path) -> None:
        child = ChildProcess("sh", tmp_path)

        async def go() -> int:
            lines = child.lines(["sh", "-c", "sleep 60 & echo $!; wait"], cwd=str(tmp_path))
            grandchild = int(await anext(lines))
            await lines.aclose()
            return grandchild

        grandchild = asyncio.run(go())

        assert gone_soon(grandchild)
        assert (tmp_path / "raw.jsonl").read_text().strip() == str(grandchild)
