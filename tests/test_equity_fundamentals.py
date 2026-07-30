import unittest
import equity_fundamentals as m

class EquityFundamentalsTests(unittest.TestCase):
    def test_universes_cover_both_indices(self):
        self.assertIn("sp500", m.UNIVERSES)
        self.assertIn("nasdaq", m.UNIVERSES)
        self.assertGreaterEqual(len(m.UNIVERSES["sp500"]["symbols"]), 15)
        self.assertGreaterEqual(len(m.UNIVERSES["nasdaq"]["symbols"]), 15)

    def test_aggregate_retains_previous_metric(self):
        prev={"values":{"forward_pe":22.0,"trailing_pe":25.0,"price_sales":3.0,"eps_growth_pct":10.0}}
        out=m.aggregate({}, prev)
        self.assertEqual(out["values"]["forward_pe"],22.0)
        self.assertIn("forward_pe",out["stale_metrics"])

if __name__ == "__main__":
    unittest.main()
