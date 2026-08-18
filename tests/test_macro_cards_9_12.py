import unittest
from macro_cards_9_12 import candidates, walk_forward, build_card11, _snapshot_oos_metrics, _global_registry

class TestCards(unittest.TestCase):
    def test_candidates(self):
        x=list(range(100))
        self.assertIn('persistence',candidates(x,3))
    def test_walk_forward(self):
        vals=[50+(i%12)*0.2 for i in range(250)]
        r=walk_forward(vals,3,30)
        self.assertIn('forecast',r)
        self.assertIn('quality_gate',r)
    def test_card11(self):
        c={'market_signal':'good','data_quality':{'core_completeness':100}}
        r=build_card11(c,c,c,c)
        self.assertEqual(r['card'],11)
        self.assertEqual(r['market_signal'],'good')

    def test_card11_has_separate_validated_future_gate(self):
        c8={'market_signal':'neutral','quality_gates':{},'data_quality':{'core_completeness':100},'forecasts':{}}
        fc={'forecast':55,'quality_gate':{'passed':True}}
        c9={'market_signal':'good','current':50,'forecasts':{'3m':fc},'data_quality':{'core_completeness':100}}
        c10={'market_signal':'neutral','current':50,'forecasts':{'3m':{'forecast':48,'quality_gate':{'passed':True}}},'data_quality':{'core_completeness':100}}
        c12={'market_signal':'neutral','predictive_validation':{'groups':{},'passed_horizons':[]},'data_quality':{'completeness':100}}
        out=build_card11(c8,c9,c10,c12)
        self.assertIn('future_score',out)
        self.assertTrue(out['future_quality_gate']['passed'])


    def test_snapshot_oos_metrics_matures_without_lookahead(self):
        snaps=[]
        hist={'ES=F':[],'NQ=F':[],'BTC=F':[]}
        from datetime import date, timedelta
        base=date(2020,1,1)
        for i in range(30):
            d=base+timedelta(days=i*100)
            start=100+i
            sign=1 if i%2==0 else -1
            snaps.append({'date':d.isoformat(),'forward_liquidity_score':10*sign,'card11_future_score':20*sign,'asset_start':{'ES=F':start,'NQ=F':start,'BTC=F':start}})
            end=start*(1.05 if sign>0 else .95)
            for sym in hist:
                hist[sym].append({'date':(d+timedelta(days=90)).isoformat(),'value':end})
        out=_snapshot_oos_metrics(snaps,hist)
        self.assertTrue(out['forward_liquidity']['3m']['sp500']['quality_gate']['passed'])
        self.assertEqual(out['forward_liquidity']['3m']['sp500']['samples'],30)

    def test_global_registry_keeps_composite_reference_only_until_all_components_pass(self):
        gm={'current':5,'forecast':5.1,'forecast_components':{'US':{'validation':{'quality_gate':{'passed':True}}},'CN':{'validation':{'quality_gate':{'passed':False}}}},'forward_liquidity_outlook':{'score':5,'validation_status':'PARTIALLY_VALIDATED_INPUTS'}}
        c8={'quality_gates':{'3m':{'passed':False,'passed_targets':['DGS2']}}}
        c9={'forecast_use':{'usable':False},'forecasts':{},'current':50}
        c10={'forecast_use':{'usable':True},'forecasts':{'3m':{'quality_gate':{'passed':True}}},'current':50,'forecast_3m':51}
        c11={'future_score':-20,'future_quality_gate':{'passed':True}}
        c12={'predictive_validation':{'passed_horizons':['equity:3m']}}
        oos={'metrics':{'forward_liquidity':{'3m':{}},'card11_future':{'3m':{}}}}
        reg=_global_registry(gm,c8,c9,c10,c11,c12,oos)
        by={x['id']:x for x in reg['entries']}
        self.assertEqual(by['global_m2_3m']['status'],'REFERENCE_ONLY')
        self.assertEqual(by['card10_3m']['status'],'VALIDATED')
        self.assertEqual(by['card11_future_3m']['status'],'REFERENCE_ONLY')
if __name__=='__main__': unittest.main()


