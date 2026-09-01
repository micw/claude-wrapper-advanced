"""Fail-open side channel from the CLI's Bun fetch to the wrapper.

The preload writes only request model ids and `anthropic-ratelimit-unified-*`
response headers to a dedicated inherited pipe. No credentials or bodies cross it.
"""
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("turn_headers")
PRELOAD = str(Path(__file__).with_name("turn_headers_preload.cjs"))
MAX_LINE = 64 << 10


def _bun_options(existing):
    option = f"--preload={PRELOAD}"
    return f"{existing} {option}".strip() if existing else option


class Capture:
    """One capture pipe for one CLI process."""

    def __init__(self):
        self.read_fd, self.write_fd = os.pipe()
        self._pipe = None
        self._transport = None
        self._task = None
        self._pending = {}
        self._updates = asyncio.Queue()
        self.ready = False

    def configure(self, env):
        env = dict(env)
        env["BUN_OPTIONS"] = _bun_options(env.get("BUN_OPTIONS"))
        env["CLAUDE_TURN_HEADERS_FD"] = str(self.write_fd)
        return env

    @property
    def pass_fds(self):
        return (self.write_fd,)

    async def parent_started(self):
        os.close(self.write_fd)
        self.write_fd = -1
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=MAX_LINE)
        protocol = asyncio.StreamReaderProtocol(reader)
        self._pipe = os.fdopen(self.read_fd, "rb", buffering=0)
        self.read_fd = -1
        self._transport, _ = await loop.connect_read_pipe(lambda: protocol, self._pipe)
        self._task = asyncio.create_task(self._read(reader))

    async def _read(self, reader):
        try:
            while True:
                try:
                    raw = await reader.readline()
                except ValueError:
                    log.warning("turn-header capture emitted an oversized line; capture disabled")
                    return
                if not raw:
                    return
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if event.get("capture") != "claude-turn-headers-v1":
                    continue
                kind, cid = event.get("kind"), event.get("id")
                if kind == "preload_ready":
                    self.ready = True
                elif kind == "request" and isinstance(cid, int):
                    self._pending[cid] = event.get("model")
                elif kind == "fetch_error" and isinstance(cid, int):
                    self._pending.pop(cid, None)
                elif kind == "response" and isinstance(cid, int):
                    model = self._pending.pop(cid, None)
                    headers = event.get("headers") or {}
                    if not isinstance(headers, dict):
                        continue
                    # Lazy import avoids cli_driver -> capture -> limits -> cli_driver.
                    from . import limits
                    snapshot = limits.observe_turn_headers(headers, model)
                    if snapshot is not None:
                        self._updates.put_nowait(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # capture is observability, never turn correctness
            log.warning("turn-header capture stopped: %s", err)

    async def latest_update(self):
        """Coalesce all updates currently available into one full snapshot."""
        await asyncio.sleep(0)  # let a pipe callback that is already ready run first
        latest = None
        while True:
            try:
                latest = self._updates.get_nowait()
            except asyncio.QueueEmpty:
                return latest

    async def close(self):
        if self.write_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.write_fd)
            self.write_fd = -1
        if self.read_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.read_fd)
            self.read_fd = -1
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._transport:
            self._transport.close()
        if self._pipe:
            with contextlib.suppress(OSError):
                self._pipe.close()

    def abort_spawn(self):
        for name in ("write_fd", "read_fd"):
            fd = getattr(self, name)
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
                setattr(self, name, -1)


async def quota_event(proc):
    capture = getattr(proc, "turn_header_capture", None)
    if capture is None:
        return None
    snapshot = await capture.latest_update()
    if snapshot is None:
        return None
    from .wire import Quota
    return Quota(usage=snapshot)
