import unittest
import asset_oos_validation as m

class ReitAssetRequiredTest(unittest.TestCase):
    def test_reit_asset_and_futures_are_configured(self):
        self.assertIn("reit", m.ASSETS)
        self.assertEqual(m.ASSETS["reit"]["ticker"], "VNQ")
        self.assertIn("reit", m.CARD12_TICKERS)
        self.assertIn("ES=F", m.CARD12_TICKERS["reit"])

if __name__ == "__main__":
    unittest.main()
