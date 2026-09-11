import unittest
from app.control_intent_router import EntityCatalog, EntityInfo
from app.core.arbitration import ControlArbiter
from app.tools.ha_aliases import merge_aliases

class PreflightTests(unittest.TestCase):
    def test_alias_enrichment_does_not_expand_exposure(self):
        catalog = EntityCatalog([EntityInfo('客厅吊灯', 'light', '卧室')])
        entries = [{'entity_id': 'light.one', 'aliases': ['饭厅灯']},
                   {'entity_id': 'light.hidden', 'aliases': ['隐藏灯']}]
        states = [{'entity_id': 'light.one', 'attributes': {'friendly_name': '客厅吊灯'}},
                  {'entity_id': 'light.hidden', 'attributes': {'friendly_name': '隐藏灯'}}]
        result = merge_aliases(catalog, {'light.one'}, entries, states)
        self.assertEqual(len(result.entities), 1)
        decision = ControlArbiter(result).decide('打开饭厅灯', 'HassTurnOn', {'area': '卧室'})
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.arguments, {'name': '客厅吊灯', 'domain': ['light']})

    def test_duplicate_friendly_names_do_not_import_aliases(self):
        catalog = EntityCatalog([EntityInfo('灯', 'light')])
        states = [{'entity_id': i, 'attributes': {'friendly_name': '灯'}} for i in ('light.a', 'light.b')]
        result = merge_aliases(catalog, {'light.a', 'light.b'},
                               [{'entity_id': 'light.a', 'aliases': ['客厅灯']}], states)
        self.assertEqual(result.entities[0].aliases, ())

    def test_mixed_and_sequential_commands_do_not_execute_subset(self):
        arbiter = ControlArbiter(EntityCatalog([EntityInfo('客厅吊灯', 'light')]))
        for text, tool, args in [('打开客厅吊灯并把亮度设为30%', 'HassTurnOn', {}),
                                 ('打开客厅吊灯并把亮度设为30%', 'HassLightSet', {'brightness':30}),
                                 ('客厅吊灯先亮度30%然后亮度60%', 'HassLightSet', {'brightness':30})]:
            with self.subTest(text=text, tool=tool):
                self.assertFalse(arbiter.decide(text, tool, args).allowed)
