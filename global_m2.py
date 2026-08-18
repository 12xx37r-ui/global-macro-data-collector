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
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "public" / "data"
CACHE_PATH = OUT_DIR / "cache" / "global_m2_last_good.json"

# Connection/read timeout. Global M2 is a monthly series, so reliability is more
# important than shaving a few seconds off a once-per-workflow collection.
TIMEOUT = (5, 15)
WEIGHTS = {"US": 0.40, "CN": 0.30, "EA": 0.20, "JP": 0.10}
MAX_AGE_DAYS = {"US": 120, "CN": 120, "EA": 120, "JP": 120}

FRED_US_M2 = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=M2SL&cosd=2018-01-01"
FED_H6_CURRENT = "https://www.federalreserve.gov/releases/h6/current/default.htm"
ECB_M2_KEY = "BSI.M.U2.Y.V.M20.X.I.U2.2300.Z01.A"
ECB_M2_CSV = f"https://data-api.ecb.europa.eu/service/data/BSI/{ECB_M2_KEY[4:]}?format=csvdata&startPeriod=2018-01"
BOJ_M2_PAGE = "https://www.stat-search.boj.or.jp/ssi/mtshtml/md02_m_1_en.html"
PBC_REPORTS_EN = "https://www.pbc.gov.cn/en/3688247/3688978/3709137/index.html"
PBC_SEARCH = "https://wzdig.pbc.gov.cn/search/pcRender?pNo={page}&pageId=c177a85bd02b4114bebebd210809f691&q={query}&sr=pubDate%20desc"
FED_ENGINE_US_CONTEXT = "https://raw.githubusercontent.com/12xx37r-ui/fed-futures-collector/main/public/data/us_liquidity_dxy.json"
_US_CONTEXT_MEMO: dict[str, Any] | None = None

# Global-M2 network safety limits. These do not change model weights or calculations.
PROVIDER_MIN_INTERVAL = {"PBC": 0.35, "FRED": 0.10, "FED": 0.10, "ECB": 0.15, "BOJ": 0.20, "GITHUB": 0.15}
_MAX_TOTAL_RETRY_WAIT = 12.0
_REQUEST_MEMO: dict[str, requests.Response] = {}
_PROVIDER_LAST_CALL: dict[str, float] = {}
_API_HEALTH: dict[str, dict[str, Any]] = {}


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


def _provider_for_url(url: str) -> str:
    u = str(url).lower()
    if "pbc.gov.cn" in u:
        return "PBC"
    if "fred.stlouisfed.org" in u:
        return "FRED"
    if "federalreserve.gov" in u:
        return "FED"
    if "ecb.europa.eu" in u:
        return "ECB"
    if "boj.or.jp" in u:
        return "BOJ"
    if "raw.githubusercontent.com" in u or "api.github.com" in u:
        return "GITHUB"
    return "OTHER"


def _health(provider: str) -> dict[str, Any]:
    return _API_HEALTH.setdefault(provider, {
        "provider": provider, "request_attempts": 0, "network_calls": 0,
        "memory_cache_hits": 0, "http_429": 0, "http_5xx": 0,
        "timeouts": 0, "retries": 0, "last_success_utc": None,
        "last_failure_utc": None, "cooldown": False, "final_status": None,
    })


def _pace(provider: str) -> None:
    gap = float(PROVIDER_MIN_INTERVAL.get(provider, 0.10))
    last = _PROVIDER_LAST_CALL.get(provider)
    if last is not None:
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _PROVIDER_LAST_CALL[provider] = time.monotonic()


def _retry_after_seconds(response: requests.Response | None) -> float | None:
    if response is None:
        return None
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), _MAX_TOTAL_RETRY_WAIT))
    except Exception:
        return None