class TestOfficialSourceParsers(unittest.TestCase):
    def test_oecd_cli_csv(self):
        from macro_cards_9_12 import _parse_oecd_cli_csv
        text='TIME_PERIOD,OBS_VALUE\n2026-05,100.21\n2026-06,100.36\n'
        rows=_parse_oecd_cli_csv(text)
        self.assertEqual(rows[-1]['date'],'2026-06-01')
        self.assertAlmostEqual(rows[-1]['value'],100.36)

    def test_gscpi_xlsx_parser(self):
        import io
        from openpyxl import Workbook
        from datetime import datetime
        from macro_cards_9_12 import _parse_gscpi_excel
        wb=Workbook(); ws=wb.active
        ws.append(['Date','GSCPI'])
        for i in range(48):
            y=2022+i//12; m=i%12+1
            ws.append([datetime(y,m,1), (i-24)/10])
        bio=io.BytesIO(); wb.save(bio)
        rows=_parse_gscpi_excel(bio.getvalue())
        self.assertEqual(len(rows),48)
        self.assertEqual(rows[-1]['date'],'2025-12-01')

    def test_gscpi_legacy_xls_dispatch(self):
        from unittest.mock import patch
        from macro_cards_9_12 import _parse_gscpi_excel
        ole=b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'+b'x'*2048
        fake=[{'date':'2026-07-01','value':0.1}]*36
        with patch('macro_cards_9_12._parse_gscpi_xls', return_value=fake) as fn:
            out=_parse_gscpi_excel(ole)
            self.assertEqual(len(out),36)
            fn.assert_called_once()

class TestGscpiLegacyLayouts(unittest.TestCase):
    def test_gscpi_matrix_accepts_yyyym_month_keys_and_numeric_strings(self):
        from macro_cards_9_12 import _extract_gscpi_rows_matrix
        matrix=[['Month','GSCPI']]
        for i in range(48):
            y=2022+i//12; m=i%12+1
            matrix.append([f'{y}m{m}', str((i-24)/10)])
        rows=_extract_gscpi_rows_matrix(matrix)
        self.assertEqual(len(rows),48)
        self.assertEqual(rows[-1]['date'],'2025-12-01')
        self.assertAlmostEqual(rows[-1]['value'],2.3)

    def test_gscpi_matrix_accepts_plain_excel_serial_dates(self):
        from macro_cards_9_12 import _extract_gscpi_rows_matrix
        from datetime import datetime
        origin=datetime(1899,12,30)
        matrix=[['Date','GSCPI']]
        for i in range(48):
            y=2022+i//12; m=i%12+1
            serial=(datetime(y,m,1)-origin).days
            matrix.append([serial, (i-24)/10])
        rows=_extract_gscpi_rows_matrix(matrix)
        self.assertEqual(len(rows),48)
        self.assertEqual(rows[0]['date'],'2022-01-01')


class TestGscpiOrientationResilience(unittest.TestCase):
    def test_gscpi_matrix_accepts_horizontal_wide_layout(self):
        from macro_cards_9_12 import _extract_gscpi_rows_matrix
        dates=['Month']
        values=['GSCPI']
        for i in range(48):
            y=2022+i//12; m=i%12+1
            dates.append(f'{y}-{m:02d}')
            values.append((i-24)/10)
        matrix=[['NY Fed GSCPI chart data'], dates, values]
        rows=_extract_gscpi_rows_matrix(matrix)
        self.assertEqual(len(rows),48)
        self.assertEqual(rows[0]['date'],'2022-01-01')
        self.assertEqual(rows[-1]['date'],'2025-12-01')

    def test_gscpi_matrix_accepts_iso_timestamp_strings(self):
        from macro_cards_9_12 import _extract_gscpi_rows_matrix
        matrix=[['Date','GSCPI']]
        for i in range(48):
            y=2022+i//12; m=i%12+1
            matrix.append([f'{y}-{m:02d}-01 00:00:00', (i-24)/10])
        rows=_extract_gscpi_rows_matrix(matrix)
        self.assertEqual(len(rows),48)
        self.assertEqual(rows[-1]['date'],'2025-12-01')

    def test_gscpi_numeric_serial_respects_1904_datemode(self):
        from macro_cards_9_12 import _extract_gscpi_rows_matrix
        from datetime import datetime
        origin=datetime(1904,1,1)
        matrix=[['Date','GSCPI']]
        for i in range(48):
            y=2022+i//12; m=i%12+1
            serial=(datetime(y,m,1)-origin).days
            matrix.append([serial, (i-24)/10])
        rows=_extract_gscpi_rows_matrix(matrix, datemode=1)
        self.assertEqual(len(rows),48)
        self.assertEqual(rows[0]['date'],'2022-01-01')

