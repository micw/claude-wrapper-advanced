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
