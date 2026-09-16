"""Cost attribution stays with the exact pooled Claude process that owns the counter."""
import asyncio
import unittest
from unittest import mock

from app import pool as pool_module
from app import wire
from app.pool import Proc


class CostCoverage(unittest.TestCase):
    @staticmethod
    def proc():
        return Proc("bucket", ["claude"], "claude-sonnet-5")

    def test_full_result_without_pending_request_is_one_call(self):
        proc = self.proc()
        stats = {"cost_usd": 0.25}
        epoch = proc._cost_epoch
        proc._settle_cost(stats, None)
        self.assertEqual(stats["cost_usd"], 0.25)
        self.assertEqual(stats["cost_scope"], "call")
        self.assertEqual(stats["cost_covered_requests"], 1)
        self.assertEqual(stats["cost_epoch"], epoch)

    def test_same_scope_pending_requests_form_one_turn_coverage(self):
        proc = self.proc()
        proc._defer_cost("turn-a")
        proc._defer_cost("turn-a")
        stats = {"cost_usd": 0.3, "model_usage": {"model": {"costUSD": 0.3}}}
        proc._settle_cost(stats, "turn-a")
        self.assertEqual(stats["cost_scope"], "turn")
        self.assertEqual(stats["cost_covered_requests"], 3)
        self.assertAlmostEqual(stats["cost_usd"], 0.3)
        self.assertEqual(stats["model_usage"], {}, "turn delta must not claim final-result by_model coverage")
        self.assertEqual(proc._pending_cost_scope_ids, [])

    def test_mixed_or_missing_scope_identity_is_not_attributed(self):
        for pending, current in (("turn-a", "turn-b"), (None, None)):
            with self.subTest(pending=pending, current=current):
                proc = self.proc()
                proc._defer_cost(pending)
                stats = {"cost_usd": 0.4}
                proc._settle_cost(stats, current)
                self.assertIsNone(stats["cost_scope"])
                self.assertIsNone(stats["cost_covered_requests"])

    def test_same_session_different_turn_scope_does_not_merge(self):
        proc = self.proc()
        proc._defer_cost("session-a/turn-1")
        stats = {"cost_usd": 0.2}
        proc._settle_cost(stats, "session-a/turn-2")
        self.assertIsNone(stats["cost_scope"])
        self.assertIsNone(stats["cost_covered_requests"])

    def test_missing_total_remains_pending_for_a_later_same_scope_result(self):
        proc = self.proc()
        missing = {"cost_usd": None}
        proc._settle_cost(missing, "turn-a")
        self.assertIsNone(missing["cost_scope"])
        self.assertIsNone(missing["cost_covered_requests"])
        self.assertEqual(proc._pending_cost_scope_ids, ["turn-a"])

        final = {"cost_usd": 0.2}
        proc._settle_cost(final, "turn-a")
        self.assertEqual(final["cost_scope"], "turn")
        self.assertEqual(final["cost_covered_requests"], 2)

    def test_counter_reset_rotates_epoch_and_discards_pending_attribution(self):
        proc = self.proc()
        first = {"cost_usd": 0.5}
        proc._settle_cost(first, "session-a")
        old_epoch = proc._cost_epoch
        proc._defer_cost("session-a")

        reset = {"cost_usd": 0.1}
        proc._settle_cost(reset, "session-a")
        self.assertNotEqual(proc._cost_epoch, old_epoch)
        self.assertEqual(reset["cost_epoch"], proc._cost_epoch)
        self.assertIsNone(reset["cost_scope"])
        self.assertIsNone(reset["cost_covered_requests"])
        self.assertEqual(proc._pending_cost_scope_ids, [])

        following = {"cost_usd": 0.2}
        proc._settle_cost(following, "session-a")
        self.assertEqual(following["cost_scope"], "call")
        self.assertEqual(following["cost_covered_requests"], 1)
        self.assertAlmostEqual(following["cost_usd"], 0.1)

    def test_new_process_has_new_epoch_and_no_pending_requests(self):
        first = self.proc()
        first._defer_cost("session-a")
        second = self.proc()
        self.assertNotEqual(first._cost_epoch, second._cost_epoch)
        self.assertEqual(second._pending_cost_scope_ids, [])

    def test_pooled_driver_threads_session_id_only_to_the_owning_proc(self):
        class FakeProc:
            dead = False
            uses = 0

            def __init__(self):
                self.identities = []

            async def run_turn(self, _prompt, _stats, session_id=None, cost_scope_id=None):
                self.identities.append((session_id, cost_scope_id))
                yield wire.Done(stop_reason="end_turn")

        class FakePool:
            def __init__(self):
                self.proc = FakeProc()
                self.released = False

            async def acquire(self, *_args):
                return "key", self.proc, True

            async def release(self, _key, _proc):
                self.released = True

        async def run(fake):
            with mock.patch.object(pool_module, "pool", fake):
                return [event async for event in pool_module.pooled_drive_turn(
                    "prompt", [], "model", {}, session_id="session-a",
                    cost_scope_id="session-a/turn-7")]

        fake = FakePool()
        events = asyncio.run(run(fake))
        self.assertEqual(fake.proc.identities, [("session-a", "session-a/turn-7")])
        self.assertTrue(fake.released)
        self.assertEqual([event.type for event in events], ["done"])


if __name__ == "__main__":
    unittest.main()
