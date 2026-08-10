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

class _FakeResp:
    def __init__(self, text, status=200):
        self.text=text; self.status_code=status
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)

class _FakeSession:
    def __init__(self, pages): self.pages=pages; self.calls=[]
    def get(self, url, **kwargs):
        self.calls.append(url)
        return _FakeResp(self.pages[url])

class CollectorLowCallTest(unittest.TestCase):
    def test_current_report_discovery_uses_hub_then_one_report(self):
        from collector import fetch_current_report
        hub='https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/'
        report='https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/july/'
        pages={
          hub:f'<a href="{report}">View Report</a>',
          report:'''Jul 2026 The Manufacturing PMI registered 55.6 percent. The New Orders Index reading of 56.7 percent. The Production Index reading of 58.5 percent. The Employment Index registered 52.8 percent. The Supplier Deliveries Index reading of 58.9 percent. The Inventories Index registered 51.2 percent. The Prices Index registered 71.1 percent.'''
        }
        session=_FakeSession(pages)
        url,row=fetch_current_report(session)
        self.assertEqual(url, report)
        self.assertEqual(row['date'],'2026-07')
        self.assertEqual(row['newOrders'],56.7)
        self.assertEqual(len(session.calls),2)
