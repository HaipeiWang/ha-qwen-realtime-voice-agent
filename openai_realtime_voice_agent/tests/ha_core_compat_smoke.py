"""HA 2026.8/9 text results and 2026.10 MCP ToolResult contracts.

Response fixtures follow the tagged Core mcp_server/server.py and
homeassistant/llm.py; use the installed MCP SDK and Pipecat bindings.
"""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from mcp import types

from app.control_intent_router import _call_mcp_tool, _extract_live_context_result
from app.mcp_service import HomeAssistantMCPService
from app.tools.legacy import callback_backend
from app.tools.schema import CanonicalToolRequest


CONTEXT = "Live Context: An overview of the areas and the devices in this smart home:\n- names: 测试灯\n  domain: light\n  state: 'on'\n  attributes:\n    brightness: 128\n"
ACTION = {"response_type": "action_done", "data": {
    "success": [{"id": "light.test"}], "failed": []}}


def response(data, *, error=False):
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        isError=error,
    )


class MCPCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = await HomeAssistantMCPService("http://ha/api/mcp", "test").initialize()

    async def execute(self, name, result):
        session = AsyncMock()
        session.call_tool.return_value = result

        async def handler(params):
            await self.client._call_tool(session, params.function_name,
                                         params.arguments, params.result_callback)

        request = CanonicalToolRequest("conversation", "turn", "call", name, {})
        outcome = await callback_backend(handler)(request, "execution")
        session.call_tool.assert_awaited_once_with(name, arguments={})
        return outcome

    async def test_context_success_across_core_versions(self):
        for name, data in (
            ("GetLiveContext", {"success": True, "result": CONTEXT}),
            ("homeassistant__GetLiveContext", {"success": True, "result": CONTEXT}),
            ("homeassistant__GetLiveContext", {"result": CONTEXT}),
        ):
            with self.subTest(name=name, data=data):
                outcome = await self.execute(name, response(data))
                self.assertEqual(outcome.result.status, "completed")
                self.assertIn("brightness_percent", outcome.data["result"])

    async def test_mcp_error_overrides_success_looking_text(self):
        for name, data in (("homeassistant__GetLiveContext", {"result": CONTEXT}),
                           ("intent__HassTurnOn", ACTION),
                           ("HassTurnOn", {"success": True, "result": "done"})):
            with self.subTest(name=name):
                outcome = await self.execute(name, response(data, error=True))
                self.assertEqual(outcome.result.status, "failed")
                self.assertFalse(outcome.result.permits_success_confirmation)

    async def test_old_and_new_context_errors_remain_failures(self):
        for data, error in (({"success": False, "error": "No exposed entities"}, False),
                             ({"error": "No exposed entities"}, True)):
            outcome = await self.execute("homeassistant__GetLiveContext", response(data, error=error))
            self.assertEqual(outcome.result.status, "failed")

    async def test_action_targets_and_partial_results_survive(self):
        for name in ("HassTurnOn", "intent__HassTurnOn"):
            for failed in ([], [{"id": "light.failed"}]):
                data = {"response_type": "action_done", "data": {
                    "success": [{"id": "light.test"}], "failed": failed}}
                outcome = await self.execute(name, response(data))
                self.assertEqual(outcome.result.status, "partial" if failed else "completed")
                self.assertEqual(outcome.result.successful_targets, ("light.test",))
                self.assertFalse(outcome.result.verified)

    async def test_empty_or_unstructured_context_does_not_become_success(self):
        for data in ({}, {"result": ""}, {"result": "not a live context"}):
            outcome = await self.execute("homeassistant__GetLiveContext", response(data))
            self.assertEqual(outcome.result.status, "unknown")
        outcome = await self.execute("homeassistant__GetLiveContext", types.CallToolResult(content=[]))
        self.assertEqual(outcome.result.status, "unknown")

    async def test_context_completion_rule_does_not_apply_to_writes(self):
        outcome = await self.execute("intent__HassTurnOn", response({"result": CONTEXT}))
        self.assertEqual(outcome.result.status, "unknown")

    async def test_transport_exception_propagates_without_retry(self):
        session = AsyncMock()
        session.call_tool.side_effect = TimeoutError("outcome unknown")
        callback = AsyncMock()
        with self.assertRaises(TimeoutError):
            await self.client._call_tool(session, "intent__HassTurnOn", {}, callback)
        self.assertEqual(session.call_tool.await_count, 1)
        callback.assert_not_awaited()

    async def test_new_metadata_required_and_union_schema_use_existing_sdk(self):
        session = AsyncMock()
        session.list_tools.return_value = types.ListToolsResult(tools=[types.Tool(
            name="light__HassLightSet", title="Set light", description="Set a light",
            inputSchema={"type": "object", "properties": {
                "name": {"type": "string"},
                "domain": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            }, "required": ["name"]},
            annotations=types.ToolAnnotations(idempotentHint=True, readOnlyHint=False),
        )])
        tools = await self.client._list_tools_helper(session)
        tool = tools.standard_tools[0]
        self.assertEqual(tool.name, "light__HassLightSet")
        self.assertEqual(tool.required, ["name"])
        self.assertIn("anyOf", tool.properties["domain"])


class StartupCompatibilityTests(unittest.TestCase):
    def call(self, result):
        http = MagicMock()
        http.__enter__.return_value.read.return_value = json.dumps(result).encode()
        with patch("app.control_intent_router.request.urlopen", return_value=http):
            return _call_mcp_tool("http://ha/api/mcp", "test", "homeassistant__GetLiveContext", {}, 1)

    def test_startup_reads_old_and_new_context_data(self):
        for data in ({"success": True, "result": CONTEXT}, {"result": CONTEXT}):
            result = response(data).model_dump(mode="json")
            text = self.call({"jsonrpc": "2.0", "id": 1, "result": result})
            self.assertEqual(_extract_live_context_result(text), CONTEXT)

    def test_startup_rejects_mcp_error_even_with_parseable_context(self):
        result = response({"result": CONTEXT}, error=True).model_dump(mode="json")
        with self.assertRaises(ValueError):
            self.call({"jsonrpc": "2.0", "id": 1, "result": result})

    def test_startup_rejects_jsonrpc_error(self):
        with self.assertRaises(ValueError):
            self.call({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad params"}})


if __name__ == "__main__":
    unittest.main()
