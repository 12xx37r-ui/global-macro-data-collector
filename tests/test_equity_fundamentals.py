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



class FinvizFallbackTests(unittest.TestCase):
    def test_finviz_label_parser(self):
        class R:
            status_code=200
            text='<table><tr><td>P/E</td><td>31.25</td><td>Forward P/E</td><td>24.50</td><td>P/S</td><td>8.40</td><td>EPS next Y</td><td>18.70%</td></tr></table>'
        old=m.SESSION.get
        try:
            m.SESSION.get=lambda *a,**k:R()
            z=m.finviz_snapshot('TEST')
            self.assertEqual(z['forward_pe'],24.5)
            self.assertEqual(z['trailing_pe'],31.25)
            self.assertEqual(z['price_sales'],8.4)
            self.assertEqual(z['eps_growth_pct'],18.7)
        finally:
            m.SESSION.get=old


if __name__ == "__main__":
    unittest.main()
