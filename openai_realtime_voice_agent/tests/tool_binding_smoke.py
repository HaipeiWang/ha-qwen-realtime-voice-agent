"""Exercise actual legacy service registration without connecting a provider."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pipecat.services.openai.realtime.events import SessionProperties

from app.main import SafeRealtimeLLMService
from app.tools.registry import ToolRegistry
from app.tools.schema import CanonicalTool


class BindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_control_uses_shared_result_and_dedup(self):
        with patch.dict(os.environ, {"QWEN_WORKSPACE_ID": "test", "QWEN_REGION": "cn-beijing"}):
            service = SafeRealtimeLLMService(api_key="test", model="qwen-audio-3.0-realtime-plus",
                                            session_properties=SessionProperties())
        service.tool_registry = ToolRegistry()
        service.tool_definitions = {"Write": CanonicalTool("Write", "", {"type": "object"})}
        calls = []
        results = []

        async def backend(params):
            calls.append(params.arguments)
            await params.result_callback({"response_type": "action_done", "data": {"success": [{"id": "light.a"}]}})

        async def capture(result, *, properties=None):
            results.append(result)

        service.register_function("Write", backend)
        for call_id, value in (("a", 30), ("b", 30), ("c", 50)):
            await service._functions["Write"].handler(SimpleNamespace(
                function_name="Write", tool_call_id=call_id, arguments={"brightness": value}, result_callback=capture))
        self.assertEqual(len(calls), 2)
        self.assertEqual(results[0]["execution_id"], results[1]["execution_id"])
        self.assertNotEqual(results[1]["execution_id"], results[2]["execution_id"])
        self.assertTrue(all(r["status"] == "completed" for r in results))

    async def test_confirmation_regeneration_cannot_execute_tools(self):
        with patch.dict(os.environ, {"QWEN_WORKSPACE_ID": "test", "QWEN_REGION": "cn-beijing"}):
            service = SafeRealtimeLLMService(api_key="test", model="qwen-audio-3.0-realtime-plus",
                                            session_properties=SessionProperties())
        service.tool_registry = ToolRegistry()
        service.tool_definitions = {"Write": CanonicalTool("Write", "", {"type": "object"})}
        calls, results = [], []
        async def backend(params):
            calls.append(params.arguments)
        async def capture(result, *, properties=None):
            results.append(result)
        service.register_function("Write", backend)
        service.control_evidence.regeneration_active = True
        await service._functions["Write"].handler(SimpleNamespace(
            function_name="Write", tool_call_id="retry", arguments={}, result_callback=capture))
        self.assertEqual(calls, [])
        self.assertEqual(results[0]["detail"], "confirmation_replay_no_tools")


if __name__ == "__main__":
    unittest.main()
