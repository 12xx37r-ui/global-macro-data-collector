from __future__ import annotations

import csv
import io
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "public" / "data"
CACHE_PATH = OUT_DIR / "cache" / "global_m2_last_good.json"

# Connection/read timeout. Global M2 is a monthly series, so reliability is more
# important than shaving a few seconds off a once-per-workflow collection.
TIMEOUT = (8, 45)
WEIGHTS = {"US": 0.40, "CN": 0.30, "EA": 0.20, "JP": 0.10}
MAX_AGE_DAYS = {"US": 120, "CN": 120, "EA": 120, "JP": 120}

FRED_US_M2 = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=M2SL&cosd=2018-01-01"
FED_H6_CURRENT = "https://www.federalreserve.gov/releases/h6/current/default.htm"
ECB_M2_KEY = "BSI.M.U2.Y.V.M20.X.I.U2.2300.Z01.A"
ECB_M2_CSV = f"https://data-api.ecb.europa.eu/service/data/BSI/{ECB_M2_KEY[4:]}?format=csvdata&startPeriod=2018-01"
BOJ_M2_PAGE = "https://www.stat-search.boj.or.jp/ssi/mtshtml/mam1yam2m2mo.html"
PBC_SEARCH = "https://wzdig.pbc.gov.cn/search/pcRender?pNo=1&pageId=c177a85bd02b4114bebebd210809f691&q=M2&sr=pubDate%20desc"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v: Any) -> float | None:
    try:
        x = float(str(v).replace(",", "").replace("\xa0", "").strip())
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _month_key(s: str) -> str | None:
    s = str(s or "").strip()
    m = re.search(r"(20\d{2})[-/年.\s](0?[1-9]|1[0-2])", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    for fmt in ("%b. %Y", "%b %Y", "%B %Y"):
        try:
            d = datetime.strptime(s.strip(), fmt)
            return f"{d.year:04d}-{d.month:02d}"
        except Exception:
            pass
    return None


def _age_days(dateish: str | None) -> int | None:
    if not dateish:
        return None
    try:
        d = datetime.fromisoformat(str(dateish)[:10]).replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - d).total_seconds() // 86400))
    except Exception:
        return None


