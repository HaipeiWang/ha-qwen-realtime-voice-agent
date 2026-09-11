"""Current runtime integration: FakeProvider -> Core -> tools and ordered audio."""

import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from pipecat.frames.frames import InputAudioRawFrame, TTSAudioRawFrame, LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from app.core.service import RealtimeCoreService
from app.core.events import CanonicalEvent, EventKind, AudioChunk, ResponseStatus
from app.core.frames import ResponseDoneFrame
from app.core.arbitration import ControlArbiter
from app.core.control_dispatch import ControlDispatch
from app.control_intent_router import EntityCatalog, EntityInfo
from app.providers.base import ProviderSession
from core_fake_provider import FakeProvider
from app.tools.schema import CanonicalTool


class ReadyFake(FakeProvider):
    async def configure_session(self, session):
        await super().configure_session(session)
        await self.events.put(CanonicalEvent(EventKind.READY, self.scope,
            tool_names=tuple(t.name for t in session.tools), tools_valid=True))


class CaptureCore(RealtimeCoreService):
    def __init__(self, **kwargs):
        self.frames, self.errors = [], []
        super().__init__(**kwargs)

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.frames.append(frame)

    async def push_error(self, **kwargs):
        self.errors.append(kwargs)


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = ReadyFake()
        self.calls = []
        definition = CanonicalTool("HassTurnOn", "", {"type": "object"})
        self.core = CaptureCore(provider_factory=lambda: self.provider,
            session=ProviderSession("test", (definition,)), router_enabled=False)
        self.core.control_dispatch = ControlDispatch(ControlArbiter(EntityCatalog([
            EntityInfo("卧室吸顶灯", "light", "卧室"), EntityInfo("客厅吊灯", "light", "卧室")
        ])))
        async def handler(params):
            self.calls.append(params.arguments)
            await params.result_callback({"response_type": "action_done", "data": {"success": [{"id": "light.a"}]}})
        self.core.register_function("HassTurnOn", handler)
        await self.core.open_conversation()

    async def asyncTearDown(self):
        await self.core.close_conversation("test finished")
        if self.core.turn.jobs:
            await asyncio.gather(*tuple(self.core.turn.jobs), return_exceptions=True)

    async def input(self, text="打开卧室的灯", item="i"):
        scope = replace(self.core.turn.scope, item_id=item)
        await self.core.handle_event(CanonicalEvent(EventKind.SPEECH_STARTED, scope))
        await self.core.handle_event(CanonicalEvent(EventKind.SPEECH_STOPPED, scope))
        if text is not None:
            await self.core.handle_event(CanonicalEvent(EventKind.TRANSCRIPT_FINAL, scope, text=text))
        return scope

    async def response(self, response_id="r"):
        scope = replace(self.core.turn.scope, response_id=response_id)
        await self.core.handle_event(CanonicalEvent(EventKind.RESPONSE_STARTED, scope))
        return scope

    async def finish(self, scope):
        await self.core.handle_event(CanonicalEvent(EventKind.RESPONSE_ENDED, scope, status=ResponseStatus.COMPLETED))

    async def join_tools(self, turn=None):
        jobs = tuple((turn or self.core.turn).jobs)
        if jobs:
            await asyncio.wait_for(asyncio.gather(*jobs), 1)

    async def test_startup_and_context_do_not_generate(self):
        await self.core.process_frame(LLMContextFrame(LLMContext([])), FrameDirection.DOWNSTREAM)
        self.assertFalse(any(c[0] == "respond" for c in self.provider.commands))
        self.assertTrue(self.core.is_ready)

    async def test_tool_before_transcript_waits_without_blocking_reader(self):
        item = await self.input(None)
        scope = await self.response()
        await self.core.handle_event(CanonicalEvent(EventKind.TOOL_CALL, scope, call_id="a",
            tool_name="HassTurnOn", arguments={"area": "卧室"}))
        await asyncio.sleep(0)
        self.assertEqual(self.calls, [])
        await self.core.handle_event(CanonicalEvent(EventKind.TRANSCRIPT_FINAL, item, text="打开卧室的灯"))
        await self.join_tools()
        self.assertEqual(self.calls, [{"name": "卧室吸顶灯", "domain": ["light"]}])
        result = [c for c in self.provider.commands if c[0] == "tool_result"][0][2]
        self.assertEqual(result.data["executed_arguments"]["name"], "卧室吸顶灯")
        self.assertFalse(any(c[0] == "respond" for c in self.provider.commands))
        await self.finish(scope)
        self.assertEqual(sum(c[0] == "respond" for c in self.provider.commands), 1)

    async def test_duplicate_calls_share_one_execution(self):
        await self.input()
        scope = await self.response()
        for call_id in ("a", "b", "a"):
            await self.core.handle_event(CanonicalEvent(EventKind.TOOL_CALL, scope, call_id=call_id,
                tool_name="HassTurnOn", arguments={"area": "卧室"}))
        await self.join_tools()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(c[0] == "tool_result" for c in self.provider.commands), 2)

    async def test_missing_transcript_never_executes(self):
        await self.input("")
        scope = await self.response()
        await self.core.handle_event(CanonicalEvent(EventKind.TOOL_CALL, scope, call_id="a",
            tool_name="HassTurnOn", arguments={"area": "卧室"}))
        await self.join_tools()
        self.assertEqual(self.calls, [])
        result = [c for c in self.provider.commands if c[0] == "tool_result"][0][2]
        self.assertEqual(result.status, "failed")

    async def test_wrong_input_item_cannot_release_write_gate(self):
        await self.input(None)
        bad = replace(self.core.turn.scope, item_id="old")
        self.assertFalse(await self.core.handle_event(CanonicalEvent(EventKind.TRANSCRIPT_FINAL, bad, text="打开客厅吊灯")))
        self.assertEqual(self.core.control_evidence.text, "")

    async def test_old_response_audio_does_not_enter_new_wake(self):
        scope = await self.response()
        self.core.begin_wake_turn()
        self.assertFalse(await self.core.handle_event(CanonicalEvent(EventKind.AUDIO, scope, audio=AudioChunk(b"\0\0"))))
        self.assertFalse(any(isinstance(f, TTSAudioRawFrame) for f in self.core.frames))

    async def test_generated_audio_requires_scoped_drain_then_mic(self):
        await self.input("你是谁")
        scope = await self.response()
        await self.core.handle_event(CanonicalEvent(EventKind.AUDIO, scope, audio=AudioChunk(b"\0\0")))
        await self.finish(scope)
        old_turn = self.core.turn
        await self.core.on_output_drained(replace(scope, response_id="old"))
        self.assertFalse(self.core._awaiting_mic)
        await self.core.on_output_drained(scope)
        self.assertIs(self.core.turn, old_turn)
        self.assertIsNone(self.core.lifecycle.follow_up_deadline)
        await self.core.process_frame(InputAudioRawFrame(audio=b"\0\0", sample_rate=16000, num_channels=1), FrameDirection.DOWNSTREAM)
        self.assertIsNot(self.core.turn, old_turn)
        self.assertEqual(self.provider.scope.turn_id, self.core.turn.scope.turn_id)

    async def test_same_connection_new_wake_has_new_turn_identity(self):
        old = self.core.turn.scope
        self.core.begin_wake_turn()
        self.assertEqual(old.conversation_id, self.core.turn.scope.conversation_id)
        self.assertNotEqual(old.turn_id, self.core.turn.scope.turn_id)
        self.assertEqual(self.provider.scope, self.core.turn.scope)

    async def test_reconnect_does_not_reuse_conversation_or_generate(self):
        old = self.core.turn.scope
        self.core.provider_factory = ReadyFake
        await self.core.reset_conversation()
        self.assertNotEqual(old.conversation_id, self.core.turn.scope.conversation_id)
        self.assertFalse(any(c[0] == "respond" for c in self.core.provider.commands))

    async def test_stop_boundary_recorded_but_live_enforcement_deferred(self):
        await self.input("你是谁")
        await self.core.cancel_response()
        self.assertTrue(self.core.turn.cancelled)
        late = replace(self.core.turn.scope, item_id="late")
        self.assertTrue(await self.core.handle_event(CanonicalEvent(EventKind.SPEECH_STARTED, late)))
        self.assertTrue(self.core.turn.cancelled)
        self.core.enforce_cancel_boundary = True
        self.assertFalse(await self.core.handle_event(CanonicalEvent(EventKind.SPEECH_STARTED, late)))

    async def test_regeneration_cannot_dispatch_ha(self):
        await self.input()
        self.core.turn.evidence.regeneration_active = True
        result = await self.core.execute_tool(self.core.turn, "a", "HassTurnOn", {"name": "卧室吸顶灯"})
        self.assertEqual(result.result.detail, "confirmation_replay_no_tools")
        self.assertEqual(self.calls, [])

    async def test_no_overlapping_response_audio(self):
        await self.response("first")
        second = replace(self.core.turn.scope, response_id="second")
        self.assertFalse(await self.core.handle_event(CanonicalEvent(EventKind.RESPONSE_STARTED, second)))
        self.assertFalse(await self.core.handle_event(CanonicalEvent(EventKind.AUDIO, second, audio=AudioChunk(b"\0\0"))))

    async def test_delayed_tool_result_stays_with_original_turn(self):
        released, started = asyncio.Event(), asyncio.Event()
        definition = CanonicalTool("Slow", "", {"type": "object"})
        self.core.tool_definitions["Slow"] = definition
        async def handler(params):
            started.set()
            await released.wait()
            await params.result_callback({"success": True, "data": {"state": "on"}})
        self.core.register_function("Slow", handler)
        old = self.core.turn
        self.core._launch_tool(old, "slow", "Slow", {})
        await started.wait()
        self.core.begin_wake_turn()
        released.set()
        await self.join_tools(old)
        self.assertFalse(any(c[0] == "tool_result" for c in self.provider.commands))
        self.assertEqual(len(old.evidence.outcomes), 1)
        self.assertEqual(self.core.control_evidence.outcomes, {})

    async def test_new_wake_clears_input_before_flushing_first_word(self):
        self.core.begin_wake_turn()
        await self.core.process_frame(InputAudioRawFrame(audio=b"\1\0" * 8, sample_rate=16000, num_channels=1), FrameDirection.DOWNSTREAM)
        before = len(self.provider.commands)
        await self.core.open_conversation()
        commands = self.provider.commands[before:]
        self.assertEqual([c[0] for c in commands], ["instructions", "clear", "audio"])
        self.assertEqual(commands[-1][1].data, b"\1\0" * 8)

    async def test_new_wake_while_reply_active_can_accept_new_input(self):
        await self.response()
        self.core.begin_wake_turn()
        await self.core.open_conversation()
        await self.input("你是谁", "new")
        self.assertEqual(self.core.control_evidence.text, "你是谁")
        self.assertTrue(any(c[0] == "cancel" for c in self.provider.commands))

    async def test_audio_generating_done_still_blocks_echo_response(self):
        scope = await self.response()
        await self.core.handle_event(CanonicalEvent(EventKind.AUDIO, scope, audio=AudioChunk(b"\0\0")))
        await self.finish(scope)
        late = replace(self.core.turn.scope, response_id="echo")
        self.assertFalse(await self.core.handle_event(CanonicalEvent(EventKind.RESPONSE_STARTED, late)))
        self.assertTrue(self.core.playback_pending)

    async def test_missing_response_end_recovers_to_closed(self):
        self.core.response_timeout_s = 0.01
        await self.response()
        watchdog = self.core._active_response.watchdog
        await asyncio.wait_for(asyncio.shield(watchdog), 1)
        self.assertFalse(self.core.conversation_active)
        self.assertIsNone(self.core.provider)
        self.assertTrue(self.core.errors)

    async def test_unverified_registration_never_dispatches(self):
        await self.input()
        scope = await self.response()
        self.core.tools_ready = False
        await self.core.handle_event(CanonicalEvent(EventKind.TOOL_CALL, scope, call_id="a",
            tool_name="HassTurnOn", arguments={"name": "卧室吸顶灯"}))
        await self.join_tools()
        self.assertFalse(self.calls)
        result = [c for c in self.provider.commands if c[0] == "tool_result"][0][2]
        self.assertEqual(result.detail, "tool_registration_not_verified")

    async def test_runtime_core_has_no_vendor_imports(self):
        root = Path(__file__).parents[1] / "app"
        self.assertFalse((root / "providers/qwen_compat.py").exists())
        self.assertFalse((root / "qwen_realtime.py").exists())
        text = (root / "core/service.py").read_text()
        for forbidden in ("providers.qwen", "openai.realtime", "QwenCompatibilitySocket", "provider._"):
            self.assertNotIn(forbidden, text)
