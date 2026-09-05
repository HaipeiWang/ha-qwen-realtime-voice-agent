"""Regression checks for pre/post Home Assistant Core 2026.9 tool names."""

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from app.tool_names import canonical_tool_name, canonical_wire_map, tool_allowed
from app.tool_policy import (
    DEFAULT_MCP_TOOL_ALLOWLIST,
    parse_tool_allowlist,
    resolve_tool_allowlist,
)


class ToolNameCompatibilityTest(unittest.TestCase):
    def test_canonical_name_supports_old_and_namespaced_core(self):
        self.assertEqual(canonical_tool_name("HassTurnOn"), "HassTurnOn")
        self.assertEqual(
            canonical_tool_name("intent__HassTurnOn"), "HassTurnOn"
        )
        self.assertEqual(
            canonical_tool_name("climate__HassClimateSetTemperature"),
            "HassClimateSetTemperature",
        )

    def test_old_allowlist_accepts_new_wire_names(self):
        allowlist = ["GetLiveContext", "HassTurnOn"]
        self.assertTrue(
            tool_allowed("homeassistant__GetLiveContext", allowlist)
        )
        self.assertTrue(tool_allowed("intent__HassTurnOn", allowlist))
        self.assertFalse(tool_allowed("intent__HassTurnOff", allowlist))

    def test_namespaced_allowlist_also_accepts_old_core(self):
        self.assertTrue(
            tool_allowed("HassTurnOn", ["intent__HassTurnOn"])
        )

    def test_wire_map_keeps_execution_name_and_rejects_collisions(self):
        mapping = canonical_wire_map(
            [
                "homeassistant__GetLiveContext",
                "intent__HassTurnOn",
                "climate__HassClimateSetTemperature",
            ]
        )
        self.assertEqual(mapping["HassTurnOn"], "intent__HassTurnOn")
        self.assertEqual(
            mapping["HassClimateSetTemperature"],
            "climate__HassClimateSetTemperature",
        )

        collided = canonical_wire_map(
            ["first__HassTurnOn", "second__HassTurnOn"]
        )
        self.assertNotIn("HassTurnOn", collided)

    def test_allowlist_accepts_ui_and_legacy_separators(self):
        parsed = parse_tool_allowlist(
            "GetLiveContext intent__HassTurnOn，intent__HassTurnOff;HassLightSet"
        )
        # The exact historic four-tool default migrates to every router
        # dependency rather than leaving newer controls unavailable.
        self.assertEqual(parsed, list(DEFAULT_MCP_TOOL_ALLOWLIST))

    def test_five_tool_namespaced_default_migrates(self):
        parsed = parse_tool_allowlist(
            "climate__HassClimateSetTemperature\n"
            "homeassistant__GetLiveContext intent__HassTurnOn "
            "intent__HassTurnOff light__HassLightSet"
        )
        self.assertEqual(parsed, list(DEFAULT_MCP_TOOL_ALLOWLIST))
        self.assertIn("HassFanSetSpeed", parsed)
        self.assertIn("HassStopMoving", parsed)

    def test_allowlist_resolves_current_core_wire_names(self):
        available = [
            "homeassistant__GetLiveContext",
            "intent__HassTurnOn",
            "intent__HassTurnOff",
            "intent__HassStopMoving",
            "light__HassLightSet",
            "climate__HassClimateSetTemperature",
            "fan__HassFanSetSpeed",
        ]
        matched, missing = resolve_tool_allowlist(
            available, DEFAULT_MCP_TOOL_ALLOWLIST
        )
        self.assertEqual(set(matched), set(available))
        self.assertEqual(missing, [])

    def test_allowlist_reports_missing_or_ambiguous_tools(self):
        matched, missing = resolve_tool_allowlist(
            ["first__HassTurnOn", "second__HassTurnOn"],
            ["HassTurnOn", "HassTurnOff"],
        )
        self.assertEqual(matched, [])
        self.assertEqual(missing, ["HassTurnOn", "HassTurnOff"])


if __name__ == "__main__":
    unittest.main()
