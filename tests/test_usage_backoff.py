"""Age-controlled quota probes and explicit force modes (all offline)."""
import asyncio
import time
import unittest
from unittest import mock

from app import limits
from app.config import settings


GLOBAL = {
    "anthropic-ratelimit-unified-5h-utilization": "0.09",
    "anthropic-ratelimit-unified-5h-reset": "1788278400",
    "anthropic-ratelimit-unified-7d-utilization": "0.19",
    "anthropic-ratelimit-unified-7d-reset": "1788631200",
}
FABLE = {**GLOBAL,
    "anthropic-ratelimit-unified-7d_oi-utilization": "0.04",
    "anthropic-ratelimit-unified-7d_oi-reset": "1788631200",
}


def fresh_both(now=None):
    now = int(time.time()) if now is None else now
    limits.observe_turn_headers(FABLE, "claude-fable-5", now=now)


class ProbePolicy(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        limits._reset_observations()
        limits._probe_inflight.update({"global": None, "fable-5": None})
        self.old_global = settings.usage_global_max_age
        self.old_fable = settings.usage_fable_max_age
        settings.usage_global_max_age = 900
        settings.usage_fable_max_age = 7200

    def tearDown(self):
        settings.usage_global_max_age = self.old_global
        settings.usage_fable_max_age = self.old_fable
        limits._reset_observations()

    async def test_fresh_observations_need_no_probe(self):
        fresh_both()
        with mock.patch.object(limits, "_run_probe") as probe:
            result = await limits.usage()
        probe.assert_not_called()
        self.assertLessEqual(result["groups"][0]["age_seconds"], 1)

    async def test_normal_refresh_uses_fable_when_both_are_missing(self):
        async def probe(kind):
            self.assertEqual(kind, "fable-5")
            fresh_both()
        with mock.patch.object(limits, "_run_probe", side_effect=probe) as run:
            await limits.usage()
        run.assert_awaited_once_with("fable-5")

    async def test_only_global_stale_uses_haiku(self):
        now = int(time.time())
        fresh_both(now)
        limits._observed["global"]["observed_at"] = now - 901
        async def probe(kind):
            self.assertEqual(kind, "global")
            limits.observe_turn_headers(GLOBAL, "claude-haiku-4-5", now=now)
        with mock.patch.object(limits, "_run_probe", side_effect=probe) as run:
            await limits.usage()
        run.assert_awaited_once_with("global")

    async def test_fable_stale_probe_also_covers_global(self):
        now = int(time.time())
        fresh_both(now)
        limits._observed["model:fable-5"]["observed_at"] = now - 7201
        with mock.patch.object(limits, "_run_probe") as run:
            await limits.usage()
        run.assert_awaited_once_with("fable-5")

    async def test_force_modes_ignore_age(self):
        for force, expected in (("global", "global"), ("fable-5", "fable-5"),
                                ("all", "fable-5")):
            with self.subTest(force=force):
                fresh_both()
                with mock.patch.object(limits, "_run_probe") as run:
                    await limits.usage(force)
                run.assert_awaited_once_with(expected)

    async def test_invalid_force_is_rejected(self):
        with self.assertRaises(ValueError):
            await limits.usage("true")

    async def test_known_snapshot_survives_probe_failure_with_real_age(self):
        fresh_both(int(time.time()) - 10000)
        with mock.patch.object(limits, "_run_probe",
                               side_effect=limits.UsageUnavailable("no headers")):
            result = await limits.usage()
        self.assertGreaterEqual(result["groups"][0]["age_seconds"], 10000)

    async def test_cold_start_failure_is_an_error(self):
        with mock.patch.object(limits, "_run_probe",
                               side_effect=limits.UsageUnavailable("no headers")):
            with self.assertRaises(limits.UsageUnavailable):
                await limits.usage()

    async def test_singleflight_joins_concurrent_force_calls(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def probe(kind):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            limits.observe_turn_headers(GLOBAL, "claude-haiku-4-5")

        with mock.patch.object(limits, "_probe", side_effect=probe):
            first = asyncio.create_task(limits.usage("global"))
            await entered.wait()
            second = asyncio.create_task(limits.usage("global"))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
