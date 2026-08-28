"""Kontingent-Projektion: beide Quellen müssen auf DENSELBEN Fensterschlüssel kommen.

Die Fixtures sind aufgezeichnete Antworten (MESSUNGEN.md §4), keine erfundenen Formen.
"""
import unittest

from app import limits


# Gekürzte, aber echte Antwort von GET /api/oauth/usage (Konto max, 2026-08-28).
USAGE = {
    "five_hour": {"utilization": 34.0, "resets_at": "2026-08-28T09:29:59.951654+00:00"},
    "seven_day": {"utilization": 73.0, "resets_at": "2026-08-29T17:59:59.951681+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "nimbus_quill": {"utilization": 0.0, "resets_at": None},
    "limits": [
        {"kind": "session", "group": "session", "percent": 34, "severity": "normal",
         "resets_at": "2026-08-28T09:29:59.951654+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 73, "severity": "normal",
         "resets_at": "2026-08-29T17:59:59.951681+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 1, "severity": "normal",
         "resets_at": "2026-08-29T17:59:59.951965+00:00", "is_active": False,
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
    "extra_usage": {"is_enabled": False, "spend_limit_reached": False, "utilization": None},
    "spend": {"can_purchase_credits": False},
}

# Aufgezeichnetes rate_limit_event. `overageStatus: rejected` ist auf diesem Konto der
# Dauerzustand (Guthaben org-weit deaktiviert) und darf deshalb NICHTS auslösen.
EVENT_ALLOWED = {"status": "allowed", "resetsAt": 1787909400, "rateLimitType": "five_hour",
                 "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
                 "isUsingOverage": False}


class WindowKeys(unittest.TestCase):
    def test_usage_projection(self):
        windows = limits.from_usage(USAGE)["windows"]
        self.assertEqual(set(windows), {"global/five_hour", "global/seven_day",
                                        "model:fable-5/seven_day"})
        self.assertEqual(windows["global/seven_day"]["used_percent"], 73)
        self.assertEqual(windows["global/five_hour"]["window_seconds"], 18000)
        self.assertEqual(windows["model:fable-5/seven_day"]["scope"]["model"], "Fable")

    def test_codename_slots_are_not_windows(self):
        """nimbus_quill steht top-level, aber nicht in limits[] — gemessen ein anderes
        Fenster als das skopierte (0.0 gegen 1). Es darf nicht als Fenster erscheinen."""
        windows = limits.from_usage(USAGE)["windows"]
        self.assertNotIn("global/nimbus_quill", windows)
        self.assertTrue(all("nimbus" not in k for k in windows))

    def test_turn_and_api_agree_on_the_key(self):
        """Der Kern: der Claim eines Fable-Turns muss auf denselben Schlüssel führen wie
        der weekly_scoped-Eintrag der API. In den Daten gibt es dafür keine Brücke —
        sie entsteht erst aus Dauer + Modell des Turns (limits.py, Modul-Docstring)."""
        from_api = set(limits.from_usage(USAGE)["windows"])
        from_turn = limits.window_key("seven_day_overage_included", "claude-fable-5")
        self.assertEqual(from_turn, "model:fable-5/seven_day")
        self.assertIn(from_turn, from_api)

    def test_account_wide_keys_need_no_model(self):
        self.assertEqual(limits.window_key("five_hour", None), "global/five_hour")
        self.assertEqual(limits.window_key("seven_day", "claude-opus-4-8"), "global/seven_day")

    def test_unknown_scoped_claim_without_model_is_none(self):
        """Lieber keine Zuordnung als eine falsche — der Konsument lädt dann nach."""
        self.assertIsNone(limits.window_key("seven_day_sonnet", None))

    def test_model_names_normalise_from_both_sides(self):
        for name in ("Fable", "fable", "Fable 5", "claude-fable-5", "fable-5"):
            self.assertEqual(limits.model_key(name), "fable-5", name)

    def test_unknown_model_is_passed_through(self):
        self.assertEqual(limits.model_key("Cinder Cove"), "cinder-cove")


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
