"""Turn-scoped execution ledger shared by router and model callers."""

import asyncio
import json
import uuid

from app.tools.registry import ToolOutcome, ToolRegistry
from app.tools.schema import CanonicalToolRequest, CanonicalToolResult


class ExecutionLedger:
    """One ledger per turn. Caller lifetime must not cancel a dispatched action.

    Read results are shared only while in flight. Consecutive equivalent writes
    reuse evidence, but intervening writes invalidate that reuse (off/on/off).
    Unknown writes remain non-retriable for this turn regardless of later calls.
    """

    def __init__(self, registry: ToolRegistry, conversation_id: str, turn_id: str, limit: int = 8):
        self.registry = registry
        self.owner = (conversation_id, turn_id)
        self.limit = limit
        self.dispatches = 0
        self._calls = {}
        self._running = {}
        self._uncertain = {}
        self._last_write = None
        self._last_write_task = None

    @staticmethod
    def _failure(detail: str) -> ToolOutcome:
        return ToolOutcome(CanonicalToolResult(uuid.uuid4().hex, "failed", detail=detail))

    async def execute(self, request: CanonicalToolRequest) -> ToolOutcome:
        if (request.conversation_id, request.turn_id) != self.owner:
            return self._failure("wrong_turn")
        key = (request.name, tuple(sorted(request.targets)),
               json.dumps(request.arguments, sort_keys=True, ensure_ascii=False, allow_nan=False))
        previous = self._calls.get(request.call_id)
        if previous is not None:
            old_key, task = previous
            if old_key != key:
                return self._failure("call_id_reused_with_different_arguments")
            return await asyncio.shield(task)
        definitions = {t.name: t for t in self.registry.tools}
        definition = definitions.get(request.name)
        if definition is None:
            return self._failure("tool_not_registered")
        read_only = definition.read_only
        task = self._running.get(key)
        if task is not None and task.done():
            task = None
        if not read_only:
            task = task or self._uncertain.get(key)
            if task is None and key == self._last_write:
                task = self._last_write_task
        if task is None:
            if self.dispatches >= self.limit:
                return self._failure("turn_tool_limit_reached")
            self.dispatches += 1
            if not read_only:
                # A query started before this write cannot satisfy a new read.
                self._running = {k: v for k, v in self._running.items()
                                 if not definitions[k[0]].read_only}
            # No await before identity registration: concurrent callers see one task.
            task = asyncio.create_task(self.registry.execute(request))
            self._running[key] = task
            if not read_only:
                self._last_write, self._last_write_task = key, task

                def finished(done):
                    if not done.cancelled() and done.exception() is None and done.result().result.status in {"unknown", "accepted"}:
                        self._uncertain[key] = done
                task.add_done_callback(finished)
        self._calls[request.call_id] = (key, task)
        return await asyncio.shield(task)

    async def drain(self) -> None:
        """Shutdown may await dispatched calls; it must not silently undo them."""
        await asyncio.gather(*(asyncio.shield(t) for _, t in self._calls.values()), return_exceptions=True)
