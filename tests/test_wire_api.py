"""`/wire/v1/info` und `/wire/v1/models`.

Beide sind schmal, und genau das soll so bleiben: `info` ist eine Identifikation, kein
Fähigkeitskatalog, und `models` ist die Registry ohne die Pseudo-Einträge, die `/v1/models`
für Model-Picker erzeugt.
"""
import asyncio
import unittest

from app import wire_api
from app.config import SERVICE, VERSION, settings


class FakeRequest:
    headers = {}


def call(endpoint):
    return asyncio.run(endpoint(FakeRequest()))


class Info(unittest.TestCase):
    def test_carries_service_and_version(self):
        self.assertEqual(call(wire_api.info), {"service": SERVICE, "version": VERSION})

    def test_stays_narrow(self):
        """Die Vertragsversion steht im Pfad, Fähigkeiten stehen bei den Endpunkten, für die
        sie gelten. Wächst hier etwas nach, ist das eine Entscheidung — kein Versehen."""
        self.assertEqual(set(call(wire_api.info)), {"service", "version"})

    def test_version_is_not_hardcoded_twice(self):
        """Sie stand schon einmal drei Releases lang falsch in main.py."""
        from app import main
        self.assertEqual(main.app.version, VERSION)
        self.assertEqual(main.app.title, SERVICE)


class Models(unittest.TestCase):
    def setUp(self):
        self.models = call(wire_api.models)["models"]

    def test_every_registry_model_appears_once(self):
        ids = [m["id"] for m in self.models]
        self.assertEqual(sorted(ids), sorted(settings.models))
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_pseudo_models(self):
        """`/v1/models` führt `opus:max` und `opus (latest)` — hier hat das nichts verloren."""
        for m in self.models:
            self.assertNotIn(":", m["id"], f"{m['id']} sieht nach einem Effort-Pick aus")
        self.assertNotIn("opus", [m["id"] for m in self.models], "Alias ist kein Modell")

    def test_aliases_hang_at_their_target(self):
        by_id = {m["id"]: m for m in self.models}
        for alias, target in settings.aliases.items():
            self.assertIn(alias, by_id[target]["aliases"])

    def test_backend_model_matches_the_cost_key(self):
        """done.cost.by_model ist nach dem CLI-Namen geschlüsselt — ohne ihn lässt sich eine
        Kostenzeile keinem Eintrag dieser Liste zuordnen."""
        for m in self.models:
            self.assertEqual(m["backend_model"], settings.models[m["id"]][0])

    def test_input_modalities_are_model_facts(self):
        """Vision ist keine globale Servicebehauptung: jeder Registry-Eintrag nennt selbst,
        was er annimmt, damit Consumer die Fähigkeit nicht aus dem Providernamen erraten."""
        for m in self.models:
            self.assertEqual(m["input_modalities"], list(settings.models[m["id"]][5]))
            self.assertIn("text", m["input_modalities"])

    def test_a_model_without_effort_levels_has_no_default(self):
        """Haiku kennt keine Stufen — dann ist der Default None und nicht 'high'."""
        haiku = next(m for m in self.models if m["id"] == "haiku-4-5")
        self.assertEqual(haiku["efforts"], {"supported": [], "default": None})

    def test_default_effort_is_clamped_to_what_the_model_knows(self):
        for m in self.models:
            supported = m["efforts"]["supported"]
            default = m["efforts"]["default"]
            if supported:
                self.assertIn(default, supported, m["id"])

    def test_cutoff_is_null_where_the_cli_names_none(self):
        """Opus 5 hat keinen — dann nennen wir auch keinen, statt einen zu erfinden."""
        opus5 = next(m for m in self.models if m["id"] == "opus-5")
        self.assertIsNone(opus5["knowledge_cutoff"])


if __name__ == "__main__":
    unittest.main()

class UsageUnavailableResponse(unittest.TestCase):
    """Der 503 muss sagen, wann es wieder Sinn hat.

    Die Gegenstelle nennt im 429 ein `Retry-After` (beobachtet: 871 s), und der Wrapper
    wertet es intern längst aus. Es für sich zu behalten heißt, dass der Konsument raten
    muss — beobachtet wurden fünf Abrufe in drei Sekunden, was die Drosselung verlängert.
    """

    @staticmethod
    def _call_with(err):
        from unittest import mock
        with mock.patch.object(wire_api.limits, "usage", side_effect=err):
            return asyncio.run(wire_api.get_usage(FakeRequest()))

    def test_retry_after_reaches_the_client(self):
        from app import limits
        resp = self._call_with(limits.UsageUnavailable("upstream 429", retry_after=871.0))

        self.assertEqual(resp.status_code, 503)
        # Header: der von RFC 9110 fuer 503 vorgesehene Weg, ganze Sekunden.
        self.assertEqual(resp.headers["retry-after"], "871")
        # Body: fuer Clients, die nur JSON lesen.
        import json
        body = json.loads(resp.body)["error"]
        self.assertEqual(body["retry_after"], 871)
        self.assertEqual(body["code"], "usage_unavailable")

    def test_without_a_known_time_no_header_is_invented(self):
        from app import limits
        resp = self._call_with(limits.UsageUnavailable("no OAuth token"))

        self.assertEqual(resp.status_code, 503)
        self.assertNotIn("retry-after", resp.headers)
        import json
        self.assertNotIn("retry_after", json.loads(resp.body)["error"])

