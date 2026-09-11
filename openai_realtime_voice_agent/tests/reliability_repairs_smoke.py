"""Regression tests for control, clarification and reply reliability."""
import json
import asyncio
import unittest
from unittest.mock import patch

from app.control_intent_router import ControlIntentRouter, EntityCatalog, EntityInfo
from app.core.arbitration import ControlArbiter, TranscriptGate
from app.core.clarification import PendingClarification
from app.core.confirmation import ConfirmationReview
from app.tools.live_context import normalize_live_context
from app.tools.registry import normalize_result
from app.tools.schema import CanonicalToolRequest
import control_dispatch_smoke
import core_service_smoke
from dataclasses import replace
from app.core.events import CanonicalEvent, EventKind, AudioChunk
from pipecat.frames.frames import InputAudioRawFrame


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.catalog = EntityCatalog([EntityInfo("卧室吸顶灯", "light", "卧室"),
                                      EntityInfo("客厅吊灯", "light", "卧室")])
        self.arbiter = ControlArbiter(self.catalog)

    def test_router_queries_and_missing_values(self):
        router = ControlIntentRouter(self.catalog)
        for text in ("卧室灯亮度", "查一下卧室灯亮度", "查一下卧室灯亮度百分之八十"):
            self.assertIsNone(router.resolve(text), text)
        self.assertEqual(router.resolve("卧室灯亮度百分之八十").arguments["brightness"], 80)

    def test_live_context_uses_explicit_units_only_for_lights(self):
        for raw, percent in ((76, 30), (3, 1), (204, 80)):
            context = "Live Context: An overview\n- names: 卧室吸顶灯\n  domain: light\n  attributes:\n    brightness: '" + str(raw) + "'\n"
            result = json.loads(normalize_live_context(json.dumps({"success": True, "result": context})))
            self.assertIn(f"brightness_percent: {percent}", result["result"])
            self.assertNotIn("    brightness:", result["result"])
        other = {"domain": "sensor", "attributes": {"brightness": 76}}
        self.assertEqual(normalize_live_context(other), other)

    def test_scheduling_false_promises_and_correct_refusal(self):
        outcome = normalize_result({"status": "failed", "detail": "scheduling_not_implemented"}, "e")
        review = ConfirmationReview()
        for text in ("我会在10分钟后打开客厅吊灯", "好的，十秒后打开客厅吊灯", "客厅吊灯未配置"):
            self.assertFalse(review.review(text, 2, [outcome], action_coverage_complete=False).allowed)
        self.assertTrue(review.review("目前无法设置定时吊灯。", 2, [outcome], action_coverage_complete=False).allowed)

    def test_no_tool_cannot_promise_a_scheduled_action(self):
        review = ConfirmationReview()
        self.assertFalse(review.review("好的，十秒后开灯。", 2, [], action_coverage_complete=False,
                                       request_text="十秒后打开客厅吊灯").allowed)

    def test_clarification_only_bare_target_and_finite_lifetime(self):
        pending = PendingClarification()
        pending.offer("打开灯", "你想打开哪盏灯？", self.arbiter, 8)
        self.assertEqual(pending.consume("客厅吊灯", self.arbiter), "打开客厅吊灯")
        self.assertEqual(pending.consume("客厅吊灯", self.arbiter), "客厅吊灯")
        for text in ("客厅吊灯亮度", "查一下客厅吊灯", "今天天气"):
            pending.offer("打开灯", "哪盏灯？", self.arbiter, 8)
            self.assertEqual(pending.consume(text, self.arbiter), text)
        pending.offer("打开灯", "哪盏灯？", self.arbiter, 8)
        with patch("app.core.clarification.time.monotonic", return_value=pending.expires + 1):
            self.assertEqual(pending.consume("客厅吊灯", self.arbiter), "客厅吊灯")

    def test_clarification_deadline_uses_speech_start_not_final_asr(self):
        pending = PendingClarification()
        pending.offer("打开灯", "哪盏灯？", self.arbiter, 8)
        started = pending.expires - 1
        with patch("app.core.clarification.time.monotonic", return_value=pending.expires + 4):
            self.assertEqual(pending.consume("客厅吊灯", self.arbiter, input_started_at=started), "打开客厅吊灯")


class DispatchRepairTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = control_dispatch_smoke.DispatchTests.asyncSetUp
    async def test_equivalent_domain_selectors_share_execution(self):
        gate = TranscriptGate()
        gate.finish("客厅吊灯亮度百分之五十")
        ids = []
        for index, extra in enumerate(({}, {"domain": "light"}, {"domain": ["light"], "area": "卧室"})):
            outcome = await self.dispatch.execute(CanonicalToolRequest("c", "t", str(index), "HassLightSet",
                {"name": "客厅吊灯", "brightness": 50, **extra}), self.ledger, gate, lambda: True)
            self.assertEqual(outcome.result.status, "completed")
            ids.append(outcome.result.execution_id)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.calls), 1)

    async def test_reproduced_queries_make_zero_backend_writes(self):
        for index, text in enumerate(("客厅吊灯亮度", "查一下客厅吊灯亮度")):
            gate = TranscriptGate()
            gate.finish(text)
            outcome = await self.dispatch.execute(CanonicalToolRequest("c", "t", str(index), "HassLightSet",
                {"brightness": 30}), self.ledger, gate, lambda: True)
            self.assertEqual(outcome.result.status, "failed")
        self.assertEqual(self.calls, [])


class CoreRepairTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = core_service_smoke.CoreTests.asyncSetUp
    asyncTearDown = core_service_smoke.CoreTests.asyncTearDown
    input = core_service_smoke.CoreTests.input
    response = core_service_smoke.CoreTests.response
    finish = core_service_smoke.CoreTests.finish
    join_tools = core_service_smoke.CoreTests.join_tools

    async def test_clarification_follows_playback_into_new_ledger(self):
        await self.input("打开灯")
        old_turn = self.core.turn
        scope = await self.response()
        await self.core.handle_event(CanonicalEvent(EventKind.TEXT_FINAL, scope, text="你想打开哪盏灯？", text_is_spoken=True))
        await self.core.handle_event(CanonicalEvent(EventKind.AUDIO, scope, audio=AudioChunk(b"\0\0")))
        await self.finish(scope)
        await self.core.on_output_drained(scope)
        self.core._wake_guard_until = 0
        await self.core._input_audio(InputAudioRawFrame(b"\0\0", 16000, 1))
        self.assertIsNot(self.core.turn, old_turn)
        await self.input("客厅吊灯", "followup")
        self.assertEqual(self.core.turn.transcript.text, "打开客厅吊灯")
        result = await self.core.execute_tool(self.core.turn, "new", "HassTurnOn", {"area": "客厅"})
        self.assertEqual(result.result.status, "completed")
        self.assertEqual(self.calls, [{"name": "客厅吊灯", "domain": ["light"]}])

    async def test_wake_and_cancel_clear_pending_target(self):
        arbiter = self.core.control_dispatch.arbiter
        for cancel in (True, False):
            self.core._clarification.offer("打开灯", "哪盏灯？", arbiter, 8)
            if cancel:
                await self.core.cancel_response()
            else:
                self.core.begin_wake_turn()
            self.assertEqual(self.core._clarification.consume("客厅吊灯", arbiter), "客厅吊灯")

    async def test_explicit_response_provider_gets_one_router_reply(self):
        self.provider.automatic_responses = False
        self.core.turn.transcript.finish("打开卧室的灯")
        self.core.turn.evidence.text = "打开卧室的灯"
        self.core._launch_tool(self.core.turn, "router", "HassTurnOn", {}, "router")
        await self.join_tools()
        self.assertEqual(sum(c[0] == "respond" for c in self.provider.commands), 1)

    async def test_router_context_does_not_queue_second_automatic_reply(self):
        await self.input()
        scope = await self.response()
        self.core._launch_tool(self.core.turn, "router", "HassTurnOn", {"name": "卧室吸顶灯"}, "router")
        await self.join_tools()
        await self.finish(scope)
        await self.core.on_output_drained(scope)
        self.assertFalse(any(c[0] == "respond" for c in self.provider.commands))

    async def test_router_result_before_automatic_start_does_not_create_reply(self):
        await self.input()
        self.core._launch_tool(self.core.turn, "router", "HassTurnOn", {}, "router")
        await self.join_tools()
        scope = await self.response()
        await self.finish(scope)
        await self.core.on_output_drained(scope)
        self.assertFalse(any(c[0] == "respond" for c in self.provider.commands))

    async def test_withheld_reply_gets_one_receipt_after_late_tool(self):
        await self.input()
        scope = await self.response()
        gate = asyncio.Event()
        original = self.core.execute_tool
        async def delayed(*args, **kwargs):
            await gate.wait()
            return await original(*args, **kwargs)
        self.core.execute_tool = delayed
        self.core._launch_tool(self.core.turn, "router", "HassTurnOn", {}, "router")
        # ConfirmationProcessor records that speech was held while a tool ran.
        self.core.turn.evidence.response_waiting_for_tools = True
        await self.finish(scope)
        await self.core.on_output_drained(scope)
        self.assertFalse(any(c[0] == "respond" for c in self.provider.commands))
        gate.set()
        await self.join_tools()
        await asyncio.sleep(0)
        self.assertEqual(sum(c[0] == "respond" for c in self.provider.commands), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.core.turn.evidence.regeneration_active)
