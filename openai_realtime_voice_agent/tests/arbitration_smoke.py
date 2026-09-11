import asyncio
import unittest

from app.core.arbitration import ControlArbiter, TranscriptGate
from app.control_intent_router import EntityCatalog, EntityInfo


class ArbitrationTests(unittest.TestCase):
    def setUp(self):
        self.arbiter = ControlArbiter(EntityCatalog([
            EntityInfo("卧室吸顶灯", "light", "卧室"),
            EntityInfo("客厅吊灯", "light", "卧室"),
        ]))

    def test_spoken_name_overrides_inherited_area(self):
        result = self.arbiter.decide("打开卧室的灯", "HassTurnOn", {"area": "卧室"})
        self.assertTrue(result.allowed)
        self.assertEqual(result.arguments["name"], "卧室吸顶灯")
        self.assertNotIn("area", result.arguments)

    def test_ambiguous_bare_light_is_not_area_action(self):
        self.assertFalse(self.arbiter.decide("打开灯", "HassTurnOn", {"area": "卧室"}).allowed)

    def test_explicit_all_is_separate(self):
        result = self.arbiter.decide("关闭卧室所有灯", "HassTurnOff", {"area": "卧室"})
        self.assertTrue(result.allowed)
        self.assertNotIn("name", result.arguments)

    def test_missing_temperature_rejected_even_if_brightness_present(self):
        result = self.arbiter.decide("客厅灯调暖，亮度30%", "HassLightSet", {"brightness": 30})
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "requested_light_effect_missing")

    def test_name_only_setting_is_not_success(self):
        self.assertFalse(self.arbiter.decide("客厅灯调暖", "HassLightSet", {"name": "客厅吊灯"}).allowed)

    def test_temperature_range_validated(self):
        attrs = {"min_color_temp_kelvin": 3003, "max_color_temp_kelvin": 5988}
        for value, allowed in ((2700, False), (3500, True)):
            result = self.arbiter.decide("客厅灯色温3500开尔文", "HassLightSet", {"temperature": value}, attrs)
            self.assertEqual(result.allowed, allowed)

    def test_scheduling_never_executes_immediately(self):
        self.assertFalse(self.arbiter.decide("10分钟后关闭客厅灯", "HassTurnOff", {}).allowed)

    def test_negation_never_executes(self):
        self.assertFalse(self.arbiter.decide("不要关闭客厅灯", "HassTurnOff", {}).allowed)

    def test_in_range_but_wrong_spoken_value_is_rejected(self):
        attrs = {"min_color_temp_kelvin": 3003, "max_color_temp_kelvin": 5988}
        result = self.arbiter.decide("客厅灯色温三千五百开尔文", "HassLightSet", {"temperature": 4000}, attrs)
        self.assertEqual(result.reason, "spoken_parameter_mismatch")

    def test_combined_chinese_values(self):
        attrs = {"min_color_temp_kelvin": 3003, "max_color_temp_kelvin": 5988}
        result = self.arbiter.decide("客厅灯亮度百分之三十，色温三千五百开尔文", "HassLightSet",
                                     {"brightness": 30, "temperature": 3500}, attrs)
        self.assertTrue(result.allowed)
        self.assertEqual(result.arguments["domain"], ["light"])

    def test_missing_value_and_query_never_authorize_model_write(self):
        for text in ("卧室灯亮度", "查一下卧室灯亮度", "卧室灯色温"):
            for value in (1, 30, 80):
                result = self.arbiter.decide(text, "HassLightSet", {"brightness": value})
                self.assertFalse(result.allowed, text)

    def test_relative_brightness_uses_live_state(self):
        result = self.arbiter.decide("客厅灯调暗一点", "HassLightSet", {"brightness": 30}, {"brightness": 204})
        self.assertEqual(result.arguments["brightness"], 70)

    def test_failed_decision_does_not_establish_followup_target(self):
        self.arbiter.decide("打开客厅灯", "HassTurnOn", {})
        self.assertFalse(self.arbiter.decide("关闭它", "HassTurnOff", {}).allowed)
        self.arbiter.record_completed({"name": "客厅吊灯"})
        self.assertTrue(self.arbiter.decide("关闭它", "HassTurnOff", {}).allowed)

    def test_query_does_not_authorize_on_off(self):
        self.assertFalse(self.arbiter.decide("客厅吊灯现在是什么状态", "HassTurnOn", {}).allowed)

    def test_alias_is_name_priority(self):
        arbiter = ControlArbiter(EntityCatalog([EntityInfo("客厅吊灯", "light", "卧室", aliases=("大灯",))]))
        self.assertEqual(arbiter.decide("打开大灯", "HassTurnOn", {"area": "卧室"}).arguments["name"], "客厅吊灯")


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_wait_does_not_block_transcription_delivery(self):
        gate = TranscriptGate()
        pending = asyncio.create_task(gate.wait())
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        gate.finish("打开灯")
        self.assertEqual(await pending, "打开灯")

    async def test_missing_transcript_does_not_authorize_writes(self):
        self.assertEqual(await TranscriptGate().wait(0.001), "")
