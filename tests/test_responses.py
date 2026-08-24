"""Unit tests for app/responses.py — the /v1/responses translation. No CLI, no backend.

The endpoint deliberately reuses messages_to_prompt(), so these tests only cover the mapping
into (and out of) that shared path.
"""
import json
import unittest
from unittest import mock

from app import main

from app.config import settings
from app.responses import (
    status_from_stop,
    ThinkingSummary,
    envelope,
    function_call_item,
    input_to_messages,
    message_item,
    tools_to_openai,
    usage_obj,
)
from app.translate import messages_to_prompt


def msg(role, *parts):
    return {"type": "message", "role": role, "content": list(parts)}


class TestInput(unittest.TestCase):
    def test_string_input_shorthand(self):
        self.assertEqual(input_to_messages({"input": "hi"}),
                         [{"role": "user", "content": "hi"}])

    def test_instructions_become_a_system_message_first(self):
        out = input_to_messages({"instructions": "be brief", "input": "hi"})
        self.assertEqual(out[0], {"role": "system", "content": "be brief"})
        self.assertEqual(out[1]["role"], "user")

    def test_input_text_and_output_text_parts(self):
        out = input_to_messages({"input": [
            msg("user", {"type": "input_text", "text": "q"}),
            msg("assistant", {"type": "output_text", "text": "a"}),
        ]})
        self.assertEqual(out[0]["content"], [{"type": "text", "text": "q"}])
        self.assertEqual(out[1]["content"], [{"type": "text", "text": "a"}])

    def test_function_call_becomes_assistant_tool_call(self):
        out = input_to_messages({"input": [
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": '{"x":1}'}]})
        self.assertEqual(out[0]["role"], "assistant")
        tc = out[0]["tool_calls"][0]
        self.assertEqual((tc["id"], tc["function"]["name"], tc["function"]["arguments"]),
                         ("c1", "f", '{"x":1}'))

    def test_function_call_output_becomes_tool_message(self):
        out = input_to_messages({"input": [
            {"type": "function_call_output", "call_id": "c1", "output": "18C"}]})
        self.assertEqual(out[0], {"role": "tool", "tool_call_id": "c1", "content": "18C"})

    def test_non_string_tool_output_is_serialized(self):
        out = input_to_messages({"input": [
            {"type": "function_call_output", "call_id": "c1", "output": {"temp": 18}}]})
        self.assertEqual(out[0]["content"], '{"temp": 18}')

    def test_reasoning_items_are_dropped(self):
        """Clients echo reasoning items back; ours carry no content worth replaying."""
        out = input_to_messages({"input": [
            {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "x"}]},
            msg("user", {"type": "input_text", "text": "hi"})]})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["role"], "user")

    def test_previous_response_id_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            input_to_messages({"input": "hi", "previous_response_id": "resp_1"})
        self.assertIn("stateless", str(cm.exception))

    def test_store_is_ignored(self):
        """We keep no state, so store is simply irrelevant — it must not fail the request."""
        self.assertEqual(input_to_messages({"input": "hi", "store": True}),
                         [{"role": "user", "content": "hi"}])

    def test_bad_input_type_raises(self):
        with self.assertRaises(ValueError):
            input_to_messages({"input": 42})


class TestImages(unittest.TestCase):
    """Open WebUI sends input_image with the URL as a plain STRING, not a dict."""

    DATA_URI = "data:image/png;base64,iVBORw0KGgo="

    def test_input_image_string_reaches_the_prompt_as_an_image_block(self):
        old = settings.cache_history
        settings.cache_history = True
        try:
            messages = input_to_messages({"input": [msg(
                "user", {"type": "input_text", "text": "what?"},
                {"type": "input_image", "image_url": self.DATA_URI})]})
            self.assertEqual(messages[0]["content"][1],
                             {"type": "image_url", "image_url": {"url": self.DATA_URI}})
            blocks = messages_to_prompt(messages)
            imgs = [b for b in blocks if b["type"] == "image"]
            self.assertEqual(len(imgs), 1, "image must survive into the CLI prompt")
            self.assertEqual(imgs[0]["source"]["media_type"], "image/png")
        finally:
            settings.cache_history = old

    def test_chat_style_image_part_is_tolerated(self):
        messages = input_to_messages({"input": [msg(
            "user", {"type": "image_url", "image_url": {"url": self.DATA_URI}})]})
        self.assertEqual(messages[0]["content"][0]["image_url"]["url"], self.DATA_URI)


class TestTools(unittest.TestCase):
    def test_flat_responses_form_is_nested_for_the_shared_path(self):
        out = tools_to_openai([{"type": "function", "name": "f", "description": "d",
                                "parameters": {"type": "object"}}])
        self.assertEqual(out, [{"type": "function", "function": {
            "name": "f", "description": "d", "parameters": {"type": "object"}}}])

    def test_already_nested_is_passed_through(self):
        t = {"type": "function", "function": {"name": "f"}}
        self.assertEqual(tools_to_openai([t]), [t])

    def test_builtin_tools_are_skipped(self):
        """web_search etc. run server-side at OpenAI — we cannot provide them."""
        self.assertEqual(tools_to_openai([{"type": "web_search"}]), [])


