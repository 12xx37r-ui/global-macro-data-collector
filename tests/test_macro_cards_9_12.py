import unittest
from macro_cards_9_12 import candidates, walk_forward, build_card11

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
