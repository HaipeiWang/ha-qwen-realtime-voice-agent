"""Native Qwen wire replay through the production adapter and shared Core."""

import asyncio
import json
import unittest
from core_service_smoke import CaptureCore
from app.providers.qwen import QwenRealtimeProvider, QwenConfig
from app.providers.base import ProviderSession
from app.tools.schema import CanonicalTool
from app.core.frames import ResponseDoneFrame


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.session = {}

    async def send(self, message):
        value = json.loads(message)
        self.sent.append(value)
        if value["type"] == "session.update":
            self.session.update(value["session"])
            await self.incoming.put({"type": "session.updated", "session": dict(self.session)})

    async def close(self):
        await self.incoming.put(None)

    async def __aiter__(self):
        while True:
            event = await self.incoming.get()
            if event is None:
                return
            yield json.dumps(event)
            self.incoming.task_done()

    async def emit(self, event):
        await self.incoming.put(event)
        await asyncio.wait_for(self.incoming.join(), 1)


class QwenCoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.socket = Socket()
        async def connect(**kwargs):
            await self.socket.incoming.put({"type": "session.created"})
            return self.socket
        provider = QwenRealtimeProvider(QwenConfig("test", "workspace", model="qwen-audio-3.0-realtime-plus"), connector=connect)
        tool = CanonicalTool("GetCurrentTime", "", {"type": "object"}, read_only=True)
        self.core = CaptureCore(provider_factory=lambda: provider, session=ProviderSession("test", (tool,)), router_enabled=False)
        async def current_time(params):
            await params.result_callback({"success": True, "data": {"datetime": "2026-09-09T12:00:00+08:00"}})
        self.core.register_function(tool.name, current_time)
        await self.core.open_conversation()

    async def asyncTearDown(self):
        await self.core.close_conversation("test")

    async def input_and_response(self):
        for event in (
            {"type": "input_audio_buffer.speech_started", "item_id": "i"},
            {"type": "input_audio_buffer.speech_stopped", "item_id": "i"},
            {"type": "conversation.item.input_audio_transcription.completed", "item_id": "i", "transcript": "现在几点"},
            {"type": "response.created", "response": {"id": "r"}},
        ):
            await self.socket.emit(event)

    async def test_real_adapter_preserves_core_turn_and_fact_result(self):
        await self.input_and_response()
        self.assertTrue(self.core.tools_ready)
        await self.socket.emit({"type": "response.function_call_arguments.done", "response_id": "r",
            "call_id": "time", "name": "GetCurrentTime", "arguments": "{}"})
        if self.core.turn.jobs:
            await asyncio.wait_for(asyncio.gather(*tuple(self.core.turn.jobs)), 1)
        replies = [m for m in self.socket.sent if m["type"] == "conversation.item.create"]
        self.assertEqual(len(replies), 1)
        output = json.loads(replies[0]["item"]["output"])
        self.assertEqual(output["status"], "completed")
        self.assertEqual(output["data"]["data"]["datetime"], "2026-09-09T12:00:00+08:00")
        await self.socket.emit({"type": "response.done", "response": {"id": "r", "status": "completed", "output": []}})
        done = [f for f in self.core.frames if isinstance(f, ResponseDoneFrame)][0]
        self.assertEqual(done.scope.turn_id, self.core.turn.scope.turn_id)
        self.assertEqual(sum(m["type"] == "response.create" for m in self.socket.sent), 1)

    async def test_new_wake_drops_old_wire_audio_without_relabelling(self):
        await self.input_and_response()
        self.core.begin_wake_turn()
        await self.core.open_conversation()
        count = len(self.core.frames)
        await self.socket.emit({"type": "response.audio.delta", "response_id": "r", "delta": "AAA="})
        self.assertEqual(len(self.core.frames), count)

    async def test_benign_provider_race_keeps_session_ready(self):
        await self.socket.emit({"type": "error", "error": {"code": "response_cancel_not_active"}})
        self.assertTrue(self.core.is_ready)
        self.assertEqual(self.core.errors, [])
