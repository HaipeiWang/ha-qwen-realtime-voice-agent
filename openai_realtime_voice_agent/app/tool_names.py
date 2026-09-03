"""Compatibility helpers for Home Assistant LLM tool names.

Home Assistant Core 2026.9 prefixes built-in LLM tools with the integration
domain that provides them (for example ``intent__HassTurnOn``). Older Core
versions expose the canonical name without a prefix. The bridge retains the
wire name for MCP execution and uses the canonical name for policy and routing.
"""

from __future__ import annotations

from collections.abc import Iterable


TOOL_NAMESPACE_SEPARATOR = "__"


def canonical_tool_name(name: str) -> str:
    """Return the provider-neutral name of an HA LLM tool."""
    value = str(name or "").strip()
    if TOOL_NAMESPACE_SEPARATOR not in value:
        return value
    namespace, canonical = value.rsplit(TOOL_NAMESPACE_SEPARATOR, 1)
    return canonical if namespace and canonical else value


def tool_allowed(wire_name: str, allowlist: Iterable[str]) -> bool:
    """Match old or namespaced configuration against an MCP wire name."""
    wire_name = str(wire_name or "").strip()
    canonical = canonical_tool_name(wire_name)
    for configured in allowlist:
        configured = str(configured or "").strip()
        if configured == wire_name or canonical_tool_name(configured) == canonical:
            return True
    return False


def canonical_wire_map(wire_names: Iterable[str]) -> dict[str, str]:
    """Build an unambiguous canonical -> wire-name map.

    A canonical collision is omitted rather than guessed so a future
    third-party integration cannot silently receive the wrong physical action.
    """
    result: dict[str, str] = {}
    ambiguous: set[str] = set()
    for wire_name in wire_names:
        wire_name = str(wire_name or "").strip()
        canonical = canonical_tool_name(wire_name)
        if not wire_name or not canonical or canonical in ambiguous:
            continue
        existing = result.get(canonical)
        if existing is not None and existing != wire_name:
            result.pop(canonical, None)
            ambiguous.add(canonical)
            continue
        result[canonical] = wire_name
    return result
