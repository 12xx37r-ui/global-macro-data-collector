import unittest
from pathlib import Path


class EquityCollectorIntegrationTest(unittest.TestCase):
    def test_collector_invokes_equity_fundamentals(self):
        text = Path("collector.py").read_text(encoding="utf-8")
        self.assertIn("from equity_fundamentals import main as equity_main", text)
        self.assertIn("equity_main()", text)

    def test_workflow_verifies_equity_output(self):
        text = Path(".github/workflows/update-data.yml").read_text(encoding="utf-8")
        self.assertIn("test -f public/data/equity_fundamentals.json", text)


if __name__ == "__main__":
    unittest.main()
