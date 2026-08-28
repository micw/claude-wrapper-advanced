"""Unit tests for oversized CLI output lines — real subprocess, no CLI, no backend, no tokens.

Where this comes from: prod, 2026-08-26. Two requests died mid-stream with
`asyncio.exceptions.LimitOverrunError: Separator is found, but chunk is longer than limit`
-> `ERROR: Exception in ASGI application`, the client saw "peer closed connection without
sending complete message body". Cause: `asyncio.StreamReader` defaults to a 64 KiB buffer, and
we read the CLI's stream-json output line by line. Deltas are small, but the final
assistant/result event carries the whole answer on ONE line — roughly 15k output tokens of
JSON-escaped text is enough to blow the buffer. Large multi-file patches hit it every time.

Two layers, both needed:
  1. spawn: the reader must be created with a limit that fits a real answer (the actual bug —
     a fake stream can never catch it, because the limit lives in the real StreamReader).
  2. read_line: even beyond ANY limit the turn must end as an error EVENT, not as an exception
     escaping the async generator into uvicorn. Layer 2 alone would look green while the
     requests still fail.

Note on the exception type: StreamReader.readline() catches the LimitOverrunError itself and
re-raises it as ValueError — the LimitOverrunError in the log is only the chained cause. So
`except LimitOverrunError` would catch nothing.
"""
import asyncio
import logging
import sys
import unittest
from unittest import mock

from app import wire
from app.cli_driver import Overlong, Silent, read_line, spawn_cli, _oneshot_turn
from app.config import settings
from app.pool import Proc


def kinds(events):
    """Ereignistypen ohne 'started' — das eröffnet jeden Turn und sagt über den Ausgang nichts."""
    return [e.type for e in events if not isinstance(e, wire.Started)]


# 64 KiB is the asyncio default we are escaping; use a line comfortably above it.
BIG = 1 << 20   # 1 MiB


class TestSpawnStreamLimit(unittest.IsolatedAsyncioTestCase):
    """Layer 1 — the real StreamReader, spawned exactly the way the proxy spawns the CLI."""

    def setUp(self):
        lg = logging.getLogger("asyncio")           # subprocess transport chatter at INFO
        old = lg.level
        lg.setLevel(logging.WARNING)
        self.addCleanup(lg.setLevel, old)

    async def _first_line(self, nbytes):
        """Spawn a stand-in 'CLI' that prints one line of nbytes and read that line back."""
        script = f"import sys; sys.stdout.write('x'*{nbytes} + '\\n'); sys.stdout.flush()"
        old = settings.claude_bin
        settings.claude_bin = sys.executable
        try:
            proc = await spawn_cli(["-c", script])
        finally:
            settings.claude_bin = old
        try:
            return await asyncio.wait_for(proc.stdout.readline(), timeout=30)
        finally:
            proc.kill()
            await proc.wait()

    async def test_small_line_round_trips(self):
        """Sanity: the harness itself works below the default limit."""
        line = await self._first_line(1000)
        self.assertEqual(len(line), 1001)

    async def test_line_beyond_the_64k_default_is_read_whole(self):
        """The regression. Without an explicit limit= this raises ValueError."""
        line = await self._first_line(BIG)
        self.assertEqual(len(line), BIG + 1, "the oversized line must arrive intact")


class TestConfiguredLimit(unittest.TestCase):
    def test_limit_clears_a_realistic_worst_case(self):
        """A max-length answer is a few hundred KB on one line; keep an order of magnitude spare.

        The limit is a per-reader high-water mark, so the worst case is roughly
        stream_limit x POOL_MAX_PROCS of buffer — hence not arbitrarily large either.
        """
        self.assertGreaterEqual(settings.stream_limit, 4 << 20)
        self.assertLessEqual(settings.stream_limit * settings.pool_max_procs, 512 << 20)


class ScriptedStdout:
    """Yields scripted lines; an exception in the script is raised instead of returned."""

    def __init__(self, script):
        self.script = list(script)

    async def readline(self):
        if not self.script:
            await asyncio.sleep(3600)          # silent forever
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeProc:
    """Minimal stand-in for asyncio.subprocess.Process — stdin is a sink, stdout is scripted."""

    def __init__(self, script):
        self.returncode = None
        self.stdout = ScriptedStdout(script)
        self.stderr = _EmptyStream()
        self.stdin = _Sink()
        self.killed = False

    def kill(self):
        self.killed = True

    async def wait(self):
        self.returncode = -9
        return self.returncode


