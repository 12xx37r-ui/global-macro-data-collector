import unittest
from treasury_card8 import candidate_forecasts, select_model_oos, grade_strength, horizon_gate

class Card8Test(unittest.TestCase):
    def test_candidate_set(self):
        vals=[2.0+i*0.001 for i in range(400)]
        names=[x[0] for x in candidate_forecasts(vals,21,0.1)]
        self.assertIn('persistence',names)
        self.assertIn('structural_blend',names)

    def test_oos_no_negative_skill(self):
        vals=[3.0+0.2*((i%40)/40) for i in range(1200)]
        r=select_model_oos(vals,21,0.0)
        self.assertGreaterEqual(r['skill_pct'],0)
        self.assertGreater(r['samples'],100)

    def test_tiny_skill_does_not_pass(self):
        r={'samples':900,'skill_pct':0.10,'active_direction_accuracy':0.60,'active_direction_coverage':0.60,'interval80_coverage':0.8,'dm_test':{'significant_10pct':True},'fallback_used':False}
        self.assertFalse(horizon_gate(r,180,'5d')['passed'])

    def test_investment_color(self):
        self.assertEqual(grade_strength(4.5,4.0,True)['signal'],'good')
        self.assertEqual(grade_strength(4.0,4.5,True)['signal'],'bad')

    def test_gate(self):
        r={'samples':200,'skill_pct':2.5,'active_direction_accuracy':0.56,'active_direction_coverage':0.45,'interval80_coverage':0.8,'dm_test':{'significant_10pct':True},'fallback_used':False}
        self.assertTrue(horizon_gate(r,180,'5d')['passed'])

if __name__=='__main__': unittest.main()

class FredFetchParserTest(unittest.TestCase):
    def test_parse_multi_series_csv(self):
        from treasury_card8 import parse_fred_csv
        text = "observation_date,DGS2,DGS10,DFII10,T10Y2Y\n2026-01-02,4.10,4.30,2.00,0.20\n2026-01-03,.,4.31,2.01,.\n"
        out = parse_fred_csv(text, ["DGS2", "DGS10", "DFII10", "T10Y2Y"])
        self.assertEqual(len(out["DGS2"]), 1)
        self.assertEqual(len(out["DGS10"]), 2)
        self.assertAlmostEqual(out["DFII10"][0]["value"], 2.0)

    def test_parse_date_header_variant(self):
        from treasury_card8 import parse_fred_csv
        text = "DATE,DGS2\n2026-01-02,4.10\n"
        out = parse_fred_csv(text, ["DGS2"])
        self.assertEqual(out["DGS2"][0]["date"], "2026-01-02")
