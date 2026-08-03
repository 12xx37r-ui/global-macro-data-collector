import unittest
from macro_cards_9_12 import YAHOO_SYMBOLS, build_card11, _validate_xlsx_response


class DummyResponse:
    def __init__(self, content, content_type='application/octet-stream'):
        self.content = content
        self.headers = {'content-type': content_type}


class TestV23Integrity(unittest.TestCase):
    def test_dollar_proxy_replaces_broken_future_symbol(self):
        self.assertNotIn('DX=F', YAHOO_SYMBOLS)
        self.assertIn('DX-Y.NYB', YAHOO_SYMBOLS)

    def test_gscpi_rejects_html_before_openpyxl(self):
        with self.assertRaises(ValueError):
            _validate_xlsx_response(DummyResponse(b'<html>' + b'x' * 2000, 'text/html'))

    def test_card11_quality_label_matches_passed_flag(self):
        card8 = {
            'market_signal': 'neutral',
            'quality_gates': {'3m': {'passed': True, 'passed_targets': ['DGS2', 'DGS10', 'DFII10']}},
            'data_quality': {'core_completeness': 100},
        }
        card9 = {
            'market_signal': 'neutral',
            'forecasts': {'3m': {'quality_gate': {'passed': True}}},
            'data_quality': {'core_completeness': 100},
        }
        card10 = {
            'market_signal': 'bad',
            'forecasts': {'3m': {'quality_gate': {'passed': True}}},
            'data_quality': {'core_completeness': 100},
        }
        card12 = {
            'market_signal': 'bad',
            'predictive_validation': {'passed_horizons': ['rates:1m']},
            'data_quality': {'completeness': 90},
        }
        result = build_card11(card8, card9, card10, card12)
        self.assertTrue(result['quality_gate']['passed'])
        self.assertEqual(result['quality_gate']['level'], '검증통과 신호 통합')


if __name__ == '__main__':
    unittest.main()
