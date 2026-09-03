"""Regression checks for pre/post Home Assistant Core 2026.9 tool names."""

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from app.tool_names import canonical_tool_name, canonical_wire_map, tool_allowed


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


if __name__ == "__main__":
    unittest.main()
