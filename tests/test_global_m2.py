import unittest
from unittest.mock import patch

import global_m2


class GlobalM2Tests(unittest.TestCase):
    def test_composite_uses_multiple_regions_and_preserves_real_direction(self):
        sample = {
            'US': {'region':'US','date':'2026-06-01','yoy_pct':4.0,'yoy_3m_ago_pct':3.0,'source':'test'},
            'CN': {'region':'CN','date':'2026-06-01','yoy_pct':8.0,'yoy_3m_ago_pct':7.0,'source':'test'},
            'EA': {'region':'EA','date':'2026-06-01','yoy_pct':3.0,'yoy_3m_ago_pct':2.0,'source':'test'},
            'JP': {'region':'JP','date':'2026-06-01','yoy_pct':1.0,'yoy_3m_ago_pct':1.5,'source':'test'},
        }
        with patch.object(global_m2, '_load_last_good', return_value={}), \
             patch.object(global_m2, '_save_last_good'), \
             patch.object(global_m2, '_fetch_us', return_value=sample['US']), \
             patch.object(global_m2, '_fetch_pbc', return_value=sample['CN']), \
             patch.object(global_m2, '_fetch_ecb', return_value=sample['EA']), \
             patch.object(global_m2, '_fetch_boj', return_value=sample['JP']):
            out = global_m2.build_global_m2()
        self.assertEqual(out['coverage_regions'], ['CN','EA','JP','US'])
        self.assertFalse(out['is_proxy'])
        self.assertNotEqual(out['forecast'], out['current'])
        self.assertGreater(out['directionScore'], 0)
        self.assertIn('components', out)

    def test_one_failed_region_reweights_not_us_only_proxy(self):
        previous = {'components': {}}
        with patch.object(global_m2, '_load_last_good', return_value=previous), \
             patch.object(global_m2, '_save_last_good'), \
             patch.object(global_m2, '_fetch_us', return_value={'region':'US','date':'2026-06-01','yoy_pct':4,'yoy_3m_ago_pct':3}), \
             patch.object(global_m2, '_fetch_pbc', side_effect=ValueError('down')), \
             patch.object(global_m2, '_fetch_ecb', return_value={'region':'EA','date':'2026-06-01','yoy_pct':3,'yoy_3m_ago_pct':2}), \
             patch.object(global_m2, '_fetch_boj', return_value={'region':'JP','date':'2026-06-01','yoy_pct':1,'yoy_3m_ago_pct':1}):
            out = global_m2.build_global_m2()
        self.assertIn('US', out['coverage_regions'])
        self.assertIn('EA', out['coverage_regions'])
        self.assertGreaterEqual(out['coverage_weight'], .5)
        self.assertEqual(out['statuses']['CN'], 'UNAVAILABLE')

    def test_last_good_component_is_visible(self):
        previous={'components': {'CN': {'region':'CN','date':'2026-06-01','yoy_pct':7.5,'yoy_3m_ago_pct':7.0,'source':'old'}}}
        with patch.object(global_m2, '_load_last_good', return_value=previous), \
             patch.object(global_m2, '_save_last_good'), \
             patch.object(global_m2, '_fetch_us', return_value={'region':'US','date':'2026-06-01','yoy_pct':4,'yoy_3m_ago_pct':3}), \
             patch.object(global_m2, '_fetch_pbc', side_effect=ValueError('temporary')), \
             patch.object(global_m2, '_fetch_ecb', return_value={'region':'EA','date':'2026-06-01','yoy_pct':3,'yoy_3m_ago_pct':2}), \
             patch.object(global_m2, '_fetch_boj', return_value={'region':'JP','date':'2026-06-01','yoy_pct':1,'yoy_3m_ago_pct':1}):
            out = global_m2.build_global_m2()
        self.assertEqual(out['statuses']['CN'], 'LAST-GOOD')

    def test_insufficient_coverage_abstains_without_breaking_workflow(self):
        with patch.object(global_m2, '_load_last_good', return_value={}), \
             patch.object(global_m2, '_save_last_good'), \
             patch.object(global_m2, '_fetch_us', side_effect=ValueError('down')), \
             patch.object(global_m2, '_fetch_pbc', side_effect=ValueError('down')), \
             patch.object(global_m2, '_fetch_ecb', side_effect=ValueError('down')), \
             patch.object(global_m2, '_fetch_boj', side_effect=ValueError('down')):
            out = global_m2.build_global_m2()
        self.assertFalse(out['available'])
        self.assertIsNone(out['value'])
        self.assertEqual(out['coverage_regions'], [])


if __name__ == '__main__':
    unittest.main()