class TestOutput(unittest.TestCase):
    def test_usage_uses_responses_names(self):
        u = usage_obj({"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
                       "prompt_tokens_details": {"cached_tokens": 7}})
        self.assertEqual(u["input_tokens"], 10)
        self.assertEqual(u["output_tokens"], 3)
        self.assertEqual(u["total_tokens"], 13)
        self.assertEqual(u["input_tokens_details"]["cached_tokens"], 7)

    def test_message_and_function_call_items(self):
        m = message_item("hi")
        self.assertEqual(m["content"][0], {"type": "output_text", "text": "hi", "annotations": []})
        f = function_call_item({"id": "call_1", "function": {"name": "f", "arguments": "{}"}})
        self.assertEqual((f["type"], f["call_id"], f["name"]), ("function_call", "call_1", "f"))

    def test_envelope_shape(self):
        e = envelope("resp_1", "sonnet", [message_item("hi")])
        self.assertEqual((e["object"], e["status"], e["model"]), ("response", "completed", "sonnet"))
        self.assertEqual(e["id"], "resp_1")
        self.assertIn("usage", e)

    def test_failed_envelope_carries_the_error(self):
        e = envelope("resp_1", "sonnet", [], status="failed",
                     error={"code": "timeout", "message": "boom"})
        self.assertEqual(e["status"], "failed")
        self.assertEqual(e["error"]["code"], "timeout")


class TestStatusAndEcho(unittest.TestCase):
    def test_truncated_answer_is_incomplete_not_completed(self):
        """A cut-off answer reported as `completed` would look like a finished result."""
        status, details = status_from_stop("max_tokens")
        self.assertEqual(status, "incomplete")
        self.assertEqual(details, {"reason": "max_output_tokens"})
        e = envelope("resp_1", "sonnet", [], status=status, incomplete_details=details)
        self.assertEqual(e["incomplete_details"], {"reason": "max_output_tokens"})

    def test_normal_stops_are_completed_without_details(self):
        for stop in ("end_turn", "tool_use", "stop_sequence", None):
            with self.subTest(stop=stop):
                status, details = status_from_stop(stop)
                self.assertEqual(status, "completed")
                self.assertIsNone(details)
        self.assertNotIn("incomplete_details", envelope("r", "m", []))

    def test_request_tools_are_echoed_not_invented(self):
        tools = [{"type": "function", "name": "f"}]
        e = envelope("r", "m", [], tools=tools, tool_choice="required")
        self.assertEqual(e["tools"], tools)
        self.assertEqual(e["tool_choice"], "required")

    def test_parallel_tool_calls_is_always_false(self):
        """Not a default — the CLI emits exactly one tool_use per turn."""
        self.assertFalse(envelope("r", "m", [])["parallel_tool_calls"])

    def test_background_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            input_to_messages({"input": "hi", "background": True})
        self.assertIn("background", str(cm.exception))


class TestReasoningTokens(unittest.TestCase):
    def test_thinking_estimate_is_reported(self):
        u = usage_obj({"completion_tokens": 900, "prompt_tokens": 10, "total_tokens": 910}, 500)
        self.assertEqual(u["output_tokens_details"]["reasoning_tokens"], 500)

    def test_estimate_is_capped_at_output_tokens(self):
        """estimated_tokens is the CLI's guess; a subset larger than the total reads as broken."""
        u = usage_obj({"completion_tokens": 100, "prompt_tokens": 10, "total_tokens": 110}, 5000)
        self.assertEqual(u["output_tokens_details"]["reasoning_tokens"], 100)

    def test_zero_without_thinking(self):
        u = usage_obj({"completion_tokens": 50})
        self.assertEqual(u["output_tokens_details"]["reasoning_tokens"], 0)

    def test_cost_is_carried_like_on_the_chat_endpoint(self):
        self.assertEqual(usage_obj({}, 0, 0.42)["cost"], 0.42)
        self.assertNotIn("cost", usage_obj({}))


