import asyncio
import unittest

from app.core.arbitration import ControlArbiter, TranscriptGate
from app.core.control_dispatch import ControlDispatch
from app.control_intent_router import EntityCatalog, EntityInfo
from app.tools.registry import ToolRegistry
from app.tools.execution import ExecutionLedger
from app.tools.schema import CanonicalTool, CanonicalToolRequest


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.registry = ToolRegistry()
        async def backend(name, args):
            self.calls.append(args)
            return {"response_type": "action_done", "data": {"success": [{"id": "light.a"}]}}
        for name in ("HassTurnOn", "HassLightSet"):
            self.registry.register_backend(CanonicalTool(name, "", {"type": "object"}), backend)
        self.ledger = ExecutionLedger(self.registry, "c", "t")
        self.dispatch = ControlDispatch(ControlArbiter(EntityCatalog([EntityInfo("客厅吊灯", "light", "卧室")])))

    async def test_write_waits_for_final_text(self):
        gate = TranscriptGate()
        req = CanonicalToolRequest("c", "t", "a", "HassTurnOn", {"area": "卧室"})
        pending = asyncio.create_task(self.dispatch.execute(req, self.ledger, gate, lambda: True))
        await asyncio.sleep(0)
        self.assertEqual(self.calls, [])
        gate.finish("打开客厅吊灯")
        self.assertEqual((await pending).result.status, "completed")
        self.assertEqual(self.calls, [{"name": "客厅吊灯", "domain": ["light"]}])

    async def test_changed_turn_cannot_release_waiting_write(self):
        gate = TranscriptGate()
        gate.finish("打开客厅吊灯")
        req = CanonicalToolRequest("c", "t", "a", "HassTurnOn", {})
        result = await self.dispatch.execute(req, self.ledger, gate, lambda: False)
        self.assertEqual(result.result.status, "failed")
        self.assertEqual(self.calls, [])

    async def test_live_range_load_and_duplicate_share_execution(self):
        async def attributes(name):
            self.assertEqual(name, "客厅吊灯")
            return {"min_color_temp_kelvin": 3003, "max_color_temp_kelvin": 5988}
        self.dispatch.attributes = attributes
        gate = TranscriptGate()
        gate.finish("客厅吊灯色温3500开尔文")
        for call_id in ("a", "b"):
            req = CanonicalToolRequest("c", "t", call_id, "HassLightSet", {"temperature": 3500})
            result = await self.dispatch.execute(req, self.ledger, gate, lambda: True)
            self.assertEqual(result.result.status, "completed")
        self.assertEqual(len(self.calls), 1)

    async def test_state_failure_never_dispatches(self):
        async def attributes(name):
            raise PermissionError()
        self.dispatch.attributes = attributes
        gate = TranscriptGate()
        gate.finish("客厅吊灯调暖一点")
        req = CanonicalToolRequest("c", "t", "a", "HassLightSet", {"temperature": 3500})
        result = await self.dispatch.execute(req, self.ledger, gate, lambda: True)
        self.assertEqual(result.result.detail, "entity_state_unavailable")
        self.assertEqual(self.calls, [])

    async def test_relative_duplicate_uses_initial_state_once(self):
        reads = []
        async def attributes(name):
            reads.append(name)
            return {"brightness": 204 if len(reads) == 1 else 178}
        self.dispatch.attributes = attributes
        gate = TranscriptGate()
        gate.finish("客厅吊灯调暗一点")
        for call_id, proposed in (("a", 30), ("b", 50)):
            result = await self.dispatch.execute(CanonicalToolRequest("c", "t", call_id, "HassLightSet",
                {"brightness": proposed}), self.ledger, gate, lambda: True)
            self.assertEqual(result.result.status, "completed")
        self.assertEqual(len(reads), 1)
        self.assertEqual(self.calls, [{"name": "客厅吊灯", "brightness": 70, "domain": ["light"]}])

    async def test_group_range_must_satisfy_every_light(self):
        self.dispatch.arbiter = ControlArbiter(EntityCatalog([
            EntityInfo("客厅吊灯", "light", "卧室"), EntityInfo("卧室吸顶灯", "light", "卧室")]))
        async def attributes(name):
            return {"min_color_temp_kelvin": 3003 if name == "客厅吊灯" else 4000,
                    "max_color_temp_kelvin": 5988}
        self.dispatch.attributes = attributes
        gate = TranscriptGate()
        gate.finish("卧室全部灯色温3500开尔文")
        result = await self.dispatch.execute(CanonicalToolRequest("c", "t", "a", "HassLightSet",
            {"area": "卧室", "temperature": 3500}), self.ledger, gate, lambda: True)
        self.assertEqual(result.result.detail, "color_temperature_out_of_range")
        self.assertEqual(self.calls, [])
