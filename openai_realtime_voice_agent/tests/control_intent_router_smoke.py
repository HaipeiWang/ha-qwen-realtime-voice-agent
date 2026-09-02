"""Regression tests for deterministic entity selection and MCP safeguards."""

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from app.control_intent_router import EntityCatalog, EntityInfo


class EntitySelectorNormalizationTest(unittest.TestCase):
    def setUp(self):
        # Both lights inherit the bedroom area from one shared Bluetooth proxy.
        self.catalog = EntityCatalog([
            EntityInfo(name="卧室吸顶灯", domain="light", area="卧室"),
            EntityInfo(name="客厅吊灯", domain="light", area="卧室"),
        ])

    def test_unique_exact_name_drops_conflicting_inherited_area(self):
        args, info = self.catalog.normalize_control_arguments(
            "HassTurnOn",
            {"name": "客厅吊灯", "area": "客厅", "domain": ["light"]},
        )

        self.assertEqual(args, {"name": "客厅吊灯", "domain": ["light"]})
        self.assertEqual(info["match"], "exact")
        self.assertIn("dropped_conflicting_area", info["changes"])

    def test_matching_area_is_preserved(self):
        original = {"name": "卧室吸顶灯", "area": "卧室", "domain": ["light"]}
        args, info = self.catalog.normalize_control_arguments("HassTurnOff", original)

        self.assertEqual(args, original)
        self.assertIsNone(info)

    def test_high_confidence_unique_name_is_canonicalised(self):
        args, info = self.catalog.normalize_control_arguments(
            "HassLightSet",
            {"name": "客厅的吊灯", "area": "客厅", "domain": ["light"], "brightness": 50},
        )

        self.assertEqual(args["name"], "客厅吊灯")
        self.assertNotIn("area", args)
        self.assertEqual(info["match"], "fuzzy")

    def test_generic_area_name_is_not_guessed(self):
        original = {"name": "客厅", "area": "客厅", "domain": ["light"]}
        args, info = self.catalog.normalize_control_arguments("HassTurnOn", original)

        self.assertEqual(args, original)
        self.assertIsNone(info)

    def test_duplicate_friendly_name_is_ambiguous(self):
        catalog = EntityCatalog([
            EntityInfo(name="吊灯", domain="light", area="客厅"),
            EntityInfo(name="吊灯", domain="light", area="餐厅"),
        ])
        original = {"name": "吊灯", "area": "书房", "domain": ["light"]}
        args, info = catalog.normalize_control_arguments("HassTurnOn", original)

        self.assertEqual(args, original)
        self.assertIsNone(info)

    def test_query_tool_keeps_area_filter(self):
        original = {"name": "客厅吊灯", "area": "客厅", "domain": "light"}
        args, info = self.catalog.normalize_control_arguments("GetLiveContext", original)

        self.assertEqual(args, original)
        self.assertIsNone(info)


if __name__ == "__main__":
    unittest.main()