class TestThinkingSummary(unittest.TestCase):
    """One summary part that gets REPLACED — clients overwrite summary[i] on .done."""

    def test_first_update_opens_item_and_part(self):
        t = ThinkingSummary(0, interval=0)
        evs = t.update(50, 0.0)
        self.assertEqual([n for n, _ in evs],
                         ["response.output_item.added", "response.reasoning_summary_part.added"])
        self.assertEqual(evs[0][1]["item"]["summary"], [], "summary must exist for later replaces")
        self.assertEqual(evs[1][1]["part"]["text"], "Thinking… · 50 tokens")

    def test_later_updates_replace_the_same_part(self):
        t = ThinkingSummary(0, interval=0)
        t.update(50, 0.0)
        evs = t.update(150, 1.0)
        self.assertEqual([n for n, _ in evs], ["response.reasoning_summary_part.done"])
        self.assertEqual(evs[0][1]["summary_index"], 0, "same index -> replaced, not appended")
        self.assertEqual(evs[0][1]["part"]["text"], "Thinking… · 200 tokens")

    def test_throttling_keeps_counting(self):
        t = ThinkingSummary(0, interval=2)
        t.update(100, 0.0)
        self.assertEqual(t.update(100, 0.5), [])
        self.assertEqual(t.update(100, 2.1)[0][1]["part"]["text"], "Thinking… · 300 tokens")

    def test_close_emits_a_completed_item(self):
        t = ThinkingSummary(0, interval=0)
        t.update(2850, 0.0)
        evs = t.close()
        self.assertEqual(evs[0][0], "response.output_item.done")
        item = evs[0][1]["item"]
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["summary"][0]["text"], "Thinking… · 2.9k tokens")

    def test_close_without_any_thinking_is_silent(self):
        self.assertEqual(ThinkingSummary(0).close(), [])

    def test_default_interval_comes_from_its_own_setting(self):
        """Separate from the chat endpoint: replacing in place tolerates a short interval."""
        self.assertEqual(ThinkingSummary(0).interval, settings.thinking_interval_responses)


class TestStreamComposition(unittest.IsolatedAsyncioTestCase):
    """The event stream as a whole — this is where the mixed-turn bugs lived.

    Claude often announces a tool call before making it, so thinking + text + tool_use land in
    ONE turn. That combination duplicated the reasoning item (close() ran twice) and dropped the
    announcement text (its index collided and it never reached the final envelope, which
    response.completed uses to replace everything the client had accumulated).
    """

    async def _events(self, turn):
        async def fake_drive(prompt, mcp_tools, model, stats, effort, append_system=None):
            for kind, data in turn:
                yield kind, data

        with mock.patch.object(main, "drive_turn", fake_drive):
            chunks = [c async for c in main._responses_stream(
                "resp_1", "sonnet", "prompt", [], "sonnet", {}, None,
                {"tools": [], "tool_choice": "auto"})]
        return [json.loads(line[6:]) for c in chunks for line in c.splitlines()
                if line.startswith("data: ")]

    @staticmethod
    def _done(evs):
        return [(e["output_index"], e["item"]["type"])
                for e in evs if e["type"] == "response.output_item.done"]

    @staticmethod
    def _final(evs):
        return [e for e in evs if e["type"] == "response.completed"][0]["response"]["output"]

    TOOL = [{"name": "mcp__t__get_weather", "input": {"city": "Berlin"}}]

    async def test_thinking_text_and_tool_in_one_turn(self):
        evs = await self._events([("thinking", 700), ("delta", "Ich schaue nach. "),
                                  ("tool_use", self.TOOL)])
        self.assertEqual(self._done(evs), [(0, "reasoning"), (1, "message"), (2, "function_call")],
                         "one reasoning item, and every item on its own index")
        final = self._final(evs)
        self.assertEqual([i["type"] for i in final], ["reasoning", "message", "function_call"])
        msg = [i for i in final if i["type"] == "message"][0]
        self.assertEqual(msg["content"][0]["text"], "Ich schaue nach. ",
                         "the announcement must survive into the envelope")

    async def test_thinking_then_tool_without_text(self):
        evs = await self._events([("thinking", 700), ("tool_use", self.TOOL)])
        self.assertEqual(self._done(evs), [(0, "reasoning"), (1, "function_call")])
        self.assertEqual([i["type"] for i in self._final(evs)], ["reasoning", "function_call"])

    async def test_thinking_then_plain_answer(self):
        evs = await self._events([("thinking", 700), ("delta", "Hallo"), ("result", "Hallo")])
        self.assertEqual(self._done(evs), [(0, "reasoning"), (1, "message")])
        self.assertEqual([i["type"] for i in self._final(evs)], ["reasoning", "message"])

    async def test_answer_without_any_thinking_starts_at_index_zero(self):
        evs = await self._events([("delta", "Hallo"), ("result", "Hallo")])
        self.assertEqual(self._done(evs), [(0, "message")])
        self.assertEqual([i["type"] for i in self._final(evs)], ["message"])

    async def test_reasoning_item_appears_exactly_once(self):
        """close() is reached from the text branch AND the tool branch."""
        evs = await self._events([("thinking", 700), ("delta", "x"), ("tool_use", self.TOOL)])
        reasoning = [e for e in evs if e["type"] == "response.output_item.done"
                     and e["item"]["type"] == "reasoning"]
        self.assertEqual(len(reasoning), 1)
        self.assertEqual(sum(1 for i in self._final(evs) if i["type"] == "reasoning"), 1)

    async def test_terminal_event_is_always_completed(self):
        evs = await self._events([("delta", "x"), ("result", "x")])
        self.assertEqual(evs[-1]["type"], "response.completed")

    async def test_sequence_numbers_are_strictly_increasing(self):
        evs = await self._events([("thinking", 700), ("delta", "x"), ("result", "x")])
        seqs = [e["sequence_number"] for e in evs]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