def _get_retry(
    session: requests.Session,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    attempts: int = 2,
    timeout: tuple[int, int] = TIMEOUT,
    memo: bool = True,
) -> requests.Response:
    provider = _provider_for_url(url)
    h = _health(provider)
    memo_key = url + "|" + json.dumps(headers or {}, sort_keys=True, ensure_ascii=False)
    h["request_attempts"] += 1
    if memo and memo_key in _REQUEST_MEMO:
        h["memory_cache_hits"] += 1
        return _REQUEST_MEMO[memo_key]

    last: Exception | None = None
    waited = 0.0
    for i in range(max(1, attempts)):
        response = None
        try:
            _pace(provider)
            h["network_calls"] += 1
            response = session.get(url, timeout=timeout, headers=headers or {}, allow_redirects=True)
            if response.status_code == 429:
                h["http_429"] += 1
                raise requests.HTTPError("HTTP 429", response=response)
            if 500 <= response.status_code < 600:
                h["http_5xx"] += 1
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            if not response.content:
                raise ValueError("empty response")
            h["last_success_utc"] = now_iso()
            h["cooldown"] = False
            if memo:
                _REQUEST_MEMO[memo_key] = response
            return response
        except requests.Timeout as exc:
            h["timeouts"] += 1
            last = exc
        except Exception as exc:
            last = exc
        h["last_failure_utc"] = now_iso()
        if i + 1 >= attempts:
            break
        h["retries"] += 1
        retry_after = _retry_after_seconds(getattr(last, "response", None))
        sleep_for = retry_after if retry_after is not None else min(0.7 * (2 ** i) + 0.15 * (i + 1), 3.0)
        if waited + sleep_for > _MAX_TOTAL_RETRY_WAIT:
            break
        h["cooldown"] = True
        time.sleep(sleep_for)
        waited += sleep_for
    raise last or RuntimeError("request failed")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        x = json.loads(path.read_text(encoding="utf-8"))
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _get_us_engine_context(session: requests.Session) -> dict[str, Any]:
    global _US_CONTEXT_MEMO
    if isinstance(_US_CONTEXT_MEMO, dict):
        return _US_CONTEXT_MEMO
    # Normal workflow: treasury_card8 already fetched the Fed engine latest.json.
    # Reuse that payload locally instead of making another GitHub request.
    card8 = _read_json(OUT_DIR / "us_treasury_card8.json")
    ctx = card8.get("upstream_us_macro_context") if isinstance(card8, dict) else None
    if isinstance(ctx, dict) and ctx.get("available"):
        _US_CONTEXT_MEMO = ctx
        return ctx
    # Standalone/global-M2 fallback: one small dedicated upstream JSON request.
    try:
        r = _get_retry(session, FED_ENGINE_US_CONTEXT, attempts=1, timeout=(4, 8))
        x = r.json()
        if isinstance(x, dict) and x.get("available"):
            _US_CONTEXT_MEMO = x
            return x
    except Exception:
        pass
    _US_CONTEXT_MEMO = {}
    return {}


def _fetch_us_engine(session: requests.Session) -> dict[str, Any]:
    ctx = _get_us_engine_context(session)
    m2 = ctx.get("m2") if isinstance(ctx, dict) else None
    if not isinstance(m2, dict) or not m2.get("available"):
        raise ValueError("Fed engine US M2 context unavailable")
    yoy = _num(m2.get("current_yoy_pct"))
    p3 = _num(m2.get("prior_3m_yoy_pct"))
    if yoy is None or p3 is None:
        raise ValueError("Fed engine US M2 required values missing")
    return {
        "region": "US", "label": "United States M2",
        "date": m2.get("observation_date"), "yoy_pct": yoy, "yoy_3m_ago_pct": p3,
        "level": m2.get("level_billions_usd"), "forecast_yoy_3m_pct": _num(m2.get("forecast_3m_yoy_pct")),
        "forecast_confidence": m2.get("confidence"), "status": m2.get("status") or "LIVE",
        "source": "Fed policy engine · " + str(m2.get("source") or "US M2"),
        "source_url": m2.get("source_url") or FED_ENGINE_US_CONTEXT,
        "upstream_generated_at_utc": ctx.get("generated_at_utc"),
    }


def _load_forward_context(session: requests.Session) -> dict[str, Any]:
    # No extra network call here: reuse context already obtained by _fetch_us_engine
    # or the local Card 8 payload produced earlier in the same collector run.
    card8 = _read_json(OUT_DIR / "us_treasury_card8.json")
    ctx = _US_CONTEXT_MEMO if isinstance(_US_CONTEXT_MEMO, dict) else {}
    if not ctx and isinstance(card8.get("upstream_us_macro_context"), dict):
        ctx = card8.get("upstream_us_macro_context") or {}
    return {"us_engine": ctx, "card8": card8}


def _liquidity_grade(score: float) -> str:
    if score >= 60: return "매우 유리"
    if score >= 30: return "유리"
    if score >= 10: return "약유리"
    if score > -10: return "중립"
    if score > -30: return "약불리"
    if score > -60: return "불리"
    return "매우 불리"


