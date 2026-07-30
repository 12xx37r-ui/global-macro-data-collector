import unittest
import asset_oos_validation as m

class NasdaqAssetRequiredTest(unittest.TestCase):
    def test_nasdaq_asset_and_futures_are_configured(self):
        self.assertIn("nasdaq", m.ASSETS)
        self.assertEqual(m.ASSETS["nasdaq"]["ticker"], "QQQ")
        self.assertIn("nasdaq", m.CARD12_TICKERS)
        self.assertIn("NQ=F", m.CARD12_TICKERS["nasdaq"])

if __name__ == "__main__":
    unittest.main()
