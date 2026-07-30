import unittest
import numpy as np
from asset_oos_validation import rolling_oos

class AssetOosValidationTests(unittest.TestCase):
    def test_insufficient_is_conservative(self):
        x=np.ones((100,2)); p=np.linspace(100,110,100)
        out=rolling_oos(x,p,21)
        self.assertEqual(out['status'],'insufficient')
        self.assertEqual(out['weight_multiplier'],0.25)

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
