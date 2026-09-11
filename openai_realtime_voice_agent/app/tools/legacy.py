"""Temporary Pipecat tool binding. No provider protocol or execution policy."""

from dataclasses import asdict
from types import SimpleNamespace

from app.tools.registry import ToolOutcome, normalize_result
from app.tools.live_context import normalize_live_context
from app.tool_names import canonical_tool_name


def outcome_payload(outcome: ToolOutcome) -> dict:
    payload = asdict(outcome.result)
    payload["success"] = outcome.result.status == "completed"
    payload["result"] = outcome.data
    return payload


def callback_backend(handler):
    async def execute(request, execution_id):
        outcome = None

        async def capture(result, *, properties=None):
            nonlocal outcome
            if outcome is None:
                if canonical_tool_name(request.name) == "GetLiveContext":
                    result = normalize_live_context(result)
                outcome = normalize_result(result, execution_id)

        params = SimpleNamespace(function_name=request.name, tool_call_id=request.call_id,
                                 arguments=request.arguments, result_callback=capture)
        await handler(params)
        return outcome or normalize_result(None, execution_id)
    return execute