def _forward_liquidity_outlook(current: float, forecast: float, context: dict[str, Any]) -> dict[str, Any]:
    inputs: list[dict[str, Any]] = []
    m2_change = forecast - current
    m2_signal = max(-1.0, min(1.0, m2_change / 0.50))
    inputs.append({"name":"Global M2 3개월 예상 변화","current":round(current,4),"forecast":round(forecast,4),"signal":round(m2_signal,4),"weight":0.60,"meaning":"광의통화 증가율 상승은 유동성에 우호적"})
    usctx = context.get("us_engine") or {}
    dxy = usctx.get("dxy") or {}
    dxy_change = _num(dxy.get("forecast_change_3m_pct"))
    if dxy.get("available") and dxy_change is not None:
        sig = -max(-1.0, min(1.0, dxy_change / 3.0))
        inputs.append({"name":"DXY 3개월 예상","current":dxy.get("current"),"forecast":dxy.get("forecast_3m"),"signal":round(sig,4),"weight":0.25,"meaning":"달러 약세는 글로벌 달러유동성에 상대적으로 우호적","source":dxy.get("source")})
    card8 = context.get("card8") or {}
    real_cur = _num((((card8.get("current") or {}).get("DFII10") or {}).get("value")))
    real3 = _num((((((card8.get("forecasts") or {}).get("3m") or {}).get("targets") or {}).get("DFII10") or {}).get("forecast")))
    gate = ((((((card8.get("forecasts") or {}).get("3m") or {}).get("targets") or {}).get("DFII10") or {}).get("quality_gate") or {}))
    if real_cur is not None and real3 is not None:
        sig = -max(-1.0, min(1.0, (real3 - real_cur) / 0.35))
        weight = 0.15 if gate.get("passed") else 0.075
        inputs.append({"name":"미국 10년 실질금리 3개월 예상","current":real_cur,"forecast":real3,"signal":round(sig,4),"weight":weight,"meaning":"실질금리 하락은 위험자산 유동성 환경에 우호적","validation":"통과" if gate.get("passed") else "미통과·절반가중"})
    sw = sum(float(x["weight"]) for x in inputs) or 1.0
    score = sum(float(x["signal"]) * float(x["weight"]) for x in inputs) / sw * 100.0
    return {
        "score": round(score, 2), "grade": _liquidity_grade(score),
        "direction": "improving" if score >= 10 else "deteriorating" if score <= -10 else "neutral",
        "inputs": inputs,
        "validation_status": "UNVALIDATED_COMPOSITE",
        "note": "M2 전망 자체와 분리된 보조 유동성 환경지표입니다. DXY·실질금리 보조신호는 Global M2 수치를 직접 바꾸지 않습니다.",
    }

# ---------- United States ----------
# Primary: FRED CSV. Secondary: Federal Reserve Board H.6 release HTML.
# The H.6 fallback is deliberately independent of fred.stlouisfed.org, so a
# transient FRED timeout does not make the 40%-weight US component disappear.

def _fetch_us_fred(session: requests.Session) -> dict[str, Any]:
    r = _get_retry(session, FRED_US_M2, attempts=1, timeout=(4, 10))
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
        attempts=1,
        timeout=(4, 10),
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
    for fn in (_fetch_us_engine, _fetch_us_fred, _fetch_us_fed_h6):
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
    """Fetch Japan M2 from BOJ's current main time-series table.

    The old per-series HTML route became brittle after BOJ's 2026 time-series/API
    refresh.  The main statistics table exposes the official M2 YoY series in a
    stable first data column (series code MD02'MAM1YAM2M2MO), so parse that first
    and keep the legacy text parser only as a fallback.
    """
    r = _get_retry(session, BOJ_M2_PAGE, attempts=3)
    soup = BeautifulSoup(r.text, "html.parser")
    yoy: list[tuple[str, float]] = []
    levels: list[dict[str, Any]] = []

    for tr in soup.find_all("tr"):
        cells = [" ".join(x.stripped_strings).strip() for x in tr.find_all(["th", "td"])]
        if len(cells) < 2:
            continue
        mk = _month_key(cells[0])
        if not mk:
            continue
        y = _num(cells[1])
        if y is not None and -30 <= y <= 30:
            yoy.append((mk, y))
        # In the BOJ main table, column 9 is M2 average amount outstanding.
        if len(cells) >= 10:
            lv = _num(cells[9])
            if lv is not None and lv > 100:
                levels.append({"date": mk + "-01", "value": lv})

    vals = sorted({m: v for m, v in yoy}.items())
    if len(vals) >= 4:
        latest_m, latest_yoy = vals[-1]
        prior3 = vals[-4][1]
        out = {"date": latest_m + "-01", "yoy_pct": latest_yoy, "yoy_3m_ago_pct": prior3}
        if levels:
            bym = {str(x["date"])[:7]: x["value"] for x in levels}
            out["level"] = bym.get(latest_m)
        out.update({
            "region": "JP", "label": "Japan M2",
            "source": "Bank of Japan Main Time-Series Statistics · MD02'MAM1YAM2M2MO",
            "source_url": BOJ_M2_PAGE,
        })
        return out

    parsed = _parse_boj_text(soup.get_text("\n"))
    if not parsed:
        raise ValueError("BOJ M2 official main time-series page has no usable observations")
    parsed.update({
        "region": "JP", "label": "Japan M2",
        "source": "Bank of Japan Money Stock official main time-series page",
        "source_url": BOJ_M2_PAGE,
    })
    return parsed


