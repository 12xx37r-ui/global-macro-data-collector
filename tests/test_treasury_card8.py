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

    def test_investment_color(self):
        self.assertEqual(grade_strength(4.5,4.0,True)['signal'],'good')
        self.assertEqual(grade_strength(4.0,4.5,True)['signal'],'bad')

    def test_gate(self):
        r={'samples':200,'skill_pct':2.5,'direction_accuracy':0.53,'interval80_coverage':0.8}
        self.assertTrue(horizon_gate(r,180)['passed'])

if __name__=='__main__': unittest.main()