def _series_yoy_from_levels(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_month: dict[str, float] = {}
    for r in rows:
        mk = _month_key(r.get("date"))
        v = _num(r.get("value"))
        if mk and v is not None and v > 0:
            by_month[mk] = v
    yoy: list[tuple[str, float]] = []
    for m in sorted(by_month):
        y, mo = map(int, m.split("-"))
        prev = f"{y-1:04d}-{mo:02d}"
        if prev in by_month and by_month[prev] > 0:
            yoy.append((m, (by_month[m] / by_month[prev] - 1.0) * 100.0))
    if not yoy:
        return None
    latest_m, latest_yoy = yoy[-1]
    prior3 = yoy[-4][1] if len(yoy) >= 4 else yoy[0][1]
    return {"date": latest_m + "-01", "yoy_pct": latest_yoy, "yoy_3m_ago_pct": prior3, "level": by_month[latest_m]}


def _get_retry(
    session: requests.Session,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    attempts: int = 3,
    timeout: tuple[int, int] = TIMEOUT,
) -> requests.Response:
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            r = session.get(url, timeout=timeout, headers=headers or {}, allow_redirects=True)
            r.raise_for_status()
            if r.content:
                return r
            raise ValueError("empty response")
        except Exception as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(0.8 * (i + 1))
    raise last or RuntimeError("request failed")


# ---------- United States ----------
# Primary: FRED CSV. Secondary: Federal Reserve Board H.6 release HTML.
# The H.6 fallback is deliberately independent of fred.stlouisfed.org, so a
# transient FRED timeout does not make the 40%-weight US component disappear.

def _fetch_us_fred(session: requests.Session) -> dict[str, Any]:
    r = _get_retry(session, FRED_US_M2, attempts=3)
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


def _fetch_us_fed_h6(session: requests.Session) -> dict[str, Any]:
    r = _get_retry(
        session,
        FED_H6_CURRENT,
        headers={"User-Agent": "Mozilla/5.0 GlobalMacroDataCollector/3.1"},
        attempts=2,
    )
    soup = BeautifulSoup(r.text, "html.parser")
    rows: list[dict[str, Any]] = []

    # Table 1 has Date, seasonally adjusted M1, M2, ... . We find a table that
    # explicitly mentions Money Stock / M2 and then take the second numeric cell
    # after each monthly date. This survives minor header/layout changes.
    for table in soup.find_all("table"):
        head = " ".join(table.stripped_strings)
        if "M2" not in head:
            continue
        local: list[dict[str, Any]] = []
        for tr in table.find_all("tr"):
            cells = [" ".join(x.stripped_strings).strip() for x in tr.find_all(["th", "td"])]
            if len(cells) < 3:
                continue
            mk = _month_key(cells[0])
            if not mk:
                continue
            nums = [_num(x) for x in cells[1:]]
            nums = [x for x in nums if x is not None]
            if len(nums) >= 2 and nums[1] > 100:
                local.append({"date": mk + "-01", "value": nums[1]})
        if len(local) >= 13:
            rows = local
            break

    # Fallback for Fed pages where responsive HTML flattens the table.
    if len(rows) < 13:
        plain = soup.get_text("\n")
        lines = [re.sub(r"\s+", " ", x).strip() for x in plain.splitlines() if x.strip()]
        for i, line in enumerate(lines):
            mk = _month_key(line)
            if not mk:
                continue
            window = " ".join(lines[i:i + 8])
            nums = [_num(x) for x in re.findall(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b", window)]
            nums = [x for x in nums if x is not None and x > 100]
            if len(nums) >= 2:
                rows.append({"date": mk + "-01", "value": nums[1]})

    out = _series_yoy_from_levels(rows)
    if not out:
        raise ValueError("Federal Reserve H.6 page has no usable M2 monthly series")
    out.update({
        "region": "US",
        "label": "United States M2",
        "source": "Federal Reserve Board H.6 Money Stock Measures",
        "source_url": FED_H6_CURRENT,
    })
    return out


def _fetch_us(session: requests.Session) -> dict[str, Any]:
    errors = []
    for fn in (_fetch_us_fred, _fetch_us_fed_h6):
        try:
            return fn(session)
        except Exception as exc:
            errors.append(f"{fn.__name__}: {type(exc).__name__}: {str(exc)[:160]}")
    raise RuntimeError("US M2 all official routes failed · " + " | ".join(errors))


# ---------- Euro area ----------

def _fetch_ecb(session: requests.Session) -> dict[str, Any]:
    r = _get_retry(session, ECB_M2_CSV, headers={"Accept": "text/csv"}, attempts=3)
    rows = list(csv.DictReader(io.StringIO(r.text)))
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


# ---------- Japan ----------

def _parse_boj_text(text: str) -> dict[str, Any] | None:
    # Accept either an explicit YoY column or a monthly level column.
    yoy: list[tuple[str, float]] = []
    levels: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        mk = _month_key(line)
        if not mk:
            continue
        # Strip the date so its year/month digits are not mistaken for observations.
        tail = re.sub(r"20\d{2}[-/年.\s](?:0?[1-9]|1[0-2])(?:月)?", " ", line, count=1)
        nums = [_num(x) for x in re.findall(r"-?\d[\d,]*(?:\.\d+)?", tail)]
        nums = [x for x in nums if x is not None]
        small = [x for x in nums if -30 <= x <= 30]
        large = [x for x in nums if x > 100]
        if small:
            yoy.append((mk, small[0]))
        elif large:
            levels.append({"date": mk + "-01", "value": large[0]})

    if len(yoy) >= 4:
        vals = sorted({m: v for m, v in yoy}.items())
        m, v = vals[-1]
        p3 = vals[-4][1]
        return {"date": m + "-01", "yoy_pct": v, "yoy_3m_ago_pct": p3}
    if len(levels) >= 13:
        return _series_yoy_from_levels(levels)
    return None


def _fetch_boj(session: requests.Session) -> dict[str, Any]:
    r = _get_retry(session, BOJ_M2_PAGE, attempts=3)
    soup = BeautifulSoup(r.text, "html.parser")

    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        label = " ".join(a.stripped_strings).lower()
        low = href.lower()
        if any(k in low for k in ("csv", "download", "famecgi")) or any(k in label for k in ("csv", "download")):
            u = urljoin(BOJ_M2_PAGE, href)
            if u not in links:
                links.append(u)

    for url in links[:8]:
        try:
            rr = _get_retry(session, url, attempts=2)
            parsed = _parse_boj_text(rr.text)
            if parsed:
                parsed.update({
                    "region": "JP", "label": "Japan M2",
                    "source": "Bank of Japan Money Stock official export",
                    "source_url": url,
                })
                return parsed
        except Exception:
            pass

    # Try visible HTML/table text as a last official route.
    parsed = _parse_boj_text(soup.get_text("\n"))
    if not parsed:
        raise ValueError("BOJ M2 official page reachable but observation parse unavailable")
    parsed.update({
        "region": "JP", "label": "Japan M2",
        "source": "Bank of Japan Money Stock official page",
        "source_url": BOJ_M2_PAGE,
    })
    return parsed


# ---------- China ----------

_MONTH_CN = {1:"01",2:"02",3:"03",4:"04",5:"05",6:"06",7:"07",8:"08",9:"09",10:"10",11:"11",12:"12"}

def _extract_pbc_article(text: str, url: str) -> dict[str, Any] | None:
    plain = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    plain = re.sub(r"\s+", " ", plain)

    # English official wording.
    yoy_m = re.search(r"M2[^.]{0,220}?(?:rising|rose|grew|increasing|increased)\s+(?:by\s+)?([0-9.]+)\s*percent", plain, re.I)
    if not yoy_m:
        yoy_m = re.search(r"broad money[^.]{0,220}?([0-9.]+)\s*percent\s+(?:year on year|yoy)", plain, re.I)

    # Chinese official wording, e.g. "广义货币(M2)余额...万亿元，同比增长8.8%".
    if not yoy_m:
        yoy_m = re.search(r"(?:广义货币(?:供应量)?|M2)[^。；;]{0,260}?(?:同比增长|同比增速(?:为)?|增长)\s*([0-9.]+)\s*%", plain, re.I)

    if not yoy_m:
        return None

    level = None
    level_m = re.search(r"M2[^.]{0,160}?(?:RMB|CNY)?\s*([0-9,.]+)\s*trillion", plain, re.I)
    if not level_m:
        level_m = re.search(r"(?:广义货币(?:供应量)?|M2)[^。；;]{0,160}?余额(?:为)?\s*([0-9,.]+)\s*万亿元", plain, re.I)
    if level_m:
        level = _num(level_m.group(1))

    date = None
    eng_date = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})", plain, re.I)
    if eng_date:
        try:
            date = datetime.strptime(eng_date.group(0).title(), "%B %Y").strftime("%Y-%m-01")
        except Exception:
            pass
    if not date:
        cn_date = re.search(r"(20\d{2})年\s*(1[0-2]|0?[1-9])月", plain)
        if cn_date:
            date = f"{int(cn_date.group(1)):04d}-{int(cn_date.group(2)):02d}-01"

    return {
        "date": date,
        "yoy_pct": float(yoy_m.group(1)),
        "level": level,
        "source_url": url,
    }


def _fetch_pbc(session: requests.Session) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GlobalMacroDataCollector/3.1",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }
    r = _get_retry(session, PBC_SEARCH, headers=headers, attempts=3)
    soup = BeautifulSoup(r.text, "html.parser")
    candidates: list[str] = []

    for a in soup.find_all("a", href=True):
        label = " ".join(a.stripped_strings)
        href = urljoin(PBC_SEARCH, a.get("href"))
        if "pbc.gov.cn" in href and (
            re.search(r"\bM2\b", label, re.I)
            or "金融统计数据报告" in label
            or "金融统计数据" in label
            or "Financial Statistics Report" in label
        ):
            if href not in candidates:
                candidates.append(href)

    # Some PBC search responses contain article URLs in JSON/JS rather than anchors.
    for href in re.findall(r'https?://[^"\']*pbc\.gov\.cn[^"\'\s<>]+', r.text, re.I):
        href = href.replace("\\/", "/")
        if href not in candidates:
            candidates.append(href)

    errors = []
    for url in candidates[:12]:
        try:
            rr = _get_retry(session, url, headers=headers, attempts=2)
            parsed = _extract_pbc_article(rr.text, url)
            if parsed and parsed.get("date"):
                parsed.update({
                    "region": "CN", "label": "China M2",
                    "source": "People's Bank of China Financial Statistics Report",
                })
                return parsed
        except Exception as exc:
            errors.append(str(exc)[:160])

    raise ValueError("PBC current M2 report parse unavailable" + (": " + errors[-1] if errors else ""))


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
    session.headers.update({"User-Agent": "GlobalMacroDataCollector-GlobalM2/3.1"})
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
            errors[code] = f"{type(exc).__name__}: {str(exc)[:400]}"
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
        return {
            "schema_version": "1.1",
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
    forecast = current + max(-2.0, min(2.0, acceleration * 0.50))
    direction_score = max(-70.0, min(70.0, current * 4.0 + acceleration * 10.0))
    change_pct = ((forecast / current) - 1.0) * 100.0 if abs(current) > 0.25 else (forecast - current) * 10.0

    latest_dates = [c.get("date") for c in usable.values() if c.get("date")]
    out = {
        "schema_version": "1.1",
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
        "source": "Composite: Federal Reserve/FRED US M2 + ECB Euro-area M2 + PBC China M2 + BOJ Japan M2; missing regions reweighted",
        "methodology": "Weighted YoY broad-money growth composite; 3-month acceleration informs a conservative forecast and direction score. Strategic weights are fixed and re-normalized when a source is temporarily unavailable.",
        "is_proxy": False,
    }
    _save_last_good(out)
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = build_global_m2()
    (OUT_DIR / "global_m2.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": "ok" if out.get("available") else "fallback-permitted",
        "current": out.get("current"),
        "forecast": out.get("forecast"),
        "coverage": out.get("coverage_regions"),
        "statuses": out.get("statuses"),
        "errors": out.get("errors"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
