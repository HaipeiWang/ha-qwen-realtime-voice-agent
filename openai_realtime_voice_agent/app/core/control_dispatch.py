"""Shared pre-execution arbitration, independent of provider event readers."""

from dataclasses import replace
import asyncio
import json
from collections.abc import Awaitable, Callable
import uuid

from app.core.arbitration import ControlArbiter, TranscriptGate, explicit_light_values, normal
from app.tools.execution import ExecutionLedger
from app.tools.registry import ToolOutcome
from app.tools.schema import CanonicalToolRequest, CanonicalToolResult
from app.control_intent_router import ENTITY_CONTROL_TOOLS
from app.tool_names import canonical_tool_name


class ControlDispatch:
    def __init__(self, arbiter: ControlArbiter,
                 attributes: Callable[[str], Awaitable[dict]] | None = None):
        self.arbiter = arbiter
        self.attributes = attributes
        self._prepare_lock = asyncio.Lock()
        self._prepared_owner = None
        self._prepared = {}

    async def execute(self, request: CanonicalToolRequest, ledger: ExecutionLedger,
                      gate: TranscriptGate, still_current: Callable[[], bool]) -> ToolOutcome:
        def reject(reason):
            return ToolOutcome(CanonicalToolResult(uuid.uuid4().hex, "failed", detail=reason))

        if canonical_tool_name(request.name) not in ENTITY_CONTROL_TOOLS:
            return await ledger.execute(request)
        text = await gate.wait()
        if not still_current():
            return reject("input_turn_changed_before_dispatch")
        async with self._prepare_lock:
            owner = (request.conversation_id, request.turn_id)
            if not still_current():
                return reject("input_turn_changed_before_dispatch")
            if owner != self._prepared_owner:
                self._prepared_owner = owner
                self._prepared.clear()
            selector = self.arbiter.decide(text, request.name, request.arguments, selection_only=True)
            key_args = dict(selector.arguments if selector.allowed else request.arguments)
            explicit = explicit_light_values(text)
            if canonical_tool_name(request.name) == "HassLightSet":
                for parameter, words in (("brightness", ("调亮", "调暗", "亮一点", "暗一点")),
                                         ("temperature", ("调暖", "调冷", "暖一点", "冷一点"))):
                    if parameter in key_args and parameter not in explicit and any(w in text for w in words):
                        key_args[parameter] = "relative_to_initial_state"
            key = (request.name, text, json.dumps(key_args, sort_keys=True, ensure_ascii=False))
            decision = self._prepared.get(key)
            if decision is None:
                decision = await self._prepare(text, request)
                if decision.allowed:
                    self._prepared[key] = decision
        if not still_current():
            return reject("input_turn_changed_before_dispatch")
        if not decision.allowed:
            return reject(decision.reason)
        outcome = await ledger.execute(replace(request, arguments=decision.arguments))
        if outcome.result.status == "completed" and still_current():
            self.arbiter.record_completed(decision.arguments)
        return ToolOutcome(outcome.result, {"executed_arguments": decision.arguments,
                                          "backend_result": outcome.data})

    async def _prepare(self, text, request):
        from app.core.arbitration import ControlDecision
        decision = self.arbiter.decide(text, request.name, request.arguments)
        # Resolve name before reading live attributes; never use the model's
        # unvalidated selector as permission to access an entity.
        needs_attributes = decision.reason in {
            "brightness_state_unknown", "color_temperature_state_unknown",
            "color_temperature_range_unknown",
            "group_light_parameters_require_per_target_validation",
        }
        if needs_attributes and self.attributes:
            selector = self.arbiter.decide(text, request.name, request.arguments, selection_only=True)
            if selector.allowed:
                try:
                    if "name" in selector.arguments:
                        attrs = await self.attributes(selector.arguments["name"])
                    else:
                        targets = [e for e in self.arbiter.catalog.entities if e.domain == "light"
                                   and normal(e.area) == normal(selector.arguments.get("area", ""))]
                        if not targets:
                            return ControlDecision(False, reason="group_not_exposed")
                        states = await asyncio.gather(*(self.attributes(e.name) for e in targets))
                        attrs = {"group_ranges_validated": True}
                        if request.arguments.get("temperature") is not None:
                            lows = [s.get("min_color_temp_kelvin") for s in states]
                            highs = [s.get("max_color_temp_kelvin") for s in states]
                            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in lows + highs):
                                return ControlDecision(False, reason="color_temperature_range_unknown")
                            attrs.update(min_color_temp_kelvin=max(lows), max_color_temp_kelvin=min(highs))
                except Exception:
                    return ControlDecision(False, reason="entity_state_unavailable")
                decision = self.arbiter.decide(text, request.name, request.arguments, attrs)
        return decision
