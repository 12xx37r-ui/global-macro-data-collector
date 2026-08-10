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


def current_report_url(session: requests.Session, expected_month_url: str | None = None) -> str:
    """Discover the current Manufacturing PMI report from the official ISM hub.

    The preferred path is one hub request plus one report request.  Some ISM
    responses served to GitHub Actions expose the visible report card but omit
    or transform its href, so we also inspect raw HTML.  If discovery still
    fails, we use one deterministic expected-month official URL derived from
    last-good history; there is no month fan-out.
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
            if "view report" in text or "manufacturing" in text:
                return href

    raw_matches = re.findall(
        r"href=['\"]([^'\"]*/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/[a-z]+/?)['\"]",
        response.text,
        re.I,
    )
    for href in raw_matches:
        if "/services/" in href.lower():
            continue
        if href.startswith("/"):
            href = "https://www.ismworld.org" + href
        if href.startswith("https://www.ismworld.org/"):
            return href

    if candidates:
        return candidates[0]
    if expected_month_url:
        return expected_month_url
    raise ValueError("ISM current Manufacturing report link not found on official hub")


def _next_month_report_url(latest_date: str | None) -> str | None:
    """Return exactly one expected official month URL from last-good history."""
    if not latest_date or not re.fullmatch(r"20\d{2}-\d{2}", str(latest_date)):
        return None
    y, m = map(int, str(latest_date).split("-"))
    if m == 12:
        y += 1
        m = 1
    else:
        m += 1

    # Do not request a future/unreleased data month. Since an ISM Manufacturing
    # report published in month T describes month T-1, previous calendar month
    # is the furthest safe deterministic fallback.
    now = datetime.now(timezone.utc)
    prev_y, prev_m = ((now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1))
    if (y, m) > (prev_y, prev_m):
        return None
    slug = MONTHS[m - 1]
    return f"https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/{slug}/"


def _latest_expected_data_month() -> str:
    now = datetime.now(timezone.utc)
    y, m = ((now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1))
    return f"{y:04d}-{m:02d}"


def fetch_current_report(session: requests.Session, latest_date: str | None = None) -> tuple[str, dict[str, Any] | None]:
    url = current_report_url(session, _next_month_report_url(latest_date))
    response = session.get(url, timeout=(4, 8), headers={"Connection": "close", "Accept-Language": "en-US,en;q=0.9"})
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

    # Zero-call fast path: once the latest released data month is already in
    # local history, do not hit ISM again until a newer month can exist.
    latest_before = history[-1].get("date") if history else None
    already_current = bool(latest_before and latest_before >= _latest_expected_data_month())
    if not already_current:
        # Low-call refresh path: one official hub request + one current-report
        # request. If the hub omits href metadata, the second request uses the
        # single deterministic next-month official URL. No month fan-out.
        try:
            url, parsed = fetch_current_report(session, latest_before)
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
        "collector_version": "2.7.0-low-call-ism-resilient-discovery",
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
        "ok": bool(found) or already_current,
        "new_reports_found": len(found),
        "history_count": len(history),
        "latest_date": latest.get("date"),
        "errors": errors[-10:],
        "request_strategy": "zero_if_current_else_official_hub_then_discovered_or_expected_next_report_max_2_requests",
        "network_skipped_fresh": already_current,
        "last_good_reused": (not bool(found)) and (not already_current),
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
