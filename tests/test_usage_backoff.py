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

    async def test_there_is_no_way_to_bypass_the_block(self):
        """Es gab einmal ein `force`, das den Cache umging — aber nicht die Sperre.

        Damit versprach es etwas, das es im wichtigsten Fall nicht halten konnte, und
        außerhalb einer Sperre erhöhte es die Chance auf genau die Drosselung. Der
        Parameter ist weg; dieser Test hält fest, dass keiner nachwächst.
        """
        import inspect
        self.assertEqual(list(inspect.signature(limits.usage).parameters), [],
                         "usage() nimmt keine Argumente — kein Umgehungsweg zum Hammer")

    async def test_the_block_reports_the_remaining_time(self):
        """Die Restzeit muss mit, sonst rät der Konsument.

        Beobachtet ohne sie: fünf Abrufe in drei Sekunden, was die Drosselung nur
        verlängert. Und es ist die **Rest**zeit, nicht der Ausgangswert — nach der
        halben Sperre ist die Hälfte übrig.
        """
        err = limits.UsageUnavailable("upstream 429", retry_after=120)
        with mock.patch.object(limits, "_fetch_sync", side_effect=err):
            with self.assertRaises(limits.UsageUnavailable):
                await limits.usage()
            # Zweiter Abruf: aus der Sperre, mit Restzeit statt ohne.
            with self.assertRaises(limits.UsageUnavailable) as caught:
                await limits.usage()
        self.assertIsNotNone(caught.exception.retry_after)
        self.assertGreater(caught.exception.retry_after, 118)
        self.assertLessEqual(caught.exception.retry_after, 120)

    async def test_the_remaining_time_never_goes_negative(self):
        """Eine abgelaufene Sperre darf keine negative Wartezeit melden."""
        limits._cache["retry_at"] = __import__("time").monotonic() - 5
        self.assertEqual(limits._remaining(__import__("time").monotonic()), 0.0)

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
