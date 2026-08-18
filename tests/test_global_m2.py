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

    def test_boj_main_table_parser_path(self):
        html = """<table><tr><td>2026/03</td><td>1.1</td>""" + "<td>0</td>"*7 + "<td>12000000</td></tr>" + \
               "<tr><td>2026/04</td><td>1.2</td>" + "<td>0</td>"*7 + "<td>12100000</td></tr>" + \
               "<tr><td>2026/05</td><td>1.4</td>" + "<td>0</td>"*7 + "<td>12200000</td></tr>" + \
               "<tr><td>2026/06</td><td>1.6</td>" + "<td>0</td>"*7 + "<td>12300000</td></tr></table>"
        class R:
            text = html
        with patch.object(global_m2, '_get_retry', return_value=R()):
            out = global_m2._fetch_boj(global_m2.requests.Session())
        self.assertEqual(out['date'], '2026-06-01')
        self.assertAlmostEqual(out['yoy_pct'], 1.6)
        self.assertAlmostEqual(out['yoy_3m_ago_pct'], 1.1)

    def test_full_coverage_quality_flag(self):
        sample = {
            'US': {'region':'US','date':'2026-06-01','yoy_pct':4.0,'yoy_3m_ago_pct':3.0},
            'CN': {'region':'CN','date':'2026-06-01','yoy_pct':8.0,'yoy_3m_ago_pct':7.0},
            'EA': {'region':'EA','date':'2026-06-01','yoy_pct':3.0,'yoy_3m_ago_pct':2.0},
            'JP': {'region':'JP','date':'2026-06-01','yoy_pct':1.0,'yoy_3m_ago_pct':1.5},
        }
        with patch.object(global_m2, '_load_last_good', return_value={}), patch.object(global_m2, '_save_last_good'), \
             patch.object(global_m2, '_fetch_us', return_value=sample['US']), patch.object(global_m2, '_fetch_pbc', return_value=sample['CN']), \
             patch.object(global_m2, '_fetch_ecb', return_value=sample['EA']), patch.object(global_m2, '_fetch_boj', return_value=sample['JP']):
            out = global_m2.build_global_m2()
        self.assertTrue(out['full_coverage'])
        self.assertEqual(out['coverage_quality'], 'FULL')
        self.assertEqual(out['missing_regions'], [])

    def test_pbc_official_listing_selects_latest_and_three_month_prior_with_bounded_calls(self):
        listing = """<html><body>
        <a href=\"/en/r/h1.html\">Financial Statistics Report (H1 2026)</a>
        <a href=\"/en/r/may.html\">Financial Statistics Report (May 2026)</a>
        <a href=\"/en/r/apr.html\">Financial Statistics Report (April 2026)</a>
        <a href=\"/en/r/q1.html\">Financial Statistics Report (Q1 2026)</a>
        </body></html>"""
        h1 = "June 2026 M2 reached RMB 340.0 trillion, rising 8.3 percent year on year."
        q1 = "March 2026 M2 reached RMB 330.0 trillion, rising 7.0 percent year on year."
        class R:
            def __init__(self, text):
                self.text=text; self.content=text.encode(); self.status_code=200; self.headers={}
            def raise_for_status(self): return None
        calls=[]
        def fake_get(url, **kwargs):
            calls.append(url)
            if url == global_m2.PBC_REPORTS_EN: return R(listing)
            if url.endswith('/h1.html'): return R(h1)
            if url.endswith('/q1.html'): return R(q1)
            return R('unneeded')
        session=global_m2.requests.Session()
        with patch.object(session, 'get', side_effect=fake_get):
            global_m2._REQUEST_MEMO.clear(); global_m2._API_HEALTH.clear(); global_m2._PROVIDER_LAST_CALL.clear()
            out=global_m2._fetch_pbc(session)
        self.assertEqual(out['date'], '2026-06-01')
        self.assertAlmostEqual(out['yoy_pct'], 8.3)
        self.assertAlmostEqual(out['yoy_3m_ago_pct'], 7.0)
        self.assertLessEqual(len(calls), 3)

    def test_retry_after_429_is_bounded_and_recovers(self):
        class R:
            def __init__(self, code, text='ok', headers=None):
                self.status_code=code; self.text=text; self.content=text.encode(); self.headers=headers or {}
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise global_m2.requests.HTTPError(f'HTTP {self.status_code}', response=self)
        session=global_m2.requests.Session()
        responses=[R(429, 'rate', {'Retry-After':'0'}), R(200, 'ok')]
        with patch.object(session, 'get', side_effect=responses), patch.object(global_m2.time, 'sleep'):
            global_m2._REQUEST_MEMO.clear(); global_m2._API_HEALTH.clear(); global_m2._PROVIDER_LAST_CALL.clear()
            r=global_m2._get_retry(session, 'https://www.pbc.gov.cn/test', attempts=2)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(global_m2._API_HEALTH['PBC']['http_429'], 1)
        self.assertEqual(global_m2._API_HEALTH['PBC']['retries'], 1)

    def test_same_run_identical_request_is_memory_deduplicated(self):
        class R:
            status_code=200; text='ok'; content=b'ok'; headers={}
            def raise_for_status(self): return None
        session=global_m2.requests.Session()
        with patch.object(session, 'get', return_value=R()) as g:
            global_m2._REQUEST_MEMO.clear(); global_m2._API_HEALTH.clear(); global_m2._PROVIDER_LAST_CALL.clear()
            global_m2._get_retry(session, global_m2.PBC_REPORTS_EN, attempts=1)
            global_m2._get_retry(session, global_m2.PBC_REPORTS_EN, attempts=1)
        self.assertEqual(g.call_count, 1)
        self.assertEqual(global_m2._API_HEALTH['PBC']['memory_cache_hits'], 1)

    def test_us_engine_context_is_primary_and_direct_forecast_improves_global_forecast(self):
        ctx={
            'available':True,'generated_at_utc':'2026-08-18T00:00:00+00:00',
            'm2':{'available':True,'status':'LIVE','observation_date':'2026-06-01','level_billions_usd':23000,'current_yoy_pct':5.0,'prior_3m_yoy_pct':4.0,'forecast_3m_yoy_pct':6.0,'confidence':80,'source':'FRED M2SL'},
            'dxy':{'available':True,'current':100.0,'forecast_3m':98.0,'forecast_change_3m_pct':-2.0,'source':'Yahoo Finance'},
        }
        with patch.object(global_m2, '_US_CONTEXT_MEMO', ctx), \
             patch.object(global_m2, '_load_last_good', return_value={}), patch.object(global_m2, '_save_last_good'), \
             patch.object(global_m2, '_fetch_pbc', return_value={'region':'CN','date':'2026-06-01','yoy_pct':8,'yoy_3m_ago_pct':7}), \
             patch.object(global_m2, '_fetch_ecb', return_value={'region':'EA','date':'2026-06-01','yoy_pct':3,'yoy_3m_ago_pct':2}), \
             patch.object(global_m2, '_fetch_boj', return_value={'region':'JP','date':'2026-06-01','yoy_pct':1,'yoy_3m_ago_pct':1}):
            out=global_m2.build_global_m2()
        self.assertEqual(out['forecast_model'], 'region-specific forecast with direct US Fed-engine M2 model')
        self.assertAlmostEqual(out['forecast_components']['US']['forecast_3m_yoy_pct'],6.0)
        self.assertIn('forward_liquidity_outlook',out)
        self.assertEqual(out['us_dxy']['forecast_3m'],98.0)

    def test_fetch_us_engine_maps_contract_without_requerying_m2_sources(self):
        ctx={'available':True,'generated_at_utc':'2026-08-18T00:00:00+00:00','m2':{'available':True,'status':'LIVE','observation_date':'2026-06-01','current_yoy_pct':5.2,'prior_3m_yoy_pct':4.7,'forecast_3m_yoy_pct':5.5,'level_billions_usd':23100,'source':'Federal Reserve H.6'}}
        with patch.object(global_m2, '_get_us_engine_context', return_value=ctx):
            out=global_m2._fetch_us_engine(global_m2.requests.Session())
        self.assertEqual(out['region'],'US')
        self.assertAlmostEqual(out['yoy_pct'],5.2)
        self.assertAlmostEqual(out['forecast_yoy_3m_pct'],5.5)



    def test_regional_forecast_never_uses_negative_skill_model(self):
        hist=[{'date':f'2024-{(i%12)+1:02d}-01','value':5.0} for i in range(24)]
        out=global_m2._regional_yoy_forecast(hist,3)
        self.assertGreaterEqual(out.get('skill_pct',0),0)
        if out.get('skill_pct',0) <= 0:
            self.assertTrue(out.get('fallback_used'))

if __name__ == '__main__':
    unittest.main()
