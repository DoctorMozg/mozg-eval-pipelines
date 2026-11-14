"""A driver's child process: its own process group, stdout read by line and teed to raw.jsonl."""

import asyncio
import logging
import os
import signal
from collections.abc import AsyncGenerator, Mapping, Sequence
from pathlib import Path

log = logging.getLogger(__name__)

# How long a driver has to exit after a cancel before it is killed.
CANCEL_GRACE_S = 5.0
# A line can hold a whole skill body or tool output; asyncio's default limit is 64 KiB.
LINE_LIMIT = 16 << 20


class ChildProcess:
    """Agents start shells, hooks and MCP servers; a cancel must reach all of them, not one pid."""

    def __init__(self, label: str, raw_dir: Path) -> None:
        self.label = label
        self.raw_dir = raw_dir
        self.returncode: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._kill_timer: asyncio.TimerHandle | None = None

    async def lines(
        self,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str] | None = None,
        stdin: bytes = b"",
        keep_stdin: bool = False,
    ) -> AsyncGenerator[bytes]:
        """Yield each non-blank stdout line; stdin is closed after `stdin` unless `keep_stdin`."""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        with (
            (self.raw_dir / "stderr.log").open("ab") as stderr,
            (self.raw_dir / "raw.jsonl").open("ab") as raw,
        ):
            process = self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                cwd=cwd,
                env=env,
                limit=LINE_LIMIT,
                start_new_session=True,
            )
            assert process.stdout is not None
            try:
                await self.send(stdin)
                if not keep_stdin:
                    assert process.stdin is not None
                    process.stdin.close()
                # Read to EOF even after a cancel, so a chatty child never blocks on a full pipe.
                async for line in process.stdout:
                    raw.write(line)
                    if line.strip():
                        yield line
                self.returncode = await process.wait()
            finally:
                if self._kill_timer is not None:
                    self._kill_timer.cancel()
                if process.returncode is None:
                    # The reader stopped early or was cancelled: nothing else will reap the group.
                    self._signal_group(signal.SIGKILL)
                    await process.wait()
        log.debug("%s exited with %s", self.label, self.returncode)

    async def send(self, data: bytes) -> None:
        assert self._process is not None
        assert self._process.stdin is not None
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def cancelling(self) -> bool:
        return self._kill_timer is not None

    def terminate(self, reason: str) -> None:
        """SIGTERM the whole group now, SIGKILL it after the grace period."""
        if not self.running() or self.cancelling:
            return
        log.info("cancelling %s: %s", self.label, reason)
        self._signal_group(signal.SIGTERM)
        self.kill_later()

    def kill_later(self) -> None:
        loop = asyncio.get_running_loop()
        self._kill_timer = loop.call_later(CANCEL_GRACE_S, self._kill)

    def _kill(self) -> None:
        if self.running():
            log.warning("%s still running %ss after cancel; killing it", self.label, CANCEL_GRACE_S)
            self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig: signal.Signals) -> None:
        assert self._process is not None
        try:
            os.killpg(self._process.pid, sig)
        except ProcessLookupError:
            log.debug("%s group already gone before %s", self.label, sig.name)
