import json
import unittest
from pathlib import Path

from asset_oos_validation import ASSETS, GROUP_TICKERS, HORIZONS, REQUIRED_ASSETS


class AssetCoverageV159Tests(unittest.TestCase):
    def test_all_assets_have_card12_mapping(self):
        self.assertEqual(set(REQUIRED_ASSETS), set(ASSETS))
        self.assertEqual(set(REQUIRED_ASSETS), set(GROUP_TICKERS))
        for key in REQUIRED_ASSETS:
            self.assertTrue(GROUP_TICKERS[key], key)

    def test_committed_output_has_all_assets_and_horizons(self):
        path = Path(__file__).resolve().parents[1] / "public" / "data" / "asset_oos_validation.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assets = payload.get("assets") or {}
        self.assertEqual(set(REQUIRED_ASSETS), set(assets))
        for key in REQUIRED_ASSETS:
            horizons = assets[key].get("horizons") or {}
            for horizon in HORIZONS:
                self.assertIn(horizon, horizons, f"{key}:{horizon}")
                self.assertIn("card8", horizons[horizon], f"{key}:{horizon}:card8")
                self.assertIn("card12", horizons[horizon], f"{key}:{horizon}:card12")
        coverage = payload.get("coverage") or {}
        if coverage:
            self.assertTrue(coverage.get("complete"))
            self.assertEqual(coverage.get("usable_assets"), len(REQUIRED_ASSETS))


if __name__ == "__main__":
    unittest.main()