# ---------- China ----------

_MONTH_CN = {1:"01",2:"02",3:"03",4:"04",5:"05",6:"06",7:"07",8:"08",9:"09",10:"10",11:"11",12:"12"}

def _extract_pbc_article(text: str, url: str) -> dict[str, Any] | None:
    plain = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    plain = re.sub(r"\s+", " ", plain)

    # English official wording.
    yoy_m = re.search(r"M2.{0,260}?(?:rising|rose|grew|increasing|increased)\s+(?:by\s+)?([0-9.]+)\s*percent", plain, re.I)
    if not yoy_m:
        yoy_m = re.search(r"broad money.{0,260}?([0-9.]+)\s*percent\s+(?:year on year|yoy)", plain, re.I)

    # Chinese official wording, e.g. "广义货币(M2)余额...万亿元，同比增长8.8%".
    if not yoy_m:
        yoy_m = re.search(r"(?:广义货币(?:供应量)?|M2)[^。；;]{0,260}?(?:同比增长|同比增速(?:为)?|增长)\s*([0-9.]+)\s*%", plain, re.I)

    if not yoy_m:
        return None

    level = None
    level_m = re.search(r"M2.{0,180}?(?:RMB|CNY)?\s*([0-9,.]+)\s*trillion", plain, re.I)
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


def _pbc_report_month(label: str) -> str | None:
    text = re.sub(r"\s+", " ", str(label or "")).strip()
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})", text, re.I)
    if m:
        return datetime.strptime(m.group(0).title(), "%B %Y").strftime("%Y-%m")
    y = re.search(r"(20\d{2})", text)
    if not y:
        return None
    year = int(y.group(1))
    if re.search(r"\bH1\b|first half", text, re.I):
        return f"{year:04d}-06"
    if re.search(r"\bQ1-Q3\b|first three quarters", text, re.I):
        return f"{year:04d}-09"
    if re.search(r"\bQ1\b|first quarter", text, re.I):
        return f"{year:04d}-03"
    if re.search(r"\b202\d\b", text) and "Report (" in text:
        return f"{year:04d}-12"
    return None


