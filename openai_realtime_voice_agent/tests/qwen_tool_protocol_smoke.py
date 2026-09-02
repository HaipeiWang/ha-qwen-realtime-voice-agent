"""Small protocol regression suite for the native Qwen tool bridge."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

# The Add-on image flattens the source package into /app, while the checkout
# keeps it under <repo>/app. Import the exact adjacent source in both layouts;
# never fall through to an unrelated site-packages package named ``app``.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MODULE_DIR = (
    _PROJECT_ROOT / "app"
    if (_PROJECT_ROOT / "app" / "qwen_realtime.py").exists()
    else _PROJECT_ROOT
)
sys.path.insert(0, str(_MODULE_DIR))

from qwen_realtime import QwenRealtimeLLMService


class _WebSocketStub:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


class QwenToolProtocolSmokeTest(unittest.TestCase):
    def test_flat_pipecat_tool_is_converted_to_native_qwen_shape(self):
        converted = QwenRealtimeLLMService._to_qwen_tool({
            "type": "function",
            "name": "HassTurnOff",
            "description": "Turn a Home Assistant entity off.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        })

        self.assertEqual(converted["type"], "function")
        self.assertNotIn("name", converted)
        self.assertEqual(converted["function"]["name"], "HassTurnOff")
        self.assertEqual(
            converted["function"]["parameters"]["required"], ["name"]
        )
        self.assertIn("必须调用", converted["function"]["description"])

    def test_invalid_flat_tool_is_rejected(self):
        with self.assertRaises(ValueError):
            QwenRealtimeLLMService._to_qwen_tool({
                "type": "function",
                "name": "",
                "parameters": {"type": "object"},
            })

    def test_session_echo_must_contain_every_expected_tool(self):
        service = object.__new__(QwenRealtimeLLMService)
        service._expected_tool_names = {"HassTurnOn", "HassTurnOff"}
        service._expected_qwen_tools = [
            {"type": "function", "function": {"name": "HassTurnOn"}},
            {"type": "function", "function": {"name": "HassTurnOff"}},
        ]
        service._tools_ready = False
        service._tool_debug = False
        service.has_function = lambda name: True

        self.assertTrue(service._validate_server_tools({"tools": [
            {"type": "function", "function": {
                "name": "HassTurnOn",
                "parameters": {"type": "object", "properties": {}},
            }},
            {"type": "function", "function": {
                "name": "HassTurnOff",
                "parameters": {"type": "object", "properties": {}},
            }},
        ]}))
        self.assertTrue(service._tools_ready)

        self.assertFalse(service._validate_server_tools({"tools": [
            {"type": "function", "function": {
                "name": "HassTurnOn",
                "parameters": {"type": "object", "properties": {}},
            }},
        ]}))
        self.assertFalse(service._tools_ready)

    def test_duplicate_response_create_is_suppressed_while_inflight(self):
        async def scenario():
            service = object.__new__(QwenRealtimeLLMService)
            service._skip_next_response_create = False
            service._response_cancel_pending = False
            service._response_after_cancel_pending = False
            service._response_create_inflight = False
            service._response_done_generation = 3
            service._api_session_ready = True
            service._pending_tool_call_ids = set()
            service._current_assistant_response = None
            service._pending_response_after_tool_result = False
            service._response_boundary_open = False
            service.push_frame = AsyncMock()
            service.start_processing_metrics = AsyncMock()
            service.start_ttfb_metrics = AsyncMock()
            service._ws_send_checked = AsyncMock()

            await service._create_response()
            await service._create_response()

            self.assertTrue(service._response_create_inflight)
            self.assertIsNone(service._response_done_generation)
            self.assertEqual(service._ws_send_checked.await_count, 1)

        asyncio.run(scenario())

    def test_wake_generation_discards_only_uncommitted_turn_state(self):
        service = object.__new__(QwenRealtimeLLMService)
        service._turn_generation = 7
        service._speech_started_generation = 7
        service._wake_guard_until = 0.0
        service._last_user_transcript = "上一轮的请求"
        service._tool_call_seen_for_turn = True
        service._det_executed_results = {"HassTurnOn": "old-result"}
        service._transcript_gate_task = None

        service.begin_wake_turn(0)

        self.assertEqual(service._turn_generation, 8)
        self.assertIsNone(service._speech_started_generation)
        self.assertEqual(service._last_user_transcript, "")
        self.assertFalse(service._tool_call_seen_for_turn)
        self.assertEqual(service._det_executed_results, {})
        self.assertFalse(service._wake_guard_active())

    def test_qwen_audio_model_uses_its_own_voice_profile(self):
        self.assertTrue(
            QwenRealtimeLLMService._is_qwen_audio_realtime_model(
                "qwen-audio-3.0-realtime-flash"
            )
        )
        self.assertFalse(
            QwenRealtimeLLMService._is_qwen_audio_realtime_model(
                "qwen3.5-omni-flash-realtime"
            )
        )
        self.assertEqual(
            QwenRealtimeLLMService._validated_voice_for_model(
                "qwen-audio-3.0-realtime-flash", "Tina"
            ),
            "longanqian",
        )
        self.assertEqual(
            QwenRealtimeLLMService._validated_voice_for_model(
                "qwen-audio-3.0-realtime-plus", "longanlingxi"
            ),
            "longanlingxi",
        )
        self.assertEqual(
            QwenRealtimeLLMService._validated_voice_for_model(
                "qwen-audio-3.0-realtime-flash",
                "qwen-audio-3.0-realtime-flash-myvoice-1234",
            ),
            "qwen-audio-3.0-realtime-flash-myvoice-1234",
        )

    def test_omni_voice_profile_rejects_audio_voice(self):
        self.assertEqual(
            QwenRealtimeLLMService._validated_voice_for_model(
                "qwen3.5-omni-flash-realtime", "Ethan"
            ),
            "Ethan",
        )
        self.assertEqual(
            QwenRealtimeLLMService._validated_voice_for_model(
                "qwen3.5-omni-flash-realtime", "Mione"
            ),
            "Mione",
        )
        self.assertEqual(
            QwenRealtimeLLMService._validated_voice_for_model(
                "qwen3.5-omni-plus-realtime", "Eliška"
            ),
            "Eliška",
        )
        self.assertEqual(
            QwenRealtimeLLMService._validated_voice_for_model(
                "qwen3.5-omni-plus-realtime", "longanqian"
            ),
            "Tina",
        )
        self.assertEqual(
            QwenRealtimeLLMService._validated_voice_for_model(
                "qwen-audio-3.0-realtime-flash",
                "qwen-audio-3.0-realtime-flash",
            ),
            "longanqian",
        )

    def test_session_echo_rejects_duplicate_or_unregistered_local_handler(self):
        service = object.__new__(QwenRealtimeLLMService)
        service._expected_tool_names = {"HassTurnOn", "HassTurnOff"}
        service._expected_qwen_tools = [
            {"type": "function", "function": {"name": "HassTurnOn"}},
            {"type": "function", "function": {"name": "HassTurnOff"}},
        ]
        service._tools_ready = False
        service._tool_debug = False
        valid_on = {"type": "function", "function": {
            "name": "HassTurnOn",
            "parameters": {"type": "object", "properties": {}},
        }}
        valid_off = {"type": "function", "function": {
            "name": "HassTurnOff",
            "parameters": {"type": "object", "properties": {}},
        }}

        service.has_function = lambda name: name == "HassTurnOn"
        self.assertFalse(service._validate_server_tools({"tools": [valid_on, valid_off]}))
        self.assertFalse(service._tools_ready)

        service.has_function = lambda name: True
        self.assertFalse(service._validate_server_tools({"tools": [valid_on, valid_on]}))
        self.assertFalse(service._tools_ready)

    def test_inflight_interruption_cancels_and_queues_next_turn(self):
        async def scenario():
            service = object.__new__(QwenRealtimeLLMService)
            service._current_assistant_response = None
            service._response_create_inflight = True
            service._response_cancel_pending = False
            service._response_after_cancel_pending = False
            service._response_boundary_open = True
            service._response_cancel_watchdog_task = None
            service._qwen_tts_active = False
            service._ws_send_checked = AsyncMock()
            service.push_frame = AsyncMock()
            service._start_cancel_response_watchdog = lambda: None

            await service._handle_interruption()
            self.assertTrue(service._response_cancel_pending)
            service._api_session_ready = True
            service._skip_next_response_create = False
            service._pending_tool_call_ids = set()
            service._pending_response_after_tool_result = False
            service.start_processing_metrics = AsyncMock()
            service.start_ttfb_metrics = AsyncMock()
            await service._create_response()

            self.assertTrue(service._response_after_cancel_pending)
            self.assertEqual(
                service._ws_send_checked.await_args_list[0].args[0],
                {"type": "response.cancel"},
            )
            self.assertEqual(service._ws_send_checked.await_count, 1)

        asyncio.run(scenario())

    def test_tool_result_send_failure_surfaces_connection_error(self):
        async def scenario():
            service = object.__new__(QwenRealtimeLLMService)
            service._pending_tool_call_ids = {"call-1"}
            service._tool_call_generations = {"call-1": 4}
            service._provider_generation = 4
            service._provider_state = "active"
            service._conversation_active = True
            service._api_session_ready = True
            service._skip_next_response_create = False
            service._ws_send_checked = AsyncMock(side_effect=ConnectionError("closed"))
            service._reset_failed_response = AsyncMock()
            service.push_error = AsyncMock()

            await service._send_tool_result("call-1", {"success": True})

            service._reset_failed_response.assert_awaited_once()
            service.push_error.assert_awaited_once()
            self.assertTrue(service._skip_next_response_create)

        asyncio.run(scenario())

    def test_pcm_never_opens_provider_without_wake(self):
        async def scenario():
            service = object.__new__(QwenRealtimeLLMService)
            service._qwen_first_input_logged = False
            service._api_session_ready = False
            service._websocket = None
            service._conversation_active = False
            service._provider_state = "disconnected"
            service._pending_input_audio = []
            service._pending_input_audio_bytes = 0
            service._pending_input_audio_limit = 160000
            service._send_audio_bytes = AsyncMock()
            service.open_conversation = AsyncMock()

            await service._send_user_audio(SimpleNamespace(audio=b"\0" * 512))

            service.open_conversation.assert_not_awaited()
            service._send_audio_bytes.assert_not_awaited()
            self.assertEqual(service._pending_input_audio_bytes, 0)

        asyncio.run(scenario())

    def test_tool_result_from_old_generation_is_discarded(self):
        async def scenario():
            service = object.__new__(QwenRealtimeLLMService)
            service._tool_call_generations = {"old-call": 2}
            service._provider_generation = 3
            service._provider_state = "active"
            service._conversation_active = True
            service._api_session_ready = True
            service._pending_tool_call_ids = {"old-call"}
            service._ws_send_checked = AsyncMock()

            sent = await service._send_tool_result("old-call", {"success": True})

            self.assertFalse(sent)
            service._ws_send_checked.assert_not_awaited()
            self.assertNotIn("old-call", service._pending_tool_call_ids)

        asyncio.run(scenario())

    def test_checked_send_really_reaches_socket(self):
        service = object.__new__(QwenRealtimeLLMService)
        service._disconnecting = False
        service._websocket = _WebSocketStub()

        asyncio.run(service._ws_send_checked({"type": "response.create"}))
        self.assertEqual(
            service._websocket.messages, [{"type": "response.create"}]
        )

    def test_checked_send_rejects_disconnected_socket(self):
        service = object.__new__(QwenRealtimeLLMService)
        service._disconnecting = False
        service._websocket = None

        with self.assertRaises(ConnectionError):
            asyncio.run(service._ws_send_checked({"type": "response.create"}))

    def test_idle_keepalive_is_partial_update_and_skips_active_turn(self):
        async def scenario():
            service = object.__new__(QwenRealtimeLLMService)
            service._current_assistant_response = None
            service._response_create_inflight = False
            service._response_cancel_pending = False
            service._pending_tool_call_ids = set()
            service._session_instructions = "keep tools mandatory"
            service._ws_send_checked = AsyncMock()

            self.assertTrue(await service.refresh_idle_session())
            service._ws_send_checked.assert_awaited_once_with({
                "type": "session.update",
                "session": {"instructions": "keep tools mandatory"},
            })

            service._response_create_inflight = True
            self.assertFalse(await service.refresh_idle_session())
            self.assertEqual(service._ws_send_checked.await_count, 1)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
