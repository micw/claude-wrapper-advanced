"""Quota projection from recorded CLI stream events and response headers."""
import unittest

from app import limits


# Aufgezeichnetes rate_limit_event. `overageStatus: rejected` ist auf diesem Konto der
# Dauerzustand (Guthaben org-weit deaktiviert) und darf deshalb NICHTS auslösen.
EVENT_ALLOWED = {"status": "allowed", "resetsAt": 1787909400, "rateLimitType": "five_hour",
                 "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
                 "isUsingOverage": False}

HEADERS = {
    "anthropic-ratelimit-unified-5h-utilization": "0.08",
    "anthropic-ratelimit-unified-5h-reset": "1788278400",
    "anthropic-ratelimit-unified-7d-utilization": "0.19",
    "anthropic-ratelimit-unified-7d-reset": "1788631200",
    "anthropic-ratelimit-unified-7d_oi-utilization": "0.04",
    "anthropic-ratelimit-unified-7d_oi-reset": "1788631200",
    "anthropic-ratelimit-unified-status": "allowed",  # deliberately not projected
}


class WindowKeys(unittest.TestCase):
    def test_account_wide_keys_need_no_model(self):
        self.assertEqual(limits.window_key("five_hour", None), "global/five_hour")
        self.assertEqual(limits.window_key("seven_day", "claude-opus-4-8"), "global/seven_day")

    def test_unknown_scoped_claim_without_model_is_none(self):
        """Lieber keine Zuordnung als eine falsche — der Konsument lädt dann nach."""
        self.assertIsNone(limits.window_key("seven_day_sonnet", None))

    def test_model_names_normalise_from_both_sides(self):
        for name in ("Fable", "fable", "Fable 5.1", "claude-fable-5-1", "fable-5-1"):
            self.assertEqual(limits.model_key(name), "fable-5-1", name)
        for name in ("Fable 5", "claude-fable-5", "fable-5"):
            self.assertEqual(limits.model_key(name), "fable-5", name)

    def test_unknown_model_is_passed_through(self):
        self.assertEqual(limits.model_key("Cinder Cove"), "cinder-cove")

    def test_dated_internal_model_normalises(self):
        self.assertEqual(limits.model_key("claude-haiku-4-5-20251001"), "haiku-4-5")


