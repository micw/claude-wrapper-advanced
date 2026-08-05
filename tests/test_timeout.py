"""Unit tests for the stream read timeout — fake stream, no CLI, no backend.

Where this comes from: the old code had ONE total deadline (180s) and no idle limit, so the
binding constraint was wall-clock — a request still streaming got killed anyway (measured in
prod: first text token at 164.9s, killed at 180s). Now the hard cap is a far backstop and the
idle window is the real limit: silence kills, work does not.
"""
import asyncio
import unittest

from app.cli_driver import Silent, read_line
from app.config import settings


class FakeStream:
    """Yields (delay, line) pairs — delay is the silence BEFORE that line arrives."""

    def __init__(self, script):
        self.script = list(script)
        self.reads = 0

    async def readline(self):
        if not self.script:
            await asyncio.sleep(3600)          # silent forever
        delay, line = self.script.pop(0)
        await asyncio.sleep(delay)
        self.reads += 1
        return line


def drive(script, idle, hard, budget=5.0):
    """Read until the stream raises or is exhausted. Returns (lines, exception|None)."""
    async def run():
        loop = asyncio.get_running_loop()
        deadline = loop.time() + hard
        stream = FakeStream(script)
        lines = []
        try:
            while len(lines) < len(script):
                lines.append(await read_line(stream, deadline, loop))
            return lines, None
        except asyncio.TimeoutError as e:
            return lines, e
    old_idle, old_total = settings.idle_timeout, settings.request_timeout
    settings.idle_timeout = idle
    try:
        return asyncio.run(asyncio.wait_for(run(), timeout=budget))
    finally:
        settings.idle_timeout, settings.request_timeout = old_idle, old_total


class TestIdleTimeout(unittest.TestCase):
    def test_line_within_idle_window_is_returned(self):
        lines, err = drive([(0.02, b"a\n")], idle=0.2, hard=5)
        self.assertIsNone(err)
        self.assertEqual(lines, [b"a\n"])

    def test_silence_beyond_idle_raises_silent(self):
        lines, err = drive([(0.4, b"never\n")], idle=0.1, hard=5)
        self.assertIsInstance(err, Silent)
        self.assertEqual(lines, [])

    def test_steady_stream_outlives_the_idle_window(self):
        """Each read gets a FRESH window — the idle limit is not a cumulative budget.

        6 lines x 0.05s = 0.3s total, three times a 0.1s idle window. Getting this wrong
        (one budget for the whole turn) reintroduces exactly the failure we came from:
        a working stream cut off while it is producing.
        """
        script = [(0.05, b"tick\n")] * 6
        lines, err = drive(script, idle=0.1, hard=5)
        self.assertIsNone(err)
        self.assertEqual(len(lines), 6)

    def test_hard_deadline_still_caps_an_endlessly_chatty_stream(self):
        """A process that streams forever must still hit the backstop."""
        script = [(0.01, b"tick\n")] * 500
        lines, err = drive(script, idle=1.0, hard=0.15)
        self.assertIsInstance(err, asyncio.TimeoutError)
        self.assertNotIsInstance(err, Silent, "hitting the cap is not silence")
        self.assertGreater(len(lines), 1, "it streamed before being capped")

    def test_expired_hard_deadline_raises_immediately(self):
        lines, err = drive([(0.01, b"a\n")], idle=1.0, hard=-1)
        self.assertIsInstance(err, asyncio.TimeoutError)
        self.assertNotIsInstance(err, Silent)
        self.assertEqual(lines, [])

    def test_idle_window_is_capped_by_the_remaining_hard_budget(self):
        """With idle > remaining budget, the wait must not overshoot the cap."""
        async def run():
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            settings.idle_timeout = 10.0
            try:
                await read_line(FakeStream([]), t0 + 0.1, loop)
            except asyncio.TimeoutError:
                return loop.time() - t0
        old = settings.idle_timeout
        try:
            elapsed = asyncio.run(asyncio.wait_for(run(), timeout=5))
        finally:
            settings.idle_timeout = old
        self.assertLess(elapsed, 1.0, f"waited {elapsed:.2f}s, should stop at the 0.1s cap")


class TestDefaults(unittest.TestCase):
    def test_idle_is_far_below_the_hard_cap(self):
        """Sanity: the idle window must be the binding limit, not the backstop."""
        self.assertLess(settings.idle_timeout, settings.request_timeout)

    def test_idle_default_clears_the_measured_worst_case(self):
        """Measured max silence: 10.1s (prefill at 1MB context). Keep real headroom."""
        self.assertGreaterEqual(settings.idle_timeout, 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
