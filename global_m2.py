from __future__ import annotations

import csv
import io
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "public" / "data"
CACHE_PATH = OUT_DIR / "cache" / "global_m2_last_good.json"

TIMEOUT = (5, 15)
# Strategic weights, not a GDP-nowcast. Missing components are re-normalized.
WEIGHTS = {"US": 0.40, "CN": 0.30, "EA": 0.20, "JP": 0.10}
MAX_AGE_DAYS = {"US": 100, "CN": 100, "EA": 100, "JP": 100}

FRED_US_M2 = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=M2SL&cosd=2018-01-01"
ECB_M2_KEY = "BSI.M.U2.Y.V.M20.X.I.U2.2300.Z01.A"
ECB_M2_CSV = f"https://data-api.ecb.europa.eu/service/data/BSI/{ECB_M2_KEY[4:]}?format=csvdata&startPeriod=2018-01"
BOJ_M2_PAGE = "https://www.stat-search.boj.or.jp/ssi/mtshtml/mam1yam2m2mo.html"
PBC_SEARCH = "https://wzdig.pbc.gov.cn/search/pcRender?pNo=1&pageId=c177a85bd02b4114bebebd210809f691&q=M2&sr=pubDate%20desc"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v: Any) -> float | None:
    try:
        x = float(str(v).replace(",", "").strip())
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _month_key(s: str) -> str | None:
    s = str(s or "").strip()
    m = re.match(r"^(20\d{2})[-/](0?[1-9]|1[0-2])", s)
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}" if m else None


def _age_days(dateish: str | None) -> int | None:
    if not dateish:
        return None
    try:
        d = datetime.fromisoformat(str(dateish)[:10]).replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - d).total_seconds() // 86400))
    except Exception:
        return None