def _pbc_listing_candidates(html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        label = " ".join(a.stripped_strings).strip()
        if not re.search(r"Financial Statistics Report", label, re.I):
            continue
        month = _pbc_report_month(label)
        if not month:
            continue
        href = urljoin(base_url, str(a.get("href") or ""))
        if "pbc.gov.cn" not in href.lower() or href in seen:
            continue
        seen.add(href)
        out.append({"month": month, "label": label, "url": href})
    return sorted(out, key=lambda x: x["month"], reverse=True)


def _month_number(month: str) -> int:
    y, m = map(int, month.split("-"))
    return y * 12 + m


def _fetch_pbc(session: requests.Session) -> dict[str, Any]:
    """Fetch China M2 with bounded official-PBC requests.

    Normal path: one official Financial Statistics Reports index request plus the
    latest report and the report at/just before the three-month comparison point.
    The former search-spider path could fan out to dozens of article requests; it
    is now a one-page fallback only and is used solely if the official index fails.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GlobalMacroDataCollector/3.3",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.7",
    }
    candidates: list[dict[str, str]] = []
    listing_error = None
    try:
        r = _get_retry(session, PBC_REPORTS_EN, headers=headers, attempts=2, timeout=(5, 12))
        candidates = _pbc_listing_candidates(r.text, PBC_REPORTS_EN)
    except Exception as exc:
        listing_error = f"{type(exc).__name__}: {str(exc)[:160]}"

    # Existing PBC search endpoint retained only as a tightly bounded fallback.
    if not candidates:
        try:
            u = PBC_SEARCH.format(page=1, query=quote("Financial Statistics Report"))
            r = _get_retry(session, u, headers=headers, attempts=1, timeout=(5, 10))
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                label = " ".join(a.stripped_strings).strip()
                month = _pbc_report_month(label)
                href = urljoin(u, str(a.get("href") or ""))
                if month and re.search(r"Financial Statistics Report", label, re.I) and "pbc.gov.cn" in href.lower():
                    candidates.append({"month": month, "label": label, "url": href})
            candidates = sorted({x["url"]: x for x in candidates}.values(), key=lambda x: x["month"], reverse=True)
        except Exception:
            pass

    if not candidates:
        raise ValueError("PBC Financial Statistics Reports index unavailable" + (f" · {listing_error}" if listing_error else ""))

    latest_month = candidates[0]["month"]
    target_num = _month_number(latest_month) - 3
    selected: list[dict[str, str]] = [candidates[0]]
    prior = next((c for c in candidates[1:] if _month_number(c["month"]) <= target_num), None)
    if prior:
        selected.append(prior)

    # At most four additional reports may be tried if a selected report changes HTML shape.
    fallback_candidates = [c for c in candidates if c not in selected][:4]
    observations: list[dict[str, Any]] = []
    errors: list[str] = []
    for c in selected + fallback_candidates:
        if len(observations) >= 2:
            obs_months = sorted(str(o["date"])[:7] for o in observations if o.get("date"))
            if obs_months and _month_number(obs_months[-1]) - _month_number(obs_months[0]) >= 3:
                break
        try:
            rr = _get_retry(session, c["url"], headers=headers, attempts=2, timeout=(5, 12))
            parsed = _extract_pbc_article(rr.text, c["url"])
            if parsed and parsed.get("date") and _num(parsed.get("yoy_pct")) is not None:
                observations.append(parsed)
        except Exception as exc:
            errors.append(f"{c['month']}: {type(exc).__name__}: {str(exc)[:120]}")

    if not observations:
        raise ValueError("PBC recent Financial Statistics Report parse unavailable" + (" · " + " | ".join(errors[-2:]) if errors else ""))

    dedup = {str(o["date"])[:7]: o for o in observations}
    rows = [dedup[k] for k in sorted(dedup)]
    latest = rows[-1]
    latest_m = str(latest["date"])[:7]
    target_num = _month_number(latest_m) - 3
    prior_candidates = [o for o in rows[:-1] if _month_number(str(o["date"])[:7]) <= target_num]
    if not prior_candidates:
        raise ValueError("PBC latest M2 obtained but genuine 3-month comparison observation unavailable")
    prior = prior_candidates[-1]

    return {
        "region": "CN", "label": "China M2", "date": latest.get("date"),
        "yoy_pct": float(latest["yoy_pct"]),
        "yoy_3m_ago_pct": float(prior["yoy_pct"]),
        "level": latest.get("level"),
        "source": "People's Bank of China Financial Statistics Reports",
        "source_url": latest.get("source_url"),
        "history_points": len(rows),
    }


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
    started = time.monotonic()
    _REQUEST_MEMO.clear()
    _PROVIDER_LAST_CALL.clear()
    _API_HEALTH.clear()
    session = session or requests.Session()
    session.headers.update({"User-Agent": "GlobalMacroDataCollector-GlobalM2/3.3"})
    previous = _load_last_good()
    prev_components = previous.get("components") if isinstance(previous.get("components"), dict) else {}

    fetchers = {"US": _fetch_us, "EA": _fetch_ecb, "JP": _fetch_boj, "CN": _fetch_pbc}
    components: dict[str, Any] = {}
    errors: dict[str, str] = {}
    statuses: dict[str, str] = {}

    for code, fn in fetchers.items():
        region_started = time.monotonic()
        try:
            c = _with_prior_from_cache(fn(session), prev_components.get(code))
            age = _age_days(c.get("date"))
            if age is not None and age > MAX_AGE_DAYS[code]:
                raise ValueError(f"stale observation age={age}d")
            c["status"] = "LIVE"
            c["age_days"] = age
            c["fetch_ms"] = int((time.monotonic() - region_started) * 1000)
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
                c["fetch_ms"] = int((time.monotonic() - region_started) * 1000)
                components[code] = c
                statuses[code] = "LAST-GOOD"
            else:
                statuses[code] = "UNAVAILABLE"

    for provider, h in _API_HEALTH.items():
        h["final_status"] = "LIVE" if h.get("last_success_utc") else "UNAVAILABLE"
    runtime_ms = int((time.monotonic() - started) * 1000)

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
            "runtime_ms": runtime_ms,
            "api_health": _API_HEALTH,
            "source": "Global M2 composite unavailable; downstream fallback permitted",
            "methodology": "Requires at least two usable regions and 50% strategic coverage; otherwise the composite abstains instead of fabricating a global value.",
            "is_proxy": False,
        }

    norm_weights = {k: WEIGHTS[k] / weight_sum for k in usable}
    current = sum(norm_weights[k] * float(usable[k]["yoy_pct"]) for k in usable)
    prior3 = sum(norm_weights[k] * float(usable[k].get("yoy_3m_ago_pct", usable[k]["yoy_pct"])) for k in usable)
    acceleration = current - prior3
    legacy_forecast = current + max(-2.0, min(2.0, acceleration * 0.50))
    component_forecasts: dict[str, Any] = {}
    has_direct_us = False
    for k, c in usable.items():
        cur = float(c["yoy_pct"]); p3 = float(c.get("yoy_3m_ago_pct", cur))
        if k == "US" and _num(c.get("forecast_yoy_3m_pct")) is not None:
            fc = float(c["forecast_yoy_3m_pct"]); method = "US Fed-engine history model"; has_direct_us = True
        else:
            fc = cur + max(-2.0, min(2.0, (cur - p3) * 0.50)); method = "half-persistence of 3m YoY acceleration"
        component_forecasts[k] = {"current_yoy_pct": round(cur,6), "forecast_3m_yoy_pct": round(fc,6), "method": method}
    forecast = sum(norm_weights[k] * float(component_forecasts[k]["forecast_3m_yoy_pct"]) for k in usable) if has_direct_us else legacy_forecast
    direction_score = max(-70.0, min(70.0, current * 4.0 + (forecast-current) * 20.0))
    change_pct = ((forecast / current) - 1.0) * 100.0 if abs(current) > 0.25 else (forecast - current) * 10.0
    forward_context = _load_forward_context(session)
    forward_liquidity = _forward_liquidity_outlook(current, forecast, forward_context)

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
        "legacy_forecast": round(legacy_forecast, 6),
        "forecast_model": "region-specific forecast with direct US Fed-engine M2 model" if has_direct_us else "legacy composite acceleration fallback",
        "forecast_components": component_forecasts,
        "forward_liquidity_outlook": forward_liquidity,
        "us_dxy": ((forward_context.get("us_engine") or {}).get("dxy") or {}),
        "coverage_weight": round(weight_sum, 4),
        "coverage_regions": sorted(usable),
        "full_coverage": weight_sum >= 0.999 and len(usable) == 4,
        "coverage_quality": "FULL" if weight_sum >= 0.999 and len(usable) == 4 else ("PARTIAL" if weight_sum >= 0.75 else "DEGRADED"),
        "missing_regions": sorted(set(WEIGHTS) - set(usable)),
        "weights_used": {k: round(v, 6) for k, v in norm_weights.items()},
        "components": components,
        "statuses": statuses,
        "errors": errors,
        "runtime_ms": runtime_ms,
        "api_health": _API_HEALTH,
        "source": "Composite: Federal Reserve/FRED US M2 + ECB Euro-area M2 + PBC China M2 + BOJ Japan M2",
        "methodology": "Weighted YoY broad-money growth composite. Current weights remain US/CN/EA/JP 40/30/20/10. The 3-month forecast uses the Fed-engine history-based US M2 forecast when available and conservative region acceleration for CN/EA/JP; the prior composite-acceleration forecast is retained as legacy_forecast for audit. DXY and real-yield signals are reported separately in forward_liquidity_outlook and do not alter the M2 level forecast.",
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
