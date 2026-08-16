import unittest
import equity_fundamentals

class TestV220OvercallGuard(unittest.TestCase):
    def test_equity_worker_cap(self):
        self.assertLessEqual(equity_fundamentals.MAX_WORKERS, 6)

if __name__ == "__main__": unittest.main()
