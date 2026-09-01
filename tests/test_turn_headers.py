"""Dedicated-fd capture plumbing; real subprocess, no Claude/backend/tokens."""
import asyncio
import json
import sys
import unittest

from app import limits, wire
from app.cli_driver import spawn_cli
from app.config import settings
from app.turn_headers import quota_event


HEADERS = {
    "anthropic-ratelimit-unified-5h-utilization": "0.12",
    "anthropic-ratelimit-unified-5h-reset": "1788278400",
    "anthropic-ratelimit-unified-7d-utilization": "0.34",
    "anthropic-ratelimit-unified-7d-reset": "1788631200",
}


class CapturePipe(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        limits._reset_observations()
        self.old_bin = settings.claude_bin
        settings.claude_bin = sys.executable

    def tearDown(self):
        settings.claude_bin = self.old_bin
        limits._reset_observations()

    async def test_spawn_passes_fd_and_response_updates_usage(self):
        events = [
            {"capture": "claude-turn-headers-v1", "kind": "preload_ready"},
            {"capture": "claude-turn-headers-v1", "kind": "request", "id": 7,
             "model": "claude-haiku-4-5-20251001"},
            {"capture": "claude-turn-headers-v1", "kind": "response", "id": 7,
             "status": 200, "headers": HEADERS},
        ]
        script = (
            "import json,os\n"
            "fd=int(os.environ['CLAUDE_TURN_HEADERS_FD'])\n"
            f"events={events!r}\n"
            "[os.write(fd,(json.dumps(e)+'\\n').encode()) for e in events]\n"
        )
        proc = await spawn_cli(["-c", script], "claude-haiku-4-5")
        await proc.wait()
        await proc.turn_header_capture.close()

        self.assertTrue(proc.turn_header_capture.ready)
        snapshot = limits.quota_snapshot()
        global_ = snapshot["groups"][0]
        self.assertEqual(global_["windows"][0]["used_percent"], 12.0)
        event = await quota_event(proc)
        self.assertIsInstance(event, wire.Quota)
        self.assertEqual(event.payload()["type"], "quota")

    async def test_unknown_protocol_and_malformed_lines_are_ignored(self):
        script = (
            "import os\n"
            "fd=int(os.environ['CLAUDE_TURN_HEADERS_FD'])\n"
            "os.write(fd,b'not-json\\n')\n"
            "os.write(fd,b'{\"capture\":\"other\",\"kind\":\"response\"}\\n')\n"
        )
        proc = await spawn_cli(["-c", script])
        await proc.wait()
        await proc.turn_header_capture.close()
        self.assertIsNone(limits.quota_snapshot()["groups"][0]["observed_at"])


if __name__ == "__main__":
    unittest.main()
