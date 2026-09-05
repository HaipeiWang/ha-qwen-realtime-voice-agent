"""Single source of truth for Home Assistant control-tool exposure."""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.tool_names import canonical_tool_name


GET_LIVE_CONTEXT_TOOL = "GetLiveContext"
TURN_ON_TOOL = "HassTurnOn"
TURN_OFF_TOOL = "HassTurnOff"
LIGHT_SET_TOOL = "HassLightSet"
CLIMATE_SET_TEMPERATURE_TOOL = "HassClimateSetTemperature"
FAN_SET_SPEED_TOOL = "HassFanSetSpeed"
COVER_STOP_TOOL = "HassStopMoving"

# Every static tool referenced by ControlIntentRouter. Generated capability
# tools register their own rules and therefore do not belong in this list.
ROUTER_CONTROL_TOOLS = (
    TURN_ON_TOOL,
    TURN_OFF_TOOL,
    LIGHT_SET_TOOL,
    CLIMATE_SET_TEMPERATURE_TOOL,
    FAN_SET_SPEED_TOOL,
    COVER_STOP_TOOL,
)
DEFAULT_MCP_TOOL_ALLOWLIST = (GET_LIVE_CONTEXT_TOOL, *ROUTER_CONTROL_TOOLS)

_LEGACY_DEFAULTS = (
    frozenset(DEFAULT_MCP_TOOL_ALLOWLIST[:4]),
    frozenset(DEFAULT_MCP_TOOL_ALLOWLIST[:5]),
)
_SEPARATORS = re.compile(r"[\s,，;；]+")


def parse_tool_allowlist(raw_value: str | Iterable[str]) -> list[str]:
    """Parse UI/legacy tool lists and migrate known historical defaults."""
    if isinstance(raw_value, str):
        values = _SEPARATORS.split(raw_value.strip()) if raw_value.strip() else []
    else:
        values = [str(value).strip() for value in raw_value if str(value).strip()]

    tools: list[str] = []
    seen: set[str] = set()
    for value in values:
        canonical = canonical_tool_name(value)
        if canonical and canonical not in seen:
            seen.add(canonical)
            tools.append(value)

    if frozenset(seen) in _LEGACY_DEFAULTS:
        return list(DEFAULT_MCP_TOOL_ALLOWLIST)
    return tools


def resolve_tool_allowlist(
    available_wire_names: Iterable[str], configured_names: Iterable[str]
) -> tuple[list[str], list[str]]:
    """Resolve old/canonical configuration to unambiguous Core wire names."""
    available = [str(name).strip() for name in available_wire_names if str(name).strip()]
    exact = set(available)
    by_canonical: dict[str, list[str]] = {}
    for wire_name in available:
        by_canonical.setdefault(canonical_tool_name(wire_name), []).append(wire_name)

    matched: list[str] = []
    missing: list[str] = []
    for configured in configured_names:
        configured = str(configured).strip()
        candidates = by_canonical.get(canonical_tool_name(configured), [])
        wire_name = configured if configured in exact else (
            candidates[0] if len(candidates) == 1 else None
        )
        if wire_name is None:
            missing.append(configured)
        elif wire_name not in matched:
            matched.append(wire_name)
    return matched, missing
