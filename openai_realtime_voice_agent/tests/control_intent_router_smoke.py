"""Regression tests for deterministic entity selection and MCP safeguards."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from app.control_intent_router import (
    EntityCatalog,
    EntityInfo,
    build_catalog_from_ha,
)


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

    def test_namespaced_tool_keeps_area_conflict_fallback(self):
        args, info = self.catalog.normalize_control_arguments(
            "intent__HassTurnOn",
            {"name": "客厅吊灯", "area": "客厅", "domain": ["light"]},
        )

        self.assertEqual(args, {"name": "客厅吊灯", "domain": ["light"]})
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

    def test_catalog_prefers_new_live_context_name(self):
        live_context = """- names: 客厅吊灯
  domain: light
  state: 'off'
  areas: 卧室
"""
        with patch(
            "app.control_intent_router._call_mcp_tool",
            return_value=live_context,
        ) as call:
            catalog = build_catalog_from_ha(
                "http://home-assistant/api/mcp",
                "unused",
                retries=1,
            )

        self.assertEqual(catalog.entities[0].name, "客厅吊灯")
        self.assertEqual(
            call.call_args.args[2], "homeassistant__GetLiveContext"
        )

    def test_catalog_falls_back_to_legacy_live_context_name(self):
        live_context = """- names: 卧室吸顶灯
  domain: light
  state: 'on'
  areas: 卧室
"""
        with patch(
            "app.control_intent_router._call_mcp_tool",
            side_effect=[ValueError("unknown new tool"), live_context],
        ) as call:
            catalog = build_catalog_from_ha(
                "http://home-assistant/api/mcp",
                "unused",
                retries=1,
            )

        self.assertEqual(catalog.entities[0].name, "卧室吸顶灯")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args.args[2], "GetLiveContext")


if __name__ == "__main__":
    unittest.main()
