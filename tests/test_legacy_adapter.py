"""Der Tupel-Adapter in `drive_turn()` — die Zusicherung, dass die OpenAI-Oberflächen
vom Wire-Umbau nichts merken.

Wichtig, weil `tests/test_responses.py` `drive_turn` wegmockt: ohne diese Datei wäre der
Adapter der einzige ungeprüfte Teil des Umbaus, und er trägt beide bestehenden Endpunkte.

Der Fall, der ihn brechen kann: seit 1.6.1 endet auch ein Tool-Turn im Wire-Strom mit
einem `Done` (vorher war der `ToolCall` selbst terminal). Der alte Vertrag kennt dafür
**nur** ein ('tool_use', blocks)-Tupel — käme das Done als ('result', '') mit, hinge an
jedem Tool-Call eine leere Antwort.
"""
import unittest
from unittest import mock

from app import cli_driver, wire

RAW_TOOL = {"type": "tool_use", "id": "toolu_1", "name": "mcp__t__get_weather",
            "input": {"city": "Berlin"}}


def events(*evs):
    """Ersetzt drive_turn_events durch einen festen Strom."""
    async def fake(*args, **kwargs):
        for ev in evs:
            yield ev
    return fake


async def collect(*evs):
    with mock.patch.object(cli_driver, "drive_turn_events", events(*evs)):
        return [tup async for tup in cli_driver.drive_turn("p", [], "m", {})]


class LegacyTuples(unittest.IsolatedAsyncioTestCase):
    async def test_plain_turn(self):
        out = await collect(
            wire.Started(model="m"),
            wire.TextDelta(text="Hi"),
            wire.Done(stop_reason="end_turn", text="Hi"),
        )
        self.assertEqual(out, [("delta", "Hi"), ("result", "Hi")])

    async def test_started_is_not_visible_to_the_old_contract(self):
        out = await collect(wire.Started(model="m"), wire.Done(stop_reason="end_turn", text=""))
        self.assertEqual(out, [("result", "")])

    async def test_tool_turn_yields_exactly_one_tool_use_and_no_empty_result(self):
        """Die Regression, die 1.6.1 einführen könnte."""
        out = await collect(
            wire.Started(model="m"),
            wire.ToolCall(id="toolu_1", name="get_weather", arguments='{"city":"Berlin"}',
                          _raw=RAW_TOOL),
            wire.Done(stop_reason="tool_use", text=""),
        )
        self.assertEqual(out, [("tool_use", [RAW_TOOL])])

    async def test_tool_turn_passes_the_raw_blocks(self):
        """main.py reicht sie an tooluse_to_toolcalls weiter — es müssen die CLI-Blöcke sein."""
        out = await collect(
            wire.ToolCall(id="x", name="get_weather", arguments="{}", _raw=RAW_TOOL),
            wire.Done(stop_reason="tool_use", text=""),
        )
        self.assertEqual(out[0][1][0]["name"], "mcp__t__get_weather",
                         "der Adapter darf den Präfix NICHT entfernen, das macht main.py")

    async def test_thinking_progress_becomes_the_old_thinking_tuple(self):
        out = await collect(wire.ThinkingProgress(tokens=200),
                            wire.Done(stop_reason="end_turn", text="x"))
        self.assertEqual(out, [("thinking", 200), ("result", "x")])

    async def test_failure_keeps_type_message_and_status(self):
        out = await collect(wire.Failed(error_type="cli_error", message="nope",
                                        upstream_status=404))
        self.assertEqual(out, [("error", {"type": "cli_error", "message": "nope",
                                          "status": 404})])

    async def test_limit_status_stays_out_of_the_old_contract(self):
        """Die alten Endpunkte kennen kein Kontingent-Ereignis — es darf sie nicht erreichen."""
        out = await collect(wire.LimitStatus(window="global/five_hour", status="rejected"),
                            wire.Done(stop_reason="end_turn", text="x"))
        self.assertEqual(out, [("result", "x")])


if __name__ == "__main__":
    unittest.main()
