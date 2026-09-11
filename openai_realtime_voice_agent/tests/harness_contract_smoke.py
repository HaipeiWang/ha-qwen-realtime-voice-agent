"""Contract invariants independent of any network, Pipecat or provider."""

import unittest
import asyncio
from dataclasses import replace

from jsonschema import ValidationError

from app.core.events import AudioChunk, AudioFormat, INPUT_FORMAT, OUTPUT_FORMAT
from app.core.turn import TurnContext
from app.providers.base import ProviderSession, RealtimeProvider
from fake_provider import FakeProvider
from app.tools.schema import CanonicalTool, CanonicalToolResult


class HarnessContractsTest(unittest.TestCase):
    def test_fake_provider_replays_without_network_or_ha(self):
        async def scenario():
            provider = FakeProvider()
            self.assertIsInstance(provider, RealtimeProvider)
            await provider.connect(TurnContext("conversation", 1).scope)
            await provider.configure_session(ProviderSession("brief"))
            await provider.disconnect()
            events = [event.kind.value async for event in provider.receive_events()]
            self.assertEqual(events, ["connected", "ready", "closed"])
            self.assertEqual(provider.commands, [])
        asyncio.run(scenario())

    def test_followup_is_a_new_turn_in_same_conversation(self):
        first = TurnContext("conversation", 1)
        second = TurnContext("conversation", 1)
        self.assertNotEqual(first.turn_id, second.turn_id)
        self.assertFalse(second.accepts(first.scope))

    def test_old_connection_and_unknown_response_are_rejected(self):
        turn = TurnContext("conversation", 3)
        turn.response_ids.add("reply")
        scope = replace(turn.scope, response_id="reply")
        self.assertTrue(turn.accepts(scope))
        self.assertFalse(turn.accepts(replace(scope, generation=2)))
        self.assertFalse(turn.accepts(replace(scope, response_id="old")))
        turn.cancelled = True
        self.assertFalse(turn.accepts(scope))

    def test_input_item_must_belong_to_turn(self):
        turn = TurnContext("conversation", 3)
        scope = replace(turn.scope, item_id="input")
        self.assertFalse(turn.accepts(scope))
        turn.input_item_ids.add("input")
        self.assertTrue(turn.accepts(scope))

    def test_audio_keeps_device_input_and_output_rates(self):
        self.assertEqual(AudioChunk(bytes(32000), INPUT_FORMAT).duration_seconds, 1)
        self.assertEqual(AudioChunk(bytes(48000), OUTPUT_FORMAT).duration_seconds, 1)
        with self.assertRaises(ValueError):
            AudioChunk(b"x")
        with self.assertRaises(ValueError):
            AudioFormat(0)

    def test_schema_is_copied_and_nested_constraints_are_validated(self):
        schema = {"type": "object", "properties": {
            "target": {"type": "object", "properties": {
                "level": {"type": "integer", "minimum": 0, "maximum": 100}
            }, "required": ["level"], "additionalProperties": False}
        }, "required": ["target"]}
        tool = CanonicalTool("set_level", "Set level", schema)
        schema["properties"].clear()
        tool.validate_arguments({"target": {"level": 30}})
        with self.assertRaises(ValidationError):
            tool.validate_arguments({"target": {"level": 200}})

    def test_missing_result_never_permits_success(self):
        for status in ("unknown", "accepted", "failed", "partial"):
            self.assertFalse(CanonicalToolResult("exec", status).permits_success_confirmation)
        self.assertTrue(CanonicalToolResult("exec", "completed").permits_success_confirmation)
        with self.assertRaises(ValueError):
            CanonicalToolResult("exec", "accepted", verified=True)

    def test_missing_latency_is_not_zero(self):
        turn = TurnContext("conversation", 1)
        turn.mark("speech_end")
        self.assertIsNone(turn.latency_ms("speech_end", "audio"))

    def test_sensitive_content_is_not_in_representations(self):
        self.assertNotIn("private", repr(ProviderSession("private")))
        self.assertNotIn("private", repr(CanonicalToolResult("exec", "unknown", detail="private")))


if __name__ == "__main__":
    unittest.main()
