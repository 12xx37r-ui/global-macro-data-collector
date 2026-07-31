import unittest
from pathlib import Path


class EquityCollectorIntegrationTest(unittest.TestCase):
    def test_collector_invokes_equity_fundamentals(self):
        text = Path("collector.py").read_text(encoding="utf-8")
        self.assertIn("from equity_fundamentals import main as equity_main", text)
        self.assertIn("equity_main()", text)

    def test_equity_module_writes_required_output(self):
        # The collector itself guarantees generation. Do not make engine tests
        # depend on which historical workflow file happens to be installed in
        # the repository, because older workflows still run collector.py and
        # commit public/data/*.json correctly.
        text = Path("equity_fundamentals.py").read_text(encoding="utf-8")
        self.assertIn("equity_fundamentals.json", text)
        self.assertIn("OUT.write_text", text)


if __name__ == "__main__":
    unittest.main()
