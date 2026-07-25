import unittest
from collector import parse_report


class CollectorTest(unittest.TestCase):
    def test_parse_report(self):
        html = """
        <html><body>
        Jun 2026
        The Manufacturing PMI registered 53.3 percent in June.
        The New Orders Index expanded, registering 56 percent.
        The Production Index registered 52.2 percent.
        The Employment Index registered 49.7 percent.
        The Supplier Deliveries Index registered 57.4 percent.
        The Inventories Index registered 51.4 percent.
        The Prices Index registered 73 percent.
        </body></html>
        """
        row = parse_report(html, "test")
        self.assertEqual(row["date"], "2026-06")
        self.assertEqual(row["newOrders"], 56.0)
        self.assertEqual(row["inventories"], 51.4)


if __name__ == "__main__":
    unittest.main()