class _Sink:
    def write(self, data):
        pass

    async def drain(self):
        pass


class _EmptyStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _overrun():
    """The exact exception asyncio hands us — ValueError, LimitOverrunError as the cause."""
    cause = asyncio.LimitOverrunError("Separator is found, but chunk is longer than limit", 0)
    err = ValueError(cause.args[0])
    err.__cause__ = cause
    return err


RESULT = b'{"type":"result","result":"ok"}\n'


class TestReadLine(unittest.IsolatedAsyncioTestCase):
    """Layer 2a — read_line must not leak the raw ValueError to its callers."""

    async def _read(self, item):
        loop = asyncio.get_running_loop()
        return await read_line(ScriptedStdout([item]), loop.time() + 5, loop)

    async def test_overrun_becomes_overlong(self):
        with self.assertRaises(Overlong):
            await self._read(_overrun())

    async def test_overlong_is_not_mistaken_for_a_timeout(self):
        """Silence and an oversized line are different failures — and different messages."""
        with self.assertRaises(Overlong) as cm:
            await self._read(_overrun())
        self.assertNotIsInstance(cm.exception, asyncio.TimeoutError)
        self.assertNotIsInstance(cm.exception, Silent)

    async def test_normal_line_is_untouched(self):
        self.assertEqual(await self._read(RESULT), RESULT)


class TestTurnLoops(unittest.IsolatedAsyncioTestCase):
    """Layer 2b — the turn must END, as an error event. Nothing may escape the generator.

    This is what the client actually sees: an exception here becomes uvicorn's
    'Exception in ASGI application' and a truncated response body, with no error event and no
    outcome in the request log (prod showed exactly that: outcome=None tokens=None cost=None).
    """

    async def test_pooled_turn_yields_an_error_event(self):
        p = Proc("k", [])
        p.proc = FakeProc([RESULT, _overrun()])   # RESULT answers run_turn's /clear
        stats = {}
        with self.assertLogs("pool", "ERROR"):    # the operator must see WHY the turn died
            evs = [ev async for ev in p.run_turn("hi", stats)]
        self.assertEqual(kinds(evs), ["failed"])
        self.assertEqual(evs[-1].error_type, "overlong_line")
        self.assertEqual(stats.get("outcome"), "error")

    async def test_pooled_proc_is_discarded_afterwards(self):
        """readline() drops the buffer on overrun -> the stream is desynchronised. Never reuse."""
        p = Proc("k", [])
        p.proc = FakeProc([RESULT, _overrun()])
        with self.assertLogs("pool", "ERROR"):
            async for _ in p.run_turn("hi", {}):
                pass
        self.assertTrue(p.dead, "an overrun leaves the process unusable")

    async def test_overrun_during_the_clear_also_ends_the_turn(self):
        """run_turn reads /clear through _await_result, which has its own raw readline()."""
        p = Proc("k", [])
        p.proc = FakeProc([_overrun()])           # the overrun hits the /clear read
        stats = {}
        evs = [ev async for ev in p.run_turn("hi", stats)]
        self.assertEqual(kinds(evs), ["failed"])
        self.assertTrue(p.dead)
        self.assertEqual(stats.get("outcome"), "error")

    async def test_oneshot_turn_yields_an_error_event(self):
        fake = FakeProc([_overrun()])
        stats = {}
        # mock.patch turns an async def into an AsyncMock -> return_value is the awaited result.
        with mock.patch("app.cli_driver.spawn_cli", return_value=fake), \
                self.assertLogs("cli", "ERROR"):
            evs = [ev async for ev in _oneshot_turn("hi", [], "claude-opus-5", stats)]
        self.assertEqual(kinds(evs), ["failed"])
        self.assertEqual(evs[-1].error_type, "overlong_line")
        self.assertEqual(stats.get("outcome"), "error")
        self.assertTrue(fake.killed, "the one-shot process must be reaped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
