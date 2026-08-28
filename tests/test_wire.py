"""Das Wire-Vokabular gegen aufgezeichnete CLI-Zeilen.

Die Zeilen unten sind echt (CLI 2.1.198, MESSUNGEN.md §2) und nicht nachgebaut — genau
deshalb fangen sie Drift: ändert die CLI ein Feld, fällt es hier auf und nicht im Betrieb.
"""
import json
import unittest

from app import wire
from app.cli_driver import classify

MESSAGE_START = {"type": "stream_event", "event": {
    "type": "message_start", "message": {"usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 5507, "cache_read_input_tokens": 3219,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 5507},
        "output_tokens": 4, "service_tier": "standard"}}}}

TEXT_DELTA = {"type": "stream_event", "event": {
    "type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}}

THINKING_DELTA = {"type": "stream_event", "event": {
    "type": "content_block_delta",
    "delta": {"type": "thinking_delta", "thinking": "", "estimated_tokens": 200}}}

MESSAGE_DELTA = {"type": "stream_event", "event": {
    "type": "message_delta", "usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 147, "cache_read_input_tokens": 8592,
        "output_tokens": 550, "output_tokens_details": {"thinking_tokens": 490}}}}

RESULT = {"type": "result", "is_error": False, "result": "Hi.", "stop_reason": "end_turn",
          "duration_ms": 1567, "total_cost_usd": 0.0346857,
          "usage": {"input_tokens": 2, "cache_creation_input_tokens": 5507,
                    "cache_read_input_tokens": 3219, "output_tokens": 6,
                    "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                       "ephemeral_1h_input_tokens": 5507},
                    "service_tier": "standard"},
          "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 522, "costUSD": 0.000582},
                         "claude-sonnet-5": {"inputTokens": 2, "costUSD": 0.0341037}}}

TOOL_USE = {"type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 5},
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "mcp__t__get_weather",
                         "input": {"city": "Berlin"}}]}}

MODEL_ERROR = {"type": "result", "is_error": True, "result": "model not found",
               "subtype": "success", "api_error_status": 404}

RATE_LIMIT_OK = {"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed", "resetsAt": 1787909400, "rateLimitType": "five_hour",
    "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
    "isUsingOverage": False}}


def only(line, stats=None, model=None):
    events = classify(line, stats if stats is not None else {}, lambda: None, model)
    return events


class Vocabulary(unittest.TestCase):
    def test_text_delta(self):
        events = only(TEXT_DELTA)
        self.assertEqual([e.type for e in events], ["text_delta"])
        self.assertEqual(events[0].text, "Hi")

    def test_thinking_carries_no_text(self):
        """Die CLI redigiert den Denktext — das Ereignis darf kein Textfeld vortäuschen."""
        event = only(THINKING_DELTA)[0]
        self.assertEqual(event.type, "thinking_progress")
        self.assertEqual(event.tokens, 200)
        self.assertNotIn("text", event.payload())

    def test_message_start_is_recorded_but_silent(self):
        """Die Input-Usage steht früh fest, ist aber kein Ereignis: sie gehört ins Done."""
        stats = {}
        self.assertEqual(only(MESSAGE_START, stats), [])
        self.assertEqual(stats["usage_raw"]["cache_read_input_tokens"], 3219)

    def test_done_carries_everything_the_turn_learned(self):
        stats = {}
        only(MESSAGE_START, stats)
        done = only(RESULT, stats)[0]
        self.assertEqual(done.type, "done")
        self.assertEqual(done.stop_reason, "end_turn")
        self.assertEqual(done.text, "Hi.")
        self.assertEqual(done.usage["cache_read"], 3219)
        self.assertEqual(done.usage["cache_write_1h"], 5507)
        self.assertEqual(done.usage["input_total"], 2 + 5507 + 3219)
        self.assertEqual(done.timing["cli_ms"], 1567)
        # Fremdarbeit sichtbar: der Haiku-Nebenaufruf steckt in total_usd mit drin.
        self.assertIn("claude-haiku-4-5-20251001", done.cost["by_model"])

    def test_input_new_excludes_cache_hits(self):
        """Abweichung zum codex-Wrapper, bewusst benannt: dort schließt input die Treffer ein."""
        usage = wire.usage(RESULT["usage"])
        self.assertEqual(usage["input_new"], 2)
        self.assertEqual(usage["input_total"], usage["input_new"] + usage["cache_read"]
                         + usage["cache_write"])

    def test_real_thinking_tokens_win_over_the_estimate(self):
        stats = {}
        only(THINKING_DELTA, stats)          # Schätzung: 200
        only(MESSAGE_DELTA, stats)           # echt: 490
        self.assertEqual(stats["thinking_tokens"], 490)
        self.assertEqual(stats["thinking_tokens_estimated"], 200)
        self.assertEqual(only(RESULT, stats)[0].usage["thinking"], 490)

    def test_estimate_survives_a_turn_without_message_delta(self):
        """Tool-Turns und Abbrüche sehen nie ein message_delta."""
        stats = {}
        only(THINKING_DELTA, stats)
        self.assertEqual(stats["thinking_tokens"], 200)

    def test_tool_call_is_normalised(self):
        events = only(TOOL_USE)
        event = events[0]
        self.assertEqual(event.type, "tool_call")
        self.assertEqual(event.id, "toolu_1", "native Wire muss die Backend-ID behalten")
        self.assertEqual(event.name, "get_weather", "mcp__t__-Präfix muss weg")
        self.assertEqual(json.loads(event.arguments), {"city": "Berlin"})
        self.assertNotIn("_raw", event.payload(), "internes Feld darf nicht nach außen")
        done = events[1]
        self.assertEqual(done.type, "done")
        self.assertEqual(done.stop_reason, "tool_use")
        self.assertEqual(done.usage["input_total"], 10)

    def test_failure_keeps_the_upstream_status(self):
        event = only(MODEL_ERROR)[0]
        self.assertEqual(event.type, "failed")
        self.assertEqual(event.upstream_status, 404)

    def test_quiet_rate_limit_emits_nothing(self):
        """Der Normalfall ist kein Ereignis — sonst käme bei jedem Turn eines."""
        self.assertEqual(only(RATE_LIMIT_OK), [])

    def test_rate_limit_alarm_names_the_window(self):
        line = {"type": "rate_limit_event",
                "rate_limit_info": dict(RATE_LIMIT_OK["rate_limit_info"], status="rejected")}
        event = only(line, model="claude-sonnet-5")[0]
        self.assertEqual(event.type, "limit_status")
        self.assertEqual(event.window, "global/five_hour")
        self.assertTrue(event.usage_stale, "der Turn kennt keine Füllstände")

    def test_payload_is_json_serialisable(self):
        for line in (TEXT_DELTA, THINKING_DELTA, RESULT, TOOL_USE, MODEL_ERROR):
            for event in only(line):
                json.dumps(event.payload())     # wirft, wenn etwas nicht serialisierbar ist


if __name__ == "__main__":
    unittest.main()
