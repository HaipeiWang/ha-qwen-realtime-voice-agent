import unittest
from app.control_intent_router import EntityCatalog, EntityInfo
from app.core.arbitration import ControlArbiter
from app.tools.ha_aliases import merge_aliases

class HAAliasTests(unittest.TestCase):
 def merge(self, rendered, aliases, friendly='主卧室空调'):
  return merge_aliases(EntityCatalog([EntityInfo(rendered,'climate')]), {'climate.test'},
    [{'entity_id':'climate.test','aliases':aliases}],
    [{'entity_id':'climate.test','attributes':{'friendly_name':friendly}}])
 def test_computed_name_with_alias(self):
  cat=self.merge('主卧室空调, 主卧空调',[None,'主卧空调'])
  for name in ('主卧室空调','主卧空调'):
   d=ControlArbiter(cat).decide('打开'+name,'HassTurnOn',{})
   self.assertTrue(d.allowed); self.assertEqual(d.arguments['name'],'主卧室空调')
 def test_alias_only_does_not_restore_disabled_computed_name(self):
  e=self.merge('主卧空调',['主卧空调']).entities[0]
  self.assertEqual(e.name,'主卧空调'); self.assertNotIn('主卧室空调',e.aliases)
 def test_literal_comma_in_friendly_name(self):
  e=self.merge('空调, 一号, 卧室冷气',[None,'卧室冷气'],'空调, 一号').entities[0]
  self.assertEqual(e.name,'空调, 一号'); self.assertEqual(e.aliases,('卧室冷气',))
 def test_legacy_aliases(self):
  e=self.merge('主卧室空调, 主卧空调',['主卧空调']).entities[0]
  self.assertEqual(e.name,'主卧室空调')
 def test_duplicate_rendering_fail_closed(self):
  cat=EntityCatalog([EntityInfo('主灯, 小灯','light')])
  ids={'light.a','light.b'}
  e=merge_aliases(cat,ids,[{'entity_id':i,'aliases':[None,'小灯']} for i in ids],
    [{'entity_id':i,'attributes':{'friendly_name':'主灯'}} for i in ids]).entities[0]
  self.assertEqual(e.entity_id,''); self.assertEqual(e.aliases,())
