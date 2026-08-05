"""Unit tests for app/translate.py — pure logic, no CLI, no backend, no tokens.

Complements tests/assumptions.py: that one verifies what the *CLI* does (and costs tokens),
this one verifies what *we* build before handing it over. Run: python -m unittest discover tests
"""
import base64
import unittest
import zlib
import struct

from app.config import settings
from app.translate import (
    ThinkingProgress,
    finish_from_stop,
    map_effort,
    map_model,
    messages_to_prompt,
    openai_tools_to_mcp,
    split_model_effort,
    tooluse_to_toolcalls,
)


def png_b64(w=8, h=8):
    """Smallest real PNG we can build without a dependency."""
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


IMG_B64 = png_b64()
DATA_URI = "data:image/png;base64," + IMG_B64


def img_part(url=DATA_URI):
    return {"type": "image_url", "image_url": {"url": url}}


def texts(blocks):
    return [b["text"] for b in blocks if b["type"] == "text"]


def images(blocks):
    return [b for b in blocks if b["type"] == "image"]


class Base(unittest.TestCase):
    """Settings are a module-level singleton — snapshot and restore what we poke at."""

    KEYS = ("cache_history", "cache_history_ttl", "max_image_bytes", "max_image_mb", "max_images")

    def setUp(self):
        self._saved = {k: getattr(settings, k) for k in self.KEYS}
        settings.cache_history = True

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(settings, k, v)


