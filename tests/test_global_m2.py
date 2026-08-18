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


    def test_month_key_preserves_oct_nov_dec(self):
        self.assertEqual(global_m2._month_key('2025-10-01'), '2025-10')
        self.assertEqual(global_m2._month_key('2025-11-01'), '2025-11')
        self.assertEqual(global_m2._month_key('2025-12-01'), '2025-12')
        self.assertEqual(global_m2._pbc_report_month('2025年11月金融统计数据报告'), '2025-11')

    def test_regional_forecast_uses_recent_contiguous_months_only(self):
        hist=[]
        y,m=2024,1
        for i in range(24):
            if not (y==2024 and m==10):
                hist.append({'date':f'{y:04d}-{m:02d}-01','value':3.0+i*0.01})
            m += 1
            if m==13: y+=1; m=1
        out=global_m2._regional_yoy_forecast(hist,3)
        self.assertGreaterEqual(out['history_gaps_dropped'], 1)
        self.assertLess(out['history_points_contiguous'], out['history_points_total'])

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
        with patch.object(session, 'get', side_effect=fake_get), \
             patch.object(global_m2, '_read_cn_history_cache', return_value=[]), \
             patch.object(global_m2, '_write_cn_history_cache'):
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
        ctx={'available':True,'generated_at_utc':'2026-08-18T00:00:00+00:00','m2':{'available':True,'status':'LIVE','observation_date':'2026-06-01','current_yoy_pct':5.2,'prior_3m_yoy_pct':4.7,'forecast_3m_yoy_pct':5.5,'level_billions_usd':23100,'source':'Federal Reserve H.6','backtest_3m':{'skill_pct':39.5,'rmse_pct':1.1,'baseline_rmse_pct':1.8,'backtests':72,'fallback_used':False},'forecast_quality_gate':{'passed':True},'yoy_history':[{'date':'2026-06-01','value':5.2}]}}
        with patch.object(global_m2, '_get_us_engine_context', return_value=ctx):
            out=global_m2._fetch_us_engine(global_m2.requests.Session())
        self.assertEqual(out['region'],'US')
        self.assertAlmostEqual(out['yoy_pct'],5.2)
        self.assertAlmostEqual(out['forecast_yoy_3m_pct'],5.5)
        self.assertAlmostEqual(out['forecast_validation']['skill_pct'],39.5)
        self.assertTrue(out['forecast_quality_gate']['passed'])
        self.assertEqual(len(out['yoy_history']),1)



    def test_regional_forecast_never_uses_negative_skill_model(self):
        hist=[{'date':f'2024-{(i%12)+1:02d}-01','value':5.0} for i in range(24)]
        out=global_m2._regional_yoy_forecast(hist,3)
        self.assertGreaterEqual(out.get('skill_pct',0),0)
        if out.get('skill_pct',0) <= 0:
            self.assertTrue(out.get('fallback_used'))

    def test_forward_liquidity_excludes_failed_us_real_rate_forecast(self):
        ctx={
            'us_engine':{
                'dxy':{'available':True,'current':100.0,'forecast_3m':100.0,'forecast_change_3m_pct':0.0,'backtest_3m':{'skill_pct':0.0,'fallback_used':True},'source':'Yahoo'},
                'real_rate':{'available':True,'current_pct':2.4,'forecast_3m_pct':2.1,'forecast_usable_3m':False,'forecast_quality_gate':{'passed':False},'source':'FRED DFII10'}
            },
            'card8':{}
        }
        out=global_m2._forward_liquidity_outlook(5.0,5.1,ctx)
        rr=[x for x in out['inputs'] if x['name'].startswith('미국 10년 실질금리')][0]
        self.assertEqual(rr['weight'],0.0)
        self.assertEqual(rr['signal'],0.0)
        self.assertEqual(rr['validation'],'미통과·상위합성 제외')

    def test_forward_liquidity_prefers_validated_us_real_rate_over_card8(self):
        ctx={
            'us_engine':{'real_rate':{'available':True,'current_pct':2.4,'forecast_3m_pct':2.2,'forecast_usable_3m':True,'forecast_quality_gate':{'passed':True},'source':'US engine real rate'}},
            'card8':{'current':{'DFII10':{'value':2.4}},'forecasts':{'3m':{'targets':{'DFII10':{'forecast':2.8,'quality_gate':{'passed':True}}}}}}
        }
        out=global_m2._forward_liquidity_outlook(5.0,5.0,ctx)
        rr=[x for x in out['inputs'] if x['name'].startswith('미국 10년 실질금리')][0]
        self.assertEqual(rr['forecast'],2.2)
        self.assertEqual(rr['origin'],'US Fed engine')
        self.assertGreater(rr['signal'],0)


    def test_pbc_money_supply_table_parser_extracts_m2_levels(self):
        html = """<table>
        <tr><td>Item</td><td>2024.01</td><td>2024.02</td><td>2024.03</td></tr>
        <tr><td>货币和准货币（M2） Money & Quasi-money</td><td>2976250.20</td><td>2995572.97</td><td>3047952.16</td></tr>
        </table>"""
        rows=global_m2._extract_pbc_money_supply_levels(html)
        self.assertEqual([r['date'] for r in rows],['2024-01-01','2024-02-01','2024-03-01'])
        self.assertAlmostEqual(rows[-1]['level'],3047952.16)

    def test_pbc_yoy_from_level_history(self):
        levels=[]
        for y,m,v in [(2024,1,100),(2024,2,110),(2025,1,108),(2025,2,121)]:
            levels.append({'date':f'{y:04d}-{m:02d}-01','level':v})
        rows=global_m2._yoy_from_level_history(levels)
        self.assertEqual(len(rows),2)
        self.assertAlmostEqual(rows[0]['value'],8.0)
        self.assertAlmostEqual(rows[1]['value'],10.0)
if __name__ == '__main__':
    unittest.main()

class FinalGlobalM2ImprovementTests(unittest.TestCase):
    def test_pbc_money_supply_parser_accepts_english_month_headers(self):
        html='''<table><tr><td>Item</td><td>Jan. 2024</td><td>Feb. 2024</td><td>Mar. 2024</td></tr><tr><td>Money & Quasi-money (M2)</td><td>2976250</td><td>2995573</td><td>3047952</td></tr></table>'''
        rows=global_m2._extract_pbc_money_supply_levels(html)
        self.assertEqual([x['date'] for x in rows],['2024-01-01','2024-02-01','2024-03-01'])

    def test_global_composite_validation_requires_all_regions(self):
        out=global_m2._global_m2_composite_validation({'US':{'yoy_history':[{'date':'2025-01-01','value':5}]}})
        self.assertFalse(out['available'])
        self.assertEqual(out['status'],'INSUFFICIENT_HISTORY')

class FinalCnGapTargetTests(unittest.TestCase):
    def test_missing_recent_months_targets_only_required_window(self):
        hist=[{'date':f'2026-{m:02d}-01','value':7.0} for m in range(1,8)]
        missing=global_m2._missing_recent_months(hist,'2026-07',18)
        self.assertEqual(len(missing),11)
        self.assertEqual(missing[0],'2025-02')
        self.assertEqual(missing[-1],'2025-12')
        self.assertNotIn('2025-01',missing)
