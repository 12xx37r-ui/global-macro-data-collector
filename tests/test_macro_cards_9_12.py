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
