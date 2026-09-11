import asyncio
import unittest

from app.tools.execution import ExecutionLedger
from app.tools.registry import ToolOutcome, ToolRegistry
from app.tools.schema import CanonicalTool, CanonicalToolRequest, CanonicalToolResult


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.status = "completed"
        self.gate = asyncio.Event()
        self.gate.set()
        self.registry = ToolRegistry()
        async def run(request, execution_id):
            self.calls.append(request)
            await self.gate.wait()
            return ToolOutcome(CanonicalToolResult(execution_id, self.status), len(self.calls))
        for name, read in (("Write", False), ("Read", True)):
            self.registry.register(CanonicalTool(name, "", {"type": "object"}, read_only=read), run)
        self.ledger = ExecutionLedger(self.registry, "c", "t")

    def req(self, call, name="Write", **arguments):
        return CanonicalToolRequest("c", "t", call, name, arguments)

    async def test_concurrent_router_and_model_call_once(self):
        results = await asyncio.gather(*(self.ledger.execute(self.req(str(i), value=30)) for i in range(5)))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len({r.result.execution_id for r in results}), 1)

    async def test_different_parameters_are_not_reused(self):
        for i in (30, 50):
            await self.ledger.execute(self.req(str(i), value=i))
        self.assertEqual(len(self.calls), 2)

    async def test_off_on_off_is_three_actions(self):
        for i, value in enumerate((False, True, False)):
            await self.ledger.execute(self.req(str(i), value=value))
        self.assertEqual(len(self.calls), 3)

    async def test_same_call_id_cannot_change_action(self):
        await self.ledger.execute(self.req("a", value=30))
        result = await self.ledger.execute(self.req("a", value=50))
        self.assertEqual(result.result.detail, "call_id_reused_with_different_arguments")
        self.assertEqual(len(self.calls), 1)

    async def test_write_then_read_is_fresh(self):
        await self.ledger.execute(self.req("a", "Read"))
        await self.ledger.execute(self.req("b"))
        result = await self.ledger.execute(self.req("c", "Read"))
        self.assertEqual(result.data, 3)

    async def test_unknown_is_not_retried_after_intervening_write(self):
        self.status = "unknown"
        first = await self.ledger.execute(self.req("a", value=30))
        self.status = "completed"
        await self.ledger.execute(self.req("b", value=50))
        again = await self.ledger.execute(self.req("c", value=30))
        self.assertEqual(again.result.execution_id, first.result.execution_id)
        self.assertEqual(len(self.calls), 2)

    async def test_limit_stops_loop(self):
        for i in range(8):
            await self.ledger.execute(self.req(str(i), value=i))
        result = await self.ledger.execute(self.req("ninth", value=9))
        self.assertEqual(result.result.detail, "turn_tool_limit_reached")
        self.assertEqual(len(self.calls), 8)

    async def test_caller_cancellation_does_not_cancel_dispatched_operation(self):
        self.gate.clear()
        caller = asyncio.create_task(self.ledger.execute(self.req("a")))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.gate.set()
        await self.ledger.drain()
        self.assertEqual((await self.ledger.execute(self.req("a"))).result.status, "completed")
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