class TestTextOnly(Base):
    def test_flat_string_without_cache_history(self):
        settings.cache_history = False
        out = messages_to_prompt([{"role": "user", "content": "hi"}])
        self.assertIsInstance(out, str)
        self.assertIn("User: hi", out)
        self.assertTrue(out.endswith("Respond to the latest message now."))

    def test_blocks_with_cache_history(self):
        out = messages_to_prompt([{"role": "user", "content": "hi"}])
        self.assertIsInstance(out, list)
        self.assertEqual(texts(out)[1], "User: hi\n")

    def test_cache_control_only_on_last_history_block(self):
        out = messages_to_prompt([{"role": "user", "content": "a"},
                                  {"role": "assistant", "content": "b"},
                                  {"role": "user", "content": "c"}])
        marked = [i for i, b in enumerate(out) if "cache_control" in b]
        self.assertEqual(len(marked), 1, "exactly one breakpoint")
        self.assertEqual(out[marked[0]]["text"], "User: c\n", "breakpoint on the newest message")
        self.assertEqual(marked[0], len(out) - 2, "closing block comes after it")
        self.assertEqual(out[marked[0]]["cache_control"],
                         {"type": "ephemeral", "ttl": settings.cache_history_ttl})

    def test_multimodal_list_without_images_is_plain_text(self):
        out = messages_to_prompt([{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
        self.assertEqual(images(out), [])
        self.assertEqual(texts(out)[1], "User: hi\n")

    def test_roles_and_tool_history_rendering(self):
        out = messages_to_prompt([
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "get_weather", "arguments": '{"city":"Berlin"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "18C"},
        ])
        t = texts(out)
        self.assertIn("[System instructions]\nbe brief", t[1])
        self.assertIn("Assistant: [called tool get_weather with arguments", t[3])
        self.assertIn("Tool get_weather returned: 18C", t[4], "tool_call_id resolves to the name")

    def test_history_blocks_are_append_stable(self):
        """THE caching invariant: an extra turn must not touch earlier blocks byte-for-byte."""
        first = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        out1 = messages_to_prompt(first)
        out2 = messages_to_prompt(first + [{"role": "user", "content": "c"}])
        strip = lambda bs: [{k: v for k, v in b.items() if k != "cache_control"} for b in bs]
        self.assertEqual(strip(out1)[:-1], strip(out2)[:len(out1) - 1],
                         "prefix blocks must stay identical, else the cache prefix breaks")


class TestImages(Base):
    def test_data_uri_becomes_image_block_before_its_text(self):
        out = messages_to_prompt([{"role": "user", "content": [
            {"type": "text", "text": "what is this?"}, img_part()]}])
        kinds = [b["type"] for b in out]
        self.assertEqual(kinds, ["text", "image", "text", "text"])
        self.assertEqual(out[1]["source"],
                         {"type": "base64", "media_type": "image/png", "data": IMG_B64})
        self.assertEqual(out[2]["text"], "User: what is this?\n", "image sits before its text")

    def test_image_forces_blocks_even_without_cache_history(self):
        settings.cache_history = False
        out = messages_to_prompt([{"role": "user", "content": [img_part()]}])
        self.assertIsInstance(out, list, "a flat string could not carry the image")
        self.assertEqual(len(images(out)), 1)
        self.assertFalse(any("cache_control" in b for b in out), "no breakpoint when caching is off")

    def test_input_image_and_anthropic_forms(self):
        for part in ({"type": "input_image", "image_url": DATA_URI},
                     {"type": "image", "source": {"type": "base64",
                                                  "media_type": "image/png", "data": IMG_B64}}):
            with self.subTest(part=part["type"]):
                out = messages_to_prompt([{"role": "user", "content": [part]}])
                self.assertEqual(len(images(out)), 1)

    def test_media_type_is_normalized(self):
        for given in ("image/jpg", "image/JPEG"):
            with self.subTest(given=given):
                out = messages_to_prompt([{"role": "user", "content": [
                    img_part(f"data:{given};base64,{IMG_B64}")]}])
                self.assertEqual(images(out)[0]["source"]["media_type"], "image/jpeg")

    def test_multiple_images_keep_order(self):
        a, b = png_b64(8, 8), png_b64(16, 16)
        out = messages_to_prompt([{"role": "user", "content": [
            img_part("data:image/png;base64," + a), img_part("data:image/png;base64," + b)]}])
        self.assertEqual([i["source"]["data"] for i in images(out)], [a, b])

    def test_image_in_older_message_stays_in_place(self):
        out = messages_to_prompt([
            {"role": "user", "content": [{"type": "text", "text": "look"}, img_part()]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "and now?"},
        ])
        self.assertEqual([b["type"] for b in out],
                         ["text", "image", "text", "text", "text", "text"])
        self.assertIn("cache_control", out[-2], "breakpoint still on the newest block")


class TestImagesRejected(Base):
    """Rejects must degrade to a note — never raise, never silently vanish."""

    def assert_dropped(self, part, needle):
        out = messages_to_prompt([{"role": "user", "content": [
            {"type": "text", "text": "hi"}, part]}])
        self.assertEqual(images(out), [], "image must not be passed through")
        note = [t for t in texts(out) if "could not be passed through" in t]
        self.assertTrue(note, "a note must reach the model")
        self.assertIn(needle, note[0])

    def test_remote_url_is_not_supported(self):
        self.assert_dropped(img_part("https://example.com/a.png"), "remote image URLs")

    def test_anthropic_url_source_is_not_supported(self):
        self.assert_dropped({"type": "image", "source": {"type": "url", "url": "https://x/a.png"}},
                            "remote image URLs")

    def test_unsupported_media_type(self):
        self.assert_dropped(img_part("data:image/tiff;base64," + IMG_B64), "unsupported media type")

    def test_non_base64_data_uri(self):
        self.assert_dropped(img_part("data:image/png,notbase64"), "base64 data URIs")

    def test_empty_and_malformed_parts(self):
        self.assert_dropped(img_part(""), "no image data")
        self.assert_dropped({"type": "image_url", "image_url": {}}, "no image data")

    def test_oversized_image(self):
        settings.max_image_mb, settings.max_image_bytes = 1, 1024 * 1024
        self.assert_dropped(img_part("data:image/png;base64," + "A" * (2 * 1024 * 1024)), "too large")

    def test_size_limit_is_measured_on_decoded_bytes(self):
        """base64 inflates by ~4/3 — a 1 MB limit must still accept ~1.3 MB of base64."""
        settings.max_image_mb, settings.max_image_bytes = 1, 1024 * 1024
        b64 = "A" * (1000 * 1024 * 4 // 3)                    # ~1000 KB decoded
        out = messages_to_prompt([{"role": "user", "content": [
            img_part("data:image/png;base64," + b64)]}])
        self.assertEqual(len(images(out)), 1)

    def test_max_images_cap(self):
        settings.max_images = 2
        out = messages_to_prompt([{"role": "user", "content": [img_part()] * 4}])
        self.assertEqual(len(images(out)), 2)
        self.assertIn("more than 2 images", "".join(texts(out)))


class TestThinkingProgress(unittest.TestCase):
    """estimated_tokens are INCREMENTS per event (measured: 50, 200, 150, 150, 250, …)."""

    def test_first_update_emits_immediately(self):
        p = ThinkingProgress(interval=2)
        self.assertEqual(p.update(50, 0.0), "Thinking… · 50 tokens")

    def test_tokens_accumulate_across_events(self):
        p = ThinkingProgress(interval=0)
        p.update(50, 0.0)
        self.assertEqual(p.update(200, 1.0), "\nThinking… · 250 tokens")
        self.assertEqual(p.update(150, 2.0), "\nThinking… · 400 tokens")

    def test_updates_are_throttled_but_still_counted(self):
        p = ThinkingProgress(interval=2)
        p.update(100, 0.0)
        self.assertIsNone(p.update(100, 0.5), "within the interval -> no line")
        self.assertIsNone(p.update(100, 1.9))
        self.assertEqual(p.update(100, 2.1), "\nThinking… · 400 tokens",
                         "throttled events must not be lost from the total")

    def test_thousands_are_abbreviated(self):
        p = ThinkingProgress(interval=0)
        p.update(2850, 0.0)
        self.assertEqual(p.update(0, 1.0), "\nThinking… · 2.9k tokens")

    def test_only_the_first_line_has_no_newline(self):
        p = ThinkingProgress(interval=0)
        self.assertFalse(p.update(10, 0.0).startswith("\n"))
        self.assertTrue(p.update(10, 1.0).startswith("\n"))

    def test_missing_estimate_does_not_crash(self):
        p = ThinkingProgress(interval=0)
        self.assertEqual(p.update(None, 0.0), "Thinking… · 0 tokens")


class TestPureHelpers(unittest.TestCase):
    def test_finish_from_stop(self):
        self.assertEqual(finish_from_stop("tool_use"), "tool_calls")
        self.assertEqual(finish_from_stop("max_tokens"), "length")
        self.assertEqual(finish_from_stop(None), "stop")
        self.assertEqual(finish_from_stop("something_new"), "stop")

    def test_split_model_effort(self):
        self.assertEqual(split_model_effort("opus:max"), ("opus", "max"))
        self.assertEqual(split_model_effort("opus:minimal"), ("opus", "low"))
        self.assertEqual(split_model_effort("claude-opus-4-8"), ("claude-opus-4-8", None))
        self.assertEqual(split_model_effort("opus[1m]"), ("opus[1m]", None))
        self.assertEqual(split_model_effort("opus:nonsense"), ("opus:nonsense", None))

    def test_map_effort_prefers_openrouter_shape(self):
        self.assertEqual(map_effort({"reasoning": {"effort": "high"}, "reasoning_effort": "low"}),
                         "high")
        self.assertEqual(map_effort({"reasoning_effort": "XHIGH"}), "xhigh")
        self.assertIsNone(map_effort({}))

    def test_map_model_falls_back_to_default(self):
        self.assertEqual(map_model("sonnet"), "sonnet")
        self.assertEqual(map_model("claude-opus-4-8"), "claude-opus-4-8")
        self.assertEqual(map_model("gpt-4o"), settings.default_model)
        self.assertEqual(map_model(""), settings.default_model)

    def test_openai_tools_to_mcp(self):
        out = openai_tools_to_mcp([
            {"type": "function", "function": {"name": "f", "description": "d",
                                              "parameters": {"type": "object"}}},
            {"type": "function", "function": {"description": "nameless"}},   # skipped
            {"type": "other", "function": {"name": "x"}},                    # skipped
        ])
        self.assertEqual(out, [{"name": "f", "description": "d",
                                "inputSchema": {"type": "object"}}])

    def test_tooluse_to_toolcalls_strips_mcp_prefix(self):
        out = tooluse_to_toolcalls([{"name": "mcp__t__get_weather", "input": {"city": "Berlin"}}])
        self.assertEqual(out[0]["function"]["name"], "get_weather")
        self.assertEqual(out[0]["function"]["arguments"], '{"city": "Berlin"}')
        self.assertTrue(out[0]["id"].startswith("call_"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