class HeaderUsage(unittest.TestCase):
    def setUp(self):
        limits._reset_observations()

    def tearDown(self):
        limits._reset_observations()

    def test_global_and_fable_have_stable_and_upstream_ids(self):
        result = limits.observe_turn_headers(HEADERS, "claude-fable-5-1", now=1000)
        groups = {g["id"]: g for g in result["groups"]}
        self.assertEqual(groups["global"]["upstream_id"], None)
        self.assertEqual(groups["model:fable-5"]["upstream_id"], "7d_oi")
        self.assertEqual(groups["model:fable-5"]["scope"], {
            "family": "fable", "models": ["fable-5-1", "fable-5"]})
        windows = {w["id"]: w for w in groups["global"]["windows"]}
        self.assertEqual(windows["five_hour"]["upstream_id"], "5h")
        self.assertEqual(windows["five_hour"]["used_percent"], 8.0)
        self.assertEqual(windows["seven_day"]["upstream_id"], "7d")

    def test_non_fable_does_not_claim_special_window(self):
        result = limits.observe_turn_headers(HEADERS, "claude-haiku-4-5", now=1000)
        fable = next(g for g in result["groups"] if g["id"] == "model:fable-5")
        self.assertIsNone(fable["observed_at"])
        self.assertIsNone(fable["windows"][0]["used_percent"])

    def test_both_fable_generations_update_the_shared_special_window(self):
        for model in ("claude-fable-5-1", "claude-fable-5"):
            with self.subTest(model=model):
                limits._reset_observations()
                result = limits.observe_turn_headers(HEADERS, model, now=1000)
                fable = next(g for g in result["groups"] if g["id"] == "model:fable-5")
                self.assertEqual(fable["windows"][0]["used_percent"], 4.0)

    def test_age_is_per_group(self):
        limits.observe_turn_headers(HEADERS, "claude-fable-5", now=1000)
        limits.observe_turn_headers(HEADERS, "claude-haiku-4-5", now=1100)
        groups = {g["id"]: g for g in limits.quota_snapshot(now=1120)["groups"]}
        self.assertEqual(groups["global"]["age_seconds"], 20)
        self.assertEqual(groups["model:fable-5"]["age_seconds"], 120)

    def test_parallel_older_percentage_does_not_move_backwards(self):
        limits.observe_turn_headers(HEADERS, "claude-haiku-4-5", now=1000)
        lower = dict(HEADERS, **{"anthropic-ratelimit-unified-5h-utilization": "0.07"})
        limits.observe_turn_headers(lower, "claude-haiku-4-5", now=1001)
        global_ = limits.quota_snapshot(now=1001)["groups"][0]
        five = next(w for w in global_["windows"] if w["id"] == "five_hour")
        self.assertEqual(five["used_percent"], 8.0)

    def test_new_reset_may_lower_percentage(self):
        limits.observe_turn_headers(HEADERS, "claude-haiku-4-5", now=1000)
        new = dict(HEADERS, **{
            "anthropic-ratelimit-unified-5h-utilization": "0.01",
            "anthropic-ratelimit-unified-5h-reset": "1788290000",
        })
        limits.observe_turn_headers(new, "claude-haiku-4-5", now=1001)
        five = limits.quota_snapshot(now=1001)["groups"][0]["windows"][0]
        self.assertEqual(five["used_percent"], 1.0)

    def test_status_is_not_in_public_usage(self):
        result = limits.observe_turn_headers(HEADERS, "claude-fable-5", now=1000)
        blob = __import__("json").dumps(result)
        self.assertNotIn("status", blob)
        self.assertNotIn("reached", blob)

    def test_malformed_pair_does_not_refresh_age(self):
        self.assertIsNone(limits.observe_turn_headers({
            "anthropic-ratelimit-unified-5h-utilization": "nope",
            "anthropic-ratelimit-unified-5h-reset": "123",
        }, "claude-haiku-4-5", now=1000))
        self.assertIsNone(limits.quota_snapshot(now=1000)["groups"][0]["observed_at"])


class TurnStatus(unittest.TestCase):
    def test_allowed_emits_nothing(self):
        self.assertIsNone(limits.status_from_event(EVENT_ALLOWED, "claude-sonnet-5"))

    def test_permanent_overage_rejection_is_not_an_alarm(self):
        """Sonst käme bei jedem Turn dieses Kontos ein limit_status."""
        event = dict(EVENT_ALLOWED, status="allowed", overageStatus="rejected")
        self.assertIsNone(limits.status_from_event(event, "claude-sonnet-5"))

    def test_warning_and_rejection_emit(self):
        for status in ("allowed_warning", "rejected"):
            event = dict(EVENT_ALLOWED, status=status)
            out = limits.status_from_event(event, "claude-sonnet-5")
            self.assertEqual(out["status"], status)
            self.assertEqual(out["window"], "global/five_hour")

    def test_overage_in_use_emits(self):
        event = dict(EVENT_ALLOWED, isUsingOverage=True)
        self.assertIsNotNone(limits.status_from_event(event, "claude-sonnet-5"))

    def test_scoped_rejection_names_the_model_window(self):
        event = dict(EVENT_ALLOWED, status="rejected",
                     rateLimitType="seven_day_overage_included")
        out = limits.status_from_event(event, "claude-fable-5")
        self.assertEqual(out["window"], "model:fable-5/seven_day")
        self.assertEqual(out["claim"], "seven_day_overage_included")

    def test_event_carries_no_numbers(self):
        """Der Turn hat keine Füllstände (gemessen). Das Flag sagt es dem Konsumenten,
        statt ihn eine fehlende Zahl als 0 lesen zu lassen."""
        out = limits.status_from_event(dict(EVENT_ALLOWED, status="rejected"), "x")
        self.assertTrue(out["usage_stale"])
        self.assertNotIn("used_percent", out)


if __name__ == "__main__":
    unittest.main()
