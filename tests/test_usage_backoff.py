"""Verhalten der Usage-Abfrage, wenn die Gegenstelle nicht mitspielt.

Aus dem Betrieb: die Usage-API antwortete im Container mit **429**, während dasselbe Konto
von anderer Stelle 200 bekam. Ohne Sperre löst dann jeder Consumer-Request einen neuen
Versuch aus — der Wrapper hält das Limit selbst am Leben.
"""
import unittest
from unittest import mock

from app import limits


def _reset():
    limits._cache.update({"at": 0.0, "val": None, "retry_at": None, "last_error": None})


USAGE = {"limits": [{"kind": "session", "group": "session", "percent": 12,
                     "resets_at": "2026-08-28T09:29:59.951654+00:00", "scope": None,
                     "is_active": True}],
         "extra_usage": {}, "spend": {}}


class Backoff(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    async def test_success_is_cached(self):
        with mock.patch.object(limits, "_fetch_sync", return_value=USAGE) as fetch:
            await limits.usage()
            await limits.usage()
        self.assertEqual(fetch.call_count, 1, "der zweite Abruf kommt aus dem Cache")

    async def test_failure_blocks_further_attempts(self):
        """Der Kern: ein 429 darf nicht bei jedem Request erneut angefragt werden."""
        err = limits.UsageUnavailable("upstream 429")
        with mock.patch.object(limits, "_fetch_sync", side_effect=err) as fetch:
            for _ in range(5):
                with self.assertRaises(limits.UsageUnavailable):
                    await limits.usage()
        self.assertEqual(fetch.call_count, 1, "nach dem Fehlschlag wird gesperrt")

    async def test_force_does_not_bypass_the_block(self):
        err = limits.UsageUnavailable("upstream 429")
        with mock.patch.object(limits, "_fetch_sync", side_effect=err) as fetch:
            with self.assertRaises(limits.UsageUnavailable):
                await limits.usage()
            with self.assertRaises(limits.UsageUnavailable):
                await limits.usage(force=True)
        self.assertEqual(fetch.call_count, 1, "sonst wäre force der Umgehungsweg zum Hammer")

    async def test_retry_after_is_honoured(self):
        err = limits.UsageUnavailable("upstream 429", retry_after=42)
        with mock.patch.object(limits, "_fetch_sync", side_effect=err):
            with self.assertRaises(limits.UsageUnavailable):
                await limits.usage()
        remaining = limits._cache["retry_at"] - __import__("time").monotonic()
        self.assertGreater(remaining, 40)
        self.assertLess(remaining, 43)

    async def test_absurd_retry_after_is_capped(self):
        """Ein Wert von Stunden darf den Endpunkt nicht stilllegen."""
        import urllib.error
        http_err = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "99999"}, None)
        with mock.patch.object(limits.urllib.request, "urlopen", side_effect=http_err), \
                mock.patch.object(limits, "_token", return_value=("x", "test")):
            with self.assertRaises(limits.UsageUnavailable) as cm:
                limits._fetch_sync()
        self.assertEqual(cm.exception.retry_after, limits.MAX_BACKOFF)

    async def test_a_known_state_survives_a_failure(self):
        """Ein alter Füllstand ist brauchbar — die Auflösung ist ohnehin ein Prozentpunkt."""
        with mock.patch.object(limits, "_fetch_sync", return_value=USAGE):
            first = await limits.usage()
        self.assertNotIn("stale", first)

        limits._cache["at"] = 0.0                      # Cache als abgelaufen markieren
        err = limits.UsageUnavailable("upstream 429")
        with mock.patch.object(limits, "_fetch_sync", side_effect=err):
            stale = await limits.usage()
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["stale_reason"], "upstream 429")
        self.assertEqual(stale["windows"], first["windows"], "die Zahlen bleiben unverändert")

    async def test_recovery_clears_the_block(self):
        err = limits.UsageUnavailable("upstream 429", retry_after=0)
        with mock.patch.object(limits, "_fetch_sync", side_effect=err):
            with self.assertRaises(limits.UsageUnavailable):
                await limits.usage()
        with mock.patch.object(limits, "_fetch_sync", return_value=USAGE):
            value = await limits.usage()
        self.assertNotIn("stale", value)
        self.assertIsNone(limits._cache["retry_at"])


if __name__ == "__main__":
    unittest.main()
