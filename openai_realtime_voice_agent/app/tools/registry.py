"""Provider-neutral tool dispatch and conservative result classification."""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import ValidationError

from app.tools.schema import CanonicalTool, CanonicalToolRequest, CanonicalToolResult


@dataclass(frozen=True)
class ToolOutcome:
    result: CanonicalToolResult
    data: Any = None


def normalize_result(value: Any, execution_id: str) -> ToolOutcome:
    """Transport completion alone is never evidence of action completion."""
    if isinstance(value, ToolOutcome):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            failed = value.lstrip().lower().startswith(("error", "failed"))
            return ToolOutcome(CanonicalToolResult(
                execution_id, "failed" if failed else "unknown",
                detail="backend_error" if failed else "unstructured_result",
            ), None if failed else value)
    if not isinstance(value, dict) or not value:
        return ToolOutcome(CanonicalToolResult(execution_id, "unknown", detail="empty_result"))
    if value.get("isError") or value.get("error") or value.get("response_type") == "error":
        return ToolOutcome(CanonicalToolResult(execution_id, "failed", detail="backend_error"))
    if "content" in value:
        content = value["content"]
        if not isinstance(content, list):
            return ToolOutcome(CanonicalToolResult(execution_id, "unknown", detail="malformed_content"))
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if len(texts) == 1:
            return normalize_result(texts[0], execution_id)
        return ToolOutcome(CanonicalToolResult(execution_id, "unknown", detail="ambiguous_content"))
    data = value.get("data")
    if value.get("response_type") == "action_done" and isinstance(data, dict):
        if not isinstance(data.get("success", []), list) or not isinstance(data.get("failed", []), list):
            return ToolOutcome(CanonicalToolResult(execution_id, "unknown", detail="malformed_targets"))
        good = tuple(str(t["id"]) for t in data.get("success", []) if isinstance(t, dict) and t.get("id"))
        bad = tuple(str(t.get("id") or t.get("name") or "unknown") for t in data.get("failed", []) if isinstance(t, dict))
        status = "partial" if good and bad else "completed" if good else "failed" if bad else "unknown"
        # action_done is HA execution evidence, not physical readback verification.
        return ToolOutcome(CanonicalToolResult(execution_id, status, successful_targets=good, failed_targets=bad), value)
    status = value.get("status")
    if isinstance(status, str) and status in {"accepted", "unknown", "failed", "partial"}:
        good = value.get("successful_targets", ())
        bad = value.get("failed_targets", ())
        return ToolOutcome(CanonicalToolResult(execution_id, status,
            verified=status == "partial" and value.get("verified") is True,
            successful_targets=tuple(good) if isinstance(good, (list, tuple)) and all(isinstance(x, str) for x in good) else (),
            failed_targets=tuple(bad) if isinstance(bad, (list, tuple)) and all(isinstance(x, str) for x in bad) else (),
            detail=str(value.get("detail") or "")), value)
    if value.get("success") is False:
        return ToolOutcome(CanonicalToolResult(execution_id, "failed", detail="backend_failure"), value)
    if value.get("success") is True and any(value.get(k) not in (None, {}, [], "") for k in ("result", "data", "evidence")):
        return ToolOutcome(CanonicalToolResult(execution_id, "completed"), value)
    return ToolOutcome(CanonicalToolResult(execution_id, "unknown", detail="no_completion_evidence"), value)


ToolHandler = Callable[[CanonicalToolRequest, str], Awaitable[ToolOutcome]]


class ToolRegistry:
    """Only registered tools can execute; no provider or HA credentials here."""

    def __init__(self, timeout_s: float = 20):
        self._entries: dict[str, tuple[CanonicalTool, ToolHandler]] = {}
        self.timeout_s = timeout_s

    def register(self, tool: CanonicalTool, handler: ToolHandler) -> None:
        if tool.name in self._entries:
            raise ValueError(f"Duplicate tool: {tool.name}")
        self._entries[tool.name] = (tool, handler)

    def register_backend(self, tool: CanonicalTool,
                         call: Callable[[str, dict], Awaitable[Any]]) -> None:
        """Adapt MCP or generated HA handlers without changing their wire names."""
        async def run(request: CanonicalToolRequest, execution_id: str) -> ToolOutcome:
            return normalize_result(await call(tool.name, request.arguments), execution_id)
        self.register(tool, run)

    @property
    def tools(self) -> tuple[CanonicalTool, ...]:
        return tuple(tool for tool, _ in self._entries.values())

    async def execute(self, request: CanonicalToolRequest) -> ToolOutcome:
        execution_id = uuid.uuid4().hex
        entry = self._entries.get(request.name)
        if entry is None:
            return ToolOutcome(CanonicalToolResult(execution_id, "failed", detail="tool_not_registered"))
        tool, handler = entry
        try:
            tool.validate_arguments(request.arguments)
        except ValidationError:
            return ToolOutcome(CanonicalToolResult(execution_id, "failed", detail="invalid_arguments"))
        try:
            return await asyncio.wait_for(handler(request, execution_id), self.timeout_s)
        except TimeoutError:
            return ToolOutcome(CanonicalToolResult(execution_id, "unknown", detail="execution_timeout_do_not_retry"))
        except Exception:
            # The call might already have reached HA. Never expose raw exceptions.
            return ToolOutcome(CanonicalToolResult(execution_id, "unknown", detail="backend_outcome_unknown"))