def _series_yoy_from_levels(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    clean = []
    for r in rows:
        mk = _month_key(r.get("date"))
        v = _num(r.get("value"))
        if mk and v is not None and v > 0:
            clean.append((mk, v))
    by_month = {m: v for m, v in clean}
    months = sorted(by_month)
    yoy = []
    for m in months:
        y, mo = map(int, m.split("-"))
        prev = f"{y-1:04d}-{mo:02d}"
        if prev in by_month and by_month[prev] > 0:
            yoy.append((m, (by_month[m] / by_month[prev] - 1.0) * 100.0))
    if not yoy:
        return None
    latest_m, latest_yoy = yoy[-1]
    prior3 = yoy[-4][1] if len(yoy) >= 4 else yoy[0][1]
    return {"date": latest_m + "-01", "yoy_pct": latest_yoy, "yoy_3m_ago_pct": prior3, "level": by_month[latest_m]}


def _fetch_us(session: requests.Session) -> dict[str, Any]:
    r = session.get(FRED_US_M2, timeout=TIMEOUT)
    r.raise_for_status()
    rows = []
    for row in csv.DictReader(io.StringIO(r.text)):
        v = _num(row.get("M2SL"))
        if v is not None:
            rows.append({"date": row.get("DATE"), "value": v})
    out = _series_yoy_from_levels(rows)
    if not out:
        raise ValueError("FRED M2SL has no usable observations")
    out.update({"region": "US", "label": "United States M2", "source": "FRED M2SL", "source_url": FRED_US_M2})
    return out


def _fetch_ecb(session: requests.Session) -> dict[str, Any]:
    r = session.get(ECB_M2_CSV, timeout=TIMEOUT, headers={"Accept": "text/csv"})
    r.raise_for_status()
    text = r.text
    rows = list(csv.DictReader(io.StringIO(text)))
    vals = []
    for row in rows:
        date = row.get("TIME_PERIOD") or row.get("TIME_PERIOD_START") or row.get("time_period")
        val = _num(row.get("OBS_VALUE") or row.get("obs_value"))
        mk = _month_key(date)
        if mk and val is not None:
            vals.append((mk, val))
    if not vals:
        raise ValueError("ECB M2 CSV has no usable observations")
    vals = sorted({m: v for m, v in vals}.items())
    latest_m, latest = vals[-1]
    prior3 = vals[-4][1] if len(vals) >= 4 else vals[0][1]
    return {
        "region": "EA", "label": "Euro area M2", "date": latest_m + "-01",
        "yoy_pct": latest, "yoy_3m_ago_pct": prior3,
        "source": f"ECB Data Portal {ECB_M2_KEY}", "source_url": ECB_M2_CSV,
    }


def _parse_boj_csv(text: str) -> dict[str, Any] | None:
    # BOJ CSV exports differ by endpoint. Accept rows containing YYYY/MM plus a numeric observation.
    found = []
    for line in text.splitlines():
        m = re.search(r"(20\d{2})[/-](0?[1-9]|1[0-2])", line)
        if not m:
            continue
        nums = re.findall(r"(?<!\d)(-?\d+(?:\.\d+)?)(?!\d)", line[m.end():])
        if not nums:
            continue
        v = _num(nums[0])
        if v is not None and -20 <= v <= 30:
            found.append((f"{int(m.group(1)):04d}-{int(m.group(2)):02d}", v))
    if not found:
        return None
    vals = sorted({m: v for m, v in found}.items())
    latest_m, latest = vals[-1]
    prior3 = vals[-4][1] if len(vals) >= 4 else vals[0][1]
    return {"date": latest_m + "-01", "yoy_pct": latest, "yoy_3m_ago_pct": prior3}


def _fetch_boj(session: requests.Session) -> dict[str, Any]:
    r = session.get(BOJ_M2_PAGE, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # Prefer an official CSV/export link when the page exposes one.
    links = []
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        low = href.lower()
        if "csv" in low or "download" in low:
            links.append(urljoin(BOJ_M2_PAGE, href))
    for url in links[:3]:
        try:
            rr = session.get(url, timeout=TIMEOUT)
            rr.raise_for_status()
            parsed = _parse_boj_csv(rr.text)
            if parsed:
                parsed.update({"region": "JP", "label": "Japan M2", "source": "Bank of Japan M2 official export", "source_url": url})
                return parsed
        except Exception:
            pass
    # The simple-chart page can contain the series data inline.
    parsed = _parse_boj_csv(soup.get_text("\n"))
    if not parsed:
        raise ValueError("BOJ M2 current page reachable but observation parse unavailable")
    parsed.update({"region": "JP", "label": "Japan M2", "source": "Bank of Japan M2 official page", "source_url": BOJ_M2_PAGE})
    return parsed


def _extract_pbc_article(text: str, url: str) -> dict[str, Any] | None:
    plain = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    # English official reports use: M2 stood at RMBxxx trillion, rising by x percent year on year.
    value = re.search(r"M2\)?\s*(?:stood at|was|reached)\s*(?:RMB|CNY)?\s*([0-9,.]+)\s*trillion", plain, re.I)
    yoy = re.search(r"M2[^.]{0,180}?(?:rising|rose|grew|increasing|increased)\s+by\s+([0-9.]+)\s*percent", plain, re.I)
    if not yoy:
        yoy = re.search(r"broad money[^.]{0,220}?([0-9.]+)\s*percent\s+(?:year on year|yoy)", plain, re.I)
    date_m = re.search(r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2}", plain, re.I)
    if not yoy:
        return None
    date = None
    if date_m:
        try:
            date = datetime.strptime(date_m.group(0), "%B %Y").strftime("%Y-%m-01")
        except Exception:
            pass
    return {"date": date, "yoy_pct": float(yoy.group(1)), "level": _num(value.group(1)) if value else None, "source_url": url}


def _fetch_pbc(session: requests.Session) -> dict[str, Any]:
    r = session.get(PBC_SEARCH, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0 GlobalMacroDataCollector/2.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        text = " ".join(a.stripped_strings)
        href = urljoin(PBC_SEARCH, a.get("href"))
        if "pbc.gov.cn" in href and ("Financial Statistics Report" in text or re.search(r"\bM2\b", text, re.I)):
            candidates.append(href)
    errors = []
    for url in candidates[:5]:
        try:
            rr = session.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0 GlobalMacroDataCollector/2.0"})
            rr.raise_for_status()
            parsed = _extract_pbc_article(rr.text, url)
            if parsed and parsed.get("date"):
                # PBC articles usually expose only current YoY. Use last-good prior reading for acceleration when available.
                parsed.update({"region": "CN", "label": "China M2", "source": "People's Bank of China Financial Statistics Report"})
                return parsed
        except Exception as exc:
            errors.append(str(exc))
    raise ValueError("PBC current M2 report parse unavailable" + (": " + errors[-1][:120] if errors else ""))


def _load_last_good() -> dict[str, Any]:
    try:
        x = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _save_last_good(obj: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _with_prior_from_cache(component: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    if component.get("yoy_3m_ago_pct") is None and old:
        old_yoy = _num(old.get("yoy_pct"))
        if old_yoy is not None:
            component["yoy_3m_ago_pct"] = old_yoy
    if component.get("yoy_3m_ago_pct") is None:
        component["yoy_3m_ago_pct"] = component.get("yoy_pct")
    return component


def build_global_m2(session: requests.Session | None = None) -> dict[str, Any]:
    session = session or requests.Session()
    session.headers.update({"User-Agent": "GlobalMacroDataCollector-GlobalM2/1.0"})
    previous = _load_last_good()
    prev_components = previous.get("components") if isinstance(previous.get("components"), dict) else {}

    fetchers = {"US": _fetch_us, "EA": _fetch_ecb, "JP": _fetch_boj, "CN": _fetch_pbc}
    components: dict[str, Any] = {}
    errors: dict[str, str] = {}
    statuses: dict[str, str] = {}

    for code, fn in fetchers.items():
        try:
            c = _with_prior_from_cache(fn(session), prev_components.get(code))
            age = _age_days(c.get("date"))
            if age is not None and age > MAX_AGE_DAYS[code]:
                raise ValueError(f"stale observation age={age}d")
            c["status"] = "LIVE"
            c["age_days"] = age
            components[code] = c
            statuses[code] = "LIVE"
        except Exception as exc:
            errors[code] = f"{type(exc).__name__}: {str(exc)[:240]}"
            old = prev_components.get(code)
            age = _age_days((old or {}).get("date")) if isinstance(old, dict) else None
            if isinstance(old, dict) and _num(old.get("yoy_pct")) is not None and (age is None or age <= MAX_AGE_DAYS[code] * 2):
                c = dict(old)
                c["status"] = "LAST-GOOD"
                c["age_days"] = age
                components[code] = c
                statuses[code] = "LAST-GOOD"
            else:
                statuses[code] = "UNAVAILABLE"

    usable = {k: v for k, v in components.items() if _num(v.get("yoy_pct")) is not None}
    weight_sum = sum(WEIGHTS[k] for k in usable)
    if weight_sum < 0.50 or len(usable) < 2:
        # Do not fail the entire daily macro workflow because one auxiliary composite is unavailable.
        # Downstream radar sees value=null and proceeds to its own regional/US fallback chain.
        return {
            "schema_version": "1.0",
            "metric": "global_m2",
            "label": "Global M2 broad-money growth composite",
            "generated_at_utc": now_iso(),
            "available": False,
            "value": None,
            "current": None,
            "forecast": None,
            "changePct": None,
            "directionScore": None,
            "coverage_weight": round(weight_sum, 4),
            "coverage_regions": sorted(usable),
            "components": components,
            "statuses": statuses,
            "errors": errors,
            "source": "Global M2 composite unavailable; downstream fallback permitted",
            "methodology": "Requires at least two usable regions and 50% strategic coverage; otherwise the composite abstains instead of fabricating a global value.",
            "is_proxy": False,
        }

    norm_weights = {k: WEIGHTS[k] / weight_sum for k in usable}
    current = sum(norm_weights[k] * float(usable[k]["yoy_pct"]) for k in usable)
    prior3 = sum(norm_weights[k] * float(usable[k].get("yoy_3m_ago_pct", usable[k]["yoy_pct"])) for k in usable)
    acceleration = current - prior3
    # Forecast is a conservative 3m growth-rate continuation; bounded to avoid a single noisy release dominating.
    forecast = current + max(-2.0, min(2.0, acceleration * 0.50))
    # Positive broad-money growth + acceleration are liquidity-positive. Bound to the radar's [-70,+70] convention.
    direction_score = max(-70.0, min(70.0, current * 4.0 + acceleration * 10.0))
    change_pct = ((forecast / current) - 1.0) * 100.0 if abs(current) > 0.25 else (forecast - current) * 10.0

    latest_dates = [c.get("date") for c in usable.values() if c.get("date")]
    out = {
        "schema_version": "1.0",
        "metric": "global_m2",
        "available": True,
        "label": "Global M2 broad-money growth composite",
        "generated_at_utc": now_iso(),
        "observation_date": max(latest_dates) if latest_dates else None,
        "value": round(current, 6),
        "current": round(current, 6),
        "forecast": round(forecast, 6),
        "changePct": round(change_pct, 6),
        "directionScore": round(direction_score, 6),
        "current_yoy_pct": round(current, 6),
        "prior_3m_yoy_pct": round(prior3, 6),
        "acceleration_pp": round(acceleration, 6),
        "coverage_weight": round(weight_sum, 4),
        "coverage_regions": sorted(usable),
        "weights_used": {k: round(v, 6) for k, v in norm_weights.items()},
        "components": components,
        "statuses": statuses,
        "errors": errors,
        "source": "Composite: FRED US M2 + ECB Euro-area M2 + PBC China M2 + BOJ Japan M2; missing regions reweighted",
        "methodology": "Weighted YoY broad-money growth composite; 3-month acceleration informs a conservative forecast and direction score. Strategic weights are fixed and re-normalized when a source is temporarily unavailable.",
        "is_proxy": False,
    }
    _save_last_good(out)
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = build_global_m2()
    (OUT_DIR / "global_m2.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "current": out["current"], "forecast": out["forecast"], "coverage": out["coverage_regions"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
