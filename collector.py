from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "public" / "data" / "ism_manufacturing.json"
STATUS = ROOT / "public" / "data" / "source_status.json"

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

SEED: list[dict[str, Any]] = [
    {
        "date": "2026-04", "pmi": 52.7, "newOrders": 54.1, "production": 53.4,
        "employment": 46.4, "supplierDeliveries": 60.6, "inventories": 49.0,
        "prices": 84.6,
        "sourceUrl": "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/april/",
    },
    {
        "date": "2026-05", "pmi": 54.0, "newOrders": 56.8, "production": 54.3,
        "employment": 48.6, "supplierDeliveries": 60.6, "inventories": 49.9,
        "prices": 82.1,
        "sourceUrl": "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/may/",
    },
    {
        "date": "2026-06", "pmi": 53.3, "newOrders": 56.0, "production": 52.2,
        "employment": 49.7, "supplierDeliveries": 57.4, "inventories": 51.4,
        "prices": 73.0,
        "sourceUrl": "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/june/",
    },
]


def number(patterns: list[str], text: str) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            try:
                return float(match.group(1))
            except (TypeError, ValueError):
                pass
    return None


def parse_report(html: str, url: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.stripped_strings)

    month_match = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d{2})\b", text, re.I)
    if not month_match:
        return None
    month_name = month_match.group(1)[:3].title()
    month_num = {
        "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    }[month_name]
    date = f"{month_match.group(2)}-{month_num:02d}"

    fields = {
        "pmi": number([
            r"Manufacturing PMI[^.]{0,120}?(?:registered|registering)\s+([0-9]{1,3}(?:\.[0-9]+)?)\s*percent",
        ], text),
        "newOrders": number([
            r"New Orders Index[^.]{0,180}?(?:registered|registering|reading of)\s+([0-9]{1,3}(?:\.[0-9]+)?)\s*percent",
            r"New Orders[^|]{0,100}?\|\s*" + re.escape(month_name) + r"\s+20\d{2}[^|]*\|\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*$",
        ], text),
        "production": number([
            r"Production Index[^.]{0,180}?(?:registered|registering|reading of)\s+([0-9]{1,3}(?:\.[0-9]+)?)\s*percent",
            r"Production Index\s*\(([0-9]{1,3}(?:\.[0-9]+)?)\s*percent\)",
        ], text),
        "employment": number([
            r"Employment Index[^.]{0,180}?(?:registered|registering)\s+([0-9]{1,3}(?:\.[0-9]+)?)\s*percent",
        ], text),
        "supplierDeliveries": number([
            r"Supplier Deliveries Index[^.]{0,220}?(?:registered|registering|reading of)\s+([0-9]{1,3}(?:\.[0-9]+)?)\s*percent",
        ], text),
        "inventories": number([
            r"Inventories Index[^.]{0,180}?(?:registered|registering)\s+([0-9]{1,3}(?:\.[0-9]+)?)\s*percent",
        ], text),
        "prices": number([
            r"Prices Index[^.]{0,180}?(?:registered|registering)\s+([0-9]{1,3}(?:\.[0-9]+)?)\s*percent",
        ], text),
    }

    if fields["newOrders"] is None or fields["inventories"] is None:
        return None

    return {"date": date, **fields, "sourceUrl": url}


def current_report_url(session: requests.Session) -> str:
    """Discover the current Manufacturing PMI report from the official ISM hub.

    One hub request replaces the old 12-month fan-out.  The hub owns the current
    report link, so we do not guess future month slugs (which previously caused
    SSO/404 noise).
    """
    hub = "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/"
    response = session.get(hub, timeout=(4, 8))
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        text = " ".join(anchor.stripped_strings).lower()
        low = href.lower()
        if "/ism-pmi-reports/pmi/" not in low or "/services/" in low:
            continue
        if href.startswith("/"):
            href = "https://www.ismworld.org" + href
        if href.startswith("https://www.ismworld.org/"):
            candidates.append(href)
            if "view report" in text:
                return href
    if candidates:
        return candidates[0]
    raise ValueError("ISM current Manufacturing report link not found on official hub")


def fetch_current_report(session: requests.Session) -> tuple[str, dict[str, Any] | None]:
    url = current_report_url(session)
    response = session.get(url, timeout=(4, 8))
    response.raise_for_status()
    return url, parse_report(response.text, url)

def merge_history(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date = {str(row["date"]): row for row in existing if row.get("date")}
    for row in incoming:
        if row.get("date"):
            by_date[str(row["date"])] = row
    return [by_date[key] for key in sorted(by_date)]


def main() -> None:
    previous: dict[str, Any] = {}
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    history = merge_history(SEED, previous.get("history", []))
    errors: list[str] = []
    found: list[dict[str, Any]] = []

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; GlobalMacroDataCollector/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    })

    # Low-call path: one official hub request + one current-report request.
    # If ISM transiently blocks GitHub Actions, keep the last-good history and
    # report degraded status instead of fanning out across 12 month URLs.
    try:
        url, parsed = fetch_current_report(session)
        if parsed:
            found.append(parsed)
        else:
            errors.append(f"{url}: report page fetched but required PMI fields were not parsed")
    except Exception as exc:
        errors.append(f"official current-report discovery/fetch: {type(exc).__name__}: {exc}")

    history = merge_history(history, found)
    if not history:
        raise RuntimeError("No valid ISM history and no last-good data")

    latest = history[-1]
    payload = {
        "schema_version": "1.1",
        "collector_version": "2.6.0-low-call-ism-current-report",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Institute for Supply Management Manufacturing PMI reports",
        "source_url": latest.get("sourceUrl"),
        "latest": latest,
        "history": history,
        "warnings": errors[-5:],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    status = {
        "generated_at_utc": payload["generated_at_utc"],
        "ok": bool(found),
        "new_reports_found": len(found),
        "history_count": len(history),
        "latest_date": latest.get("date"),
        "errors": errors[-10:],
        "request_strategy": "official_hub_then_current_report_max_2_requests",
        "last_good_reused": not bool(found),
    }
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
    from treasury_card8 import main as card8_main
    card8_main()
    from macro_cards_9_12 import main as cards_main
    cards_main()
    # Equity fundamentals are part of the collector contract. This guarantees
    # equity_fundamentals.json is generated even when an older workflow only
    # runs `python collector.py` and omits a separate equity step.
    from equity_fundamentals import main as equity_main
    equity_main()
