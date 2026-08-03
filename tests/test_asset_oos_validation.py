import unittest
import numpy as np
from asset_oos_validation import rolling_oos

class AssetOosValidationTests(unittest.TestCase):
    def test_insufficient_is_conservative(self):
        x=np.ones((100,2)); p=np.linspace(100,110,100)
        out=rolling_oos(x,p,21)
        self.assertEqual(out['status'],'insufficient')
        self.assertEqual(out['weight_multiplier'],0.0)

    def test_negative_skill_never_receives_weight(self):
        rng=np.random.default_rng(11)
        n=3000
        x=rng.normal(size=(n,3))
        price=100*np.exp(np.cumsum(rng.normal(0,.01,n)))
        out=rolling_oos(x,price,21)
        if out.get('skill_pct',0) <= 0:
            self.assertEqual(out['weight_multiplier'],0.0)
            self.assertFalse(out.get('production_eligible',False))

    def test_output_multiplier_is_bounded(self):
        rng=np.random.default_rng(7)
        n=2400
        x=rng.normal(size=(n,3))
        ret=np.r_[np.zeros(1),0.002*x[:-1,0]+rng.normal(0,.008,n-1)]
        price=100*np.exp(np.cumsum(ret))
        out=rolling_oos(x,price,21)
        self.assertGreaterEqual(out['weight_multiplier'],0)
        self.assertLessEqual(out['weight_multiplier'],1)

if __name__=='__main__': unittest.main()