class TestGscpiLiveShapeResilience(unittest.TestCase):
    def test_gscpi_four_column_shifted_yyyymm_numeric_layout(self):
        from macro_cards_9_12 import _extract_gscpi_rows_matrix
        # Mimic live legacy sheet shape: 4 columns, metadata rows, actual data shifted right.
        matrix=[['Date','GSCPI',None,None],
                ['NEW YORK FED  ECONOMIC RESEARCH',None,None,None],
                ['https://www.newyorkfed.org/research',None,None,None],
                [None,None,'Date','GSCPI']]
        for i in range(120):
            y=2016+i//12; m=i%12+1
            val=(i-60)/25
            matrix.append([None,None,float(f'{y}{m:02d}'), f'{val:.4f}'])
        rows=_extract_gscpi_rows_matrix(matrix)
        self.assertEqual(len(rows),120)
        self.assertEqual(rows[0]['date'],'2016-01-01')
        self.assertEqual(rows[-1]['date'],'2025-12-01')

    def test_gscpi_day_first_text_dates_and_unicode_minus(self):
        from macro_cards_9_12 import _extract_gscpi_rows_matrix
        from datetime import datetime
        matrix=[['Date','GSCPI']]
        for i in range(48):
            y=2022+i//12; m=i%12+1
            d=datetime(y,m,1).strftime('%d-%b-%Y')
            raw=f'{abs((i-24)/10):.2f}'
            if i < 24:
                raw='−'+raw
            matrix.append([d, raw])
        rows=_extract_gscpi_rows_matrix(matrix)
        self.assertEqual(len(rows),48)
        self.assertLess(rows[0]['value'],0)
        self.assertEqual(rows[-1]['date'],'2025-12-01')

    def test_gscpi_numeric_serial_as_text(self):
        from macro_cards_9_12 import _extract_gscpi_rows_matrix
        from datetime import datetime
        origin=datetime(1899,12,30)
        matrix=[['Date','GSCPI']]
        for i in range(48):
            y=2022+i//12; m=i%12+1
            serial=(datetime(y,m,1)-origin).days
            matrix.append([f'{serial}.0', (i-24)/10])
        rows=_extract_gscpi_rows_matrix(matrix)
        self.assertEqual(len(rows),48)
        self.assertEqual(rows[0]['date'],'2022-01-01')

    def test_gscpi_profile_inference_ignores_metadata_headers(self):
        from macro_cards_9_12 import _extract_gscpi_by_column_profile
        matrix=[['Date','GSCPI',None,None],
                ['NEW YORK FED  ECONOMIC RESEARCH',None,None,None],
                ['https://www.newyorkfed.org/research',None,None,None]]
        for i in range(72):
            y=2020+i//12; m=i%12+1
            matrix.append([None,'note',f'{y}-{m:02d}-01', (i-36)/10])
        rows=_extract_gscpi_by_column_profile(matrix)
        self.assertEqual(len(rows),72)
        self.assertEqual(rows[-1]['date'],'2025-12-01')

class TestGscpiLowCallPolicy(unittest.TestCase):
    def test_gscpi_success_uses_one_official_request(self):
        from unittest.mock import Mock, patch
        from macro_cards_9_12 import fetch_gscpi_official
        session=Mock(); response=Mock()
        response.content=b'PK'+b'x'*4096
        response.raise_for_status=Mock()
        session.get.return_value=response
        fake=[{'date':f'2023-{m:02d}-01','value':0.1} for m in range(1,13)]*3
        with patch('macro_cards_9_12._parse_gscpi_excel', return_value=fake), \
             patch('macro_cards_9_12._write_gscpi_cache'):
            rows,source=fetch_gscpi_official(session)
        self.assertEqual(session.get.call_count,1)
        self.assertEqual(len(rows),36)
        self.assertEqual(source,'New York Fed official workbook')

    def test_gscpi_failure_does_not_add_network_fallback(self):
        from unittest.mock import Mock, patch
        from macro_cards_9_12 import fetch_gscpi_official
        session=Mock(); response=Mock()
        response.content=b'PK'+b'x'*4096
        response.raise_for_status=Mock()
        session.get.return_value=response
        with patch('macro_cards_9_12._parse_gscpi_excel', side_effect=ValueError('bad layout')), \
             patch('macro_cards_9_12._read_gscpi_cache', return_value=[]):
            with self.assertRaises(RuntimeError):
                fetch_gscpi_official(session)
        self.assertEqual(session.get.call_count,1)

class FinalHealthTests(unittest.TestCase):
    def test_engine_health_reports_fallbacks_and_counts(self):
        from macro_cards_9_12 import _engine_health
        gm={'coverage_weight':1.0,'coverage_regions':['US','CN','EA','JP'],'coverage_quality':'FULL','components':{'US':{'status':'LIVE'}},'forecast_components':{'CN':{'validation':{'fallback_used':True}}},'api_health':{},'runtime_ms':123}
        reg={'entries':[{'id':'x','degradation_status':'STABLE'}],'summary':{'validated':1,'reference_only':0,'degraded':0}}
        out=_engine_health(gm,reg,{'status':'ACTIVE_ACCUMULATION','snapshot_count':1})
        self.assertIn('global_m2:CN',out['fallbacks'])
        self.assertEqual(out['runtime_ms'],123)
