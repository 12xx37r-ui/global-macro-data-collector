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
CN_HISTORY_CACHE_PATH = OUT_DIR / "cache" / "pbc_m2_yoy_history.json"

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
PBC_REPORTS_CN = "https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html"
PBC_REPORTS_CN_PAGE = "https://www.pbc.gov.cn/diaochatongjisi/116219/116225/11871-{page}.html"
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
    m = re.search(r"(20\d{2})[-/年.\s](1[0-2]|0?[1-9])(?!\d)", s)
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


def _history_from_yoy_pairs(vals: list[tuple[str, float]]) -> list[dict[str, Any]]:
    return [{"date": m + "-01", "value": float(v)} for m, v in sorted({m: float(v) for m, v in vals}.items())]


def _recent_contiguous_month_history(history: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Return the most recent contiguous monthly block and the number of gaps dropped."""
    ded: dict[str, dict[str, Any]] = {}
    for row in history:
        mk = _month_key(row.get("date"))
        v = _num(row.get("value"))
        if mk and v is not None:
            ded[mk] = {"date": mk + "-01", "value": float(v)}
    keys = sorted(ded)
    if not keys:
        return [], 0
    block = [ded[keys[-1]]]
    expected = _month_number(keys[-1]) - 1
    for mk in reversed(keys[:-1]):
        n = _month_number(mk)
        if n == expected:
            block.append(ded[mk]); expected -= 1
        elif n < expected:
            break
    block.reverse()
    return block, max(0, len(keys) - len(block))


def _regional_candidates(values: list[float], horizon: int = 3) -> list[float]:
    cur = values[-1]
    def slope(months: int, damp: float) -> float:
        if len(values) <= months:
            return cur
        return cur + (cur - values[-1-months]) / months * horizon * damp
    short = slope(3, 0.50)
    medium = slope(6, 0.50)
    if len(values) >= 12:
        ys = values[-12:]; n=len(ys); xb=(n-1)/2; yb=sum(ys)/n
        den=sum((i-xb)**2 for i in range(n)) or 1.0
        b=sum((i-xb)*(y-yb) for i,y in enumerate(ys))/den
        linear = cur + b*horizon*0.65
    else:
        linear = cur
    mean36 = sum(values[-36:])/min(36,len(values))
    meanrev = cur + (mean36-cur)*min(0.25, horizon/12*0.25)
    return [cur, short, medium, linear, meanrev]


def _regional_yoy_forecast(history: list[dict[str, Any]], horizon: int = 3) -> dict[str, Any]:
    continuous, gaps = _recent_contiguous_month_history(history)
    vals=[float(x["value"]) for x in continuous if _num(x.get("value")) is not None]
    if len(vals) < 18:
        return {"forecast": vals[-1] if vals else None, "model":"persistence_insufficient_history", "samples":0, "skill_pct":0.0, "direction_accuracy":None, "fallback_used":True, "history_points_total":len(history), "history_points_contiguous":len(vals), "history_gaps_dropped":gaps, "quality_gate":{"passed":False,"reason":"연속 월별 YoY 이력 18개월 미만"}}
    start=max(12,len(vals)-60-horizon)
    errs=[[] for _ in range(5)]; base=[]
    origins=list(range(start,len(vals)-horizon))
    for o in origins:
        tr=vals[:o+1]; actual=vals[o+horizon]; preds=_regional_candidates(tr,horizon)
        for i,pred in enumerate(preds): errs[i].append(pred-actual)
        base.append(tr[-1]-actual)
    rmses=[math.sqrt(sum(e*e for e in es)/len(es)) if es else 999.0 for es in errs]
    inv=[1/(r*r+0.04) for r in rmses]; sw=sum(inv) or 1.0; weights=[x/sw for x in inv]
    preds=_regional_candidates(vals,horizon); fc=sum(w*p for w,p in zip(weights,preds))
    n=min((len(e) for e in errs),default=0); ens=[]
    for k in range(n): ens.append(sum(weights[i]*errs[i][k] for i in range(5)))
    rmse=math.sqrt(sum(e*e for e in ens)/len(ens)) if ens else 999.0
    br=math.sqrt(sum(e*e for e in base)/len(base)) if base else 999.0
    skill=(1-rmse/br)*100 if br>0 and br<900 else 0.0
    hits=cases=0
    for k,o in enumerate(origins[:n]):
        actual=vals[o+horizon]-vals[o]; pred=(vals[o+horizon]+ens[k])-vals[o]
        if abs(actual)>=0.15:
            cases+=1; hits += int((actual>=0)==(pred>=0))
    da=hits/cases*100 if cases else None
    fallback=skill<=0 or n<12
    if fallback: fc=vals[-1]; skill=max(0.0,skill); rmse=br
    passed=(not fallback) and n>=18 and skill>=3.0 and (da is None or da>=55.0)
    return {"forecast":fc,"model":"inverse-RMSE ensemble of persistence/3m/6m/12m/mean-reversion" if not fallback else "persistence", "samples":n,"rmse":rmse,"baseline_rmse":br,"skill_pct":skill,"direction_accuracy":da,"fallback_used":fallback,"history_points_total":len(history),"history_points_contiguous":len(vals),"history_gaps_dropped":gaps,"quality_gate":{"passed":passed,"requirements":{"samples_min":18,"skill_pct_min":3.0,"direction_accuracy_min":55.0}}}


def _read_cn_history_cache() -> list[dict[str, Any]]:
    try:
        obj=json.loads(CN_HISTORY_CACHE_PATH.read_text(encoding="utf-8")); rows=obj.get("history") if isinstance(obj,dict) else []
        current_month = _current_utc_month()
        return [
            r for r in rows
            if r.get("date")
            and _num(r.get("value")) is not None
            and str(r.get("date"))[:7] <= current_month
        ]
    except Exception:
        return []


def _write_cn_history_cache(rows: list[dict[str, Any]]) -> None:
    CN_HISTORY_CACHE_PATH.parent.mkdir(parents=True,exist_ok=True)
    current_month = _current_utc_month()
    ded={
        str(r["date"])[:7]:{"date":str(r["date"])[:7]+"-01","value":float(r["value"])}
        for r in rows
        if r.get("date")
        and _num(r.get("value")) is not None
        and str(r["date"])[:7] <= current_month
    }
    clean=[ded[k] for k in sorted(ded)]
    contiguous,_=_recent_contiguous_month_history(clean)
    CN_HISTORY_CACHE_PATH.write_text(json.dumps({"saved_at_utc":now_iso(),"history":clean,"bootstrap_complete":len(contiguous)>=18,"contiguous_points":len(contiguous)},ensure_ascii=False,indent=2),encoding="utf-8")

def _cn_bootstrap_complete() -> bool:
    try:
        obj=json.loads(CN_HISTORY_CACHE_PATH.read_text(encoding="utf-8"))
        return bool(isinstance(obj,dict) and obj.get("bootstrap_complete"))
    except Exception:
        return False


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
        "forecast_confidence": m2.get("confidence"), "forecast_validation": m2.get("backtest_3m") or {},
        "forecast_quality_gate": m2.get("forecast_quality_gate") or {}, "yoy_history": m2.get("yoy_history") or [],
        "status": m2.get("status") or "LIVE",
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
    inputs.append({"name":"Global M2 3개월 예상 변화","current":round(current,4),"forecast":round(forecast,4),"signal":round(m2_signal,4),"weight":0.60,"meaning":"광의통화 증가율 상승은 유동성에 우호적","validation":"지역별 benchmark-safe forecast"})

    usctx = context.get("us_engine") or {}
    dxy = usctx.get("dxy") or {}
    dxy_change = _num(dxy.get("forecast_change_3m_pct"))
    dxy_gate = bool((((dxy.get("backtest_3m") or {}).get("fallback_used")) is False) and _num((dxy.get("backtest_3m") or {}).get("skill_pct")) is not None and float((dxy.get("backtest_3m") or {}).get("skill_pct") or 0) > 0)
    if dxy.get("available") and dxy_change is not None:
        sig = -max(-1.0, min(1.0, dxy_change / 3.0)) if dxy_gate else 0.0
        inputs.append({"name":"DXY 3개월 예상","current":dxy.get("current"),"forecast":dxy.get("forecast_3m"),"signal":round(sig,4),"weight":0.25 if dxy_gate else 0.0,"meaning":"달러 약세는 글로벌 달러유동성에 상대적으로 우호적","source":dxy.get("source"),"validation":"통과" if dxy_gate else "미통과·상위합성 제외"})

    # Primary real-rate source is the US engine. Card 8 remains an independent
    # fallback only when the US-engine block is unavailable. A failed forecast
    # quality gate contributes zero forward signal rather than half-weight noise.
    real = usctx.get("real_rate") or {}
    real_cur = _num(real.get("current_pct"))
    real3 = _num(real.get("forecast_3m_pct"))
    real_gate = bool((real.get("forecast_quality_gate") or {}).get("passed")) or bool(real.get("forecast_usable_3m"))
    real_source = real.get("source")
    real_origin = "US Fed engine"
    if real_cur is None or real3 is None:
        card8 = context.get("card8") or {}
        target = (((card8.get("forecasts") or {}).get("3m") or {}).get("targets") or {}).get("DFII10") or {}
        real_cur = _num((((card8.get("current") or {}).get("DFII10") or {}).get("value")))
        real3 = _num(target.get("forecast"))
        real_gate = bool((target.get("quality_gate") or {}).get("passed"))
        real_source = target.get("source") or "Card 8 DFII10"
        real_origin = "Card 8 fallback"
    if real_cur is not None and real3 is not None:
        sig = -max(-1.0, min(1.0, (real3 - real_cur) / 0.35)) if real_gate else 0.0
        inputs.append({"name":"미국 10년 실질금리 3개월 예상","current":real_cur,"forecast":real3,"signal":round(sig,4),"weight":0.15 if real_gate else 0.0,"meaning":"실질금리 하락은 위험자산 유동성 환경에 우호적","validation":"통과" if real_gate else "미통과·상위합성 제외","source":real_source,"origin":real_origin})

    active = [x for x in inputs if float(x.get("weight") or 0.0) > 0]
    sw = sum(float(x["weight"]) for x in active) or 1.0
    score = sum(float(x["signal"]) * float(x["weight"]) for x in active) / sw * 100.0
    return {
        "score": round(score, 2), "grade": _liquidity_grade(score),
        "direction": "improving" if score >= 10 else "deteriorating" if score <= -10 else "neutral",
        "inputs": inputs, "validated_input_count": len(active),
        "validation_status": "PARTIALLY_VALIDATED_INPUTS",
        "note": "M2 전망과 분리된 보조 유동성 환경지표입니다. DXY·실질금리는 자체 forecast gate를 통과한 경우에만 합성점수에 들어가며, 실패 시 weight=0입니다. 합성점수 자체의 자산수익률 OOS 검증은 별도 과제입니다.",
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
    soup = BeautifulSoup(_pbc_response_html(r), "html.parser")
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
        "yoy_pct": latest, "yoy_3m_ago_pct": prior3, "yoy_history": _history_from_yoy_pairs(vals),
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
        out = {"date": latest_m + "-01", "yoy_pct": latest_yoy, "yoy_3m_ago_pct": prior3, "yoy_history": _history_from_yoy_pairs(vals)}
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
    # Accept ASCII/full-width parentheses and punctuation/layout variants.
    if not yoy_m:
        yoy_m = re.search(
            r"(?:广义货币(?:供应量)?\s*[（(]?\s*M2\s*[）)]?|广义货币(?:供应量)?|M2)"
            r"[^。；;]{0,320}?(?:同比增长|同比增速\s*(?:为)?|同比\s*(?:增长|增速)?|增长)"
            r"\s*([0-9]+(?:\.[0-9]+)?)\s*%",
            plain,
            re.I,
        )

    # PBC reports also expose a stable section heading such as "一、广义货币增长8%".
    # Keep this as a narrowly-scoped fallback only after the M2/body patterns above.
    if not yoy_m:
        yoy_m = re.search(
            r"(?:^|\s)(?:[一二三四五六七八九十]+[、.]\s*)?广义货币(?:\s*[（(]?\s*M2\s*[）)]?)?"
            r"\s*(?:增长|同比增长|同比增速(?:为)?)\s*([0-9]+(?:\.[0-9]+)?)\s*%",
            plain,
            re.I,
        )

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
    text = str(label or "").replace("\u00c2\u00a0", " ").replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()

    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s*,?\s*(20\d{2})",
        text,
        re.I,
    )
    if m:
        month_no = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }[m.group(1).lower()]
        return f"{int(m.group(2)):04d}-{month_no:02d}"

    cm = re.search(r"(20\d{2})年\s*(1[0-2]|0?[1-9])月", text)
    if cm:
        return f"{int(cm.group(1)):04d}-{int(cm.group(2)):02d}"

    y = re.search(r"(20\d{2})", text)
    if not y:
        return None
    year = int(y.group(1))

    if "上半年" in text or "前6个月" in text or re.search(r"\bH1\b|first half", text, re.I):
        return f"{year:04d}-06"
    if "前三季度" in text or "前9个月" in text or re.search(r"\bQ1\s*[-–]\s*Q3\b|first three quarters", text, re.I):
        return f"{year:04d}-09"
    if "一季度" in text or "前3个月" in text or re.search(r"\bQ1\b|first quarter", text, re.I):
        return f"{year:04d}-03"

    if re.fullmatch(rf"{year}年金融统计数据(?:报告|解读)", text):
        return f"{year:04d}-12"
    if re.search(r"\bannual\b|full[- ]year|year[- ]end", text, re.I):
        return f"{year:04d}-12"
    if re.search(rf"Financial Statistics Report\s*\(\s*{year}\s*\)", text, re.I):
        return f"{year:04d}-12"

    return None


def _pbc_listing_candidates(html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        label = " ".join(a.stripped_strings).strip()
        if not re.search(r"Financial Statistics Report|金融统计数据(?:报告|解读)", label, re.I):
            continue
        month = _pbc_report_month(label)
        if not month:
            continue
        # Financial-statistics observations cannot be in a future month.
        # This blocks a malformed/ambiguous title from becoming e.g. 2026-12
        # while the current calendar month is only 2026-08.
        if month > _current_utc_month():
            continue
        href = urljoin(base_url, str(a.get("href") or ""))
        if "pbc.gov.cn" not in href.lower() or href in seen:
            continue
        seen.add(href)
        out.append({"month": month, "label": label, "url": href})
    return sorted(out, key=lambda x: x["month"], reverse=True)


def _pbc_cn_archive_candidates(
    session: requests.Session,
    headers: dict[str, str],
    target_months: set[str] | None = None,
) -> tuple[list[dict[str, str]], int]:
    """Discover exact missing Financial Statistics Reports from PBC's official Chinese archive.

    The Chinese 数据解读 archive is paginated and exposes the historical monthly,
    quarterly and annual report links that the compact English index omits.
    The recent 18-month bootstrap only needs archive pages 1-2 for the current
    window; page 3 is a bounded safety fallback.
    """
    wanted = set(target_months or [])
    found: dict[str, dict[str, str]] = {}
    calls = 0
    urls = [
        PBC_REPORTS_CN,
        PBC_REPORTS_CN_PAGE.format(page=2),
        PBC_REPORTS_CN_PAGE.format(page=3),
    ]
    for u in urls:
        if wanted and wanted.issubset(found):
            break
        try:
            r = _get_retry(session, u, headers=headers, attempts=1, timeout=(5, 10))
            calls += 1
            # PBC's Chinese archive is sometimes served without a reliable charset
            # header. Decode from raw bytes so Chinese titles remain intact.
            archive_html = _pbc_response_html(r)
            for c in _pbc_listing_candidates(archive_html, u):
                mk = c.get("month")
                if not mk:
                    continue
                if wanted and mk not in wanted:
                    continue
                found.setdefault(mk, c)
        except Exception:
            calls += 1
            continue
    return [found[k] for k in sorted(found, reverse=True)], calls


def _pbc_observation_month_from_text(text: str, fallback_month: str | None = None) -> str | None:
    """Infer the economic observation month from the report body, not its publication date."""
    plain = BeautifulSoup(str(text or ""), "html.parser").get_text(" ", strip=True)

    # Chinese body: "6月末，广义货币(M2)..."
    cm = re.search(r"(20\d{2})年[^。]{0,120}?([1-9]|1[0-2])月末", plain)
    if cm:
        return f"{int(cm.group(1)):04d}-{int(cm.group(2)):02d}"

    # If the year is only present elsewhere in the article, combine it with "6月末".
    cmonth = re.search(r"(?:^|[^\d])([1-9]|1[0-2])月末", plain)
    year_m = re.search(r"(20\d{2})年", plain)
    if cmonth and year_m:
        return f"{int(year_m.group(1)):04d}-{int(cmonth.group(1)):02d}"

    # English body: "At end-June, broad money supply (M2)..."
    em = re.search(
        r"At\s+end[-\s]+(January|February|March|April|May|June|July|August|September|October|November|December)",
        plain,
        re.I,
    )
    if em:
        months = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        year_match = re.search(r"(20\d{2})", plain)
        if year_match:
            return f"{int(year_match.group(1)):04d}-{months[em.group(1).lower()]:02d}"

    # Period-report fallback.
    year_match = re.search(r"(20\d{2})", plain)
    if year_match:
        y = int(year_match.group(1))
        if "上半年" in plain or re.search(r"\bH1\b|first half", plain, re.I):
            return f"{y:04d}-06"

    return fallback_month


def _pbc_parse_candidate(rr_text: str, c: dict[str, str]) -> dict[str, Any] | None:
    """Parse PBC M2 and anchor date to the report's economic observation month."""
    parsed = _extract_pbc_article(rr_text, c["url"])
    if not parsed:
        return None

    obs_month = _pbc_observation_month_from_text(rr_text, c.get("month"))
    if obs_month and obs_month <= _current_utc_month():
        parsed["date"] = obs_month + "-01"
    elif c.get("month") and str(c["month"]) <= _current_utc_month():
        parsed["date"] = str(c["month"]) + "-01"
    else:
        return None
    return parsed


def _pbc_response_html(response: requests.Response) -> str:
    """Decode PBC HTML from raw bytes first, avoiding mojibake from unreliable charset headers."""
    content = getattr(response, "content", b"") or b""
    if content:
        try:
            return str(BeautifulSoup(content, "html.parser"))
        except Exception:
            pass
    return str(getattr(response, "text", "") or "")


def _current_utc_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _month_number(month: str) -> int:
    y, m = map(int, month.split("-"))
    return y * 12 + m


def _month_key_from_number(value: int) -> str:
    y, m0 = divmod(value - 1, 12)
    return f"{y:04d}-{m0 + 1:02d}"


def _missing_recent_months(history: list[dict[str, Any]], latest_month: str, target: int = 18) -> list[str]:
    """Return exact missing month keys needed for a recent contiguous window."""
    latest_num = _month_number(latest_month)
    required = [_month_key_from_number(latest_num - i) for i in range(target - 1, -1, -1)]
    have = {str(r.get("date") or "")[:7] for r in history if r.get("date") and _num(r.get("value")) is not None}
    return [mk for mk in required if mk not in have]


def _pbc_history_bootstrap_candidates(session: requests.Session, headers: dict[str, str], seen_urls: set[str], target_months: set[str] | None = None) -> list[dict[str, str]]:
    """Discover older official PBC reports with a hard bounded search budget.

    English and Chinese report titles are both queried because older PBC search
    results are not consistently bilingual. This path is bootstrap-only: once a
    contiguous modelling window exists, normal runs make none of these calls.
    """
    out: list[dict[str, str]] = []
    wanted = set(target_months or [])
    for query in ("Financial Statistics Report", "金融统计数据报告"):
        for page in (1, 2):
            if wanted and wanted.issubset({x.get("month") for x in out}):
                break
            try:
                u = PBC_SEARCH.format(page=page, query=quote(query))
                r = _get_retry(session, u, headers=headers, attempts=1, timeout=(5, 10))
                soup = BeautifulSoup(r.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    label = " ".join(a.stripped_strings).strip()
                    month = _pbc_report_month(label)
                    href = urljoin(u, str(a.get("href") or ""))
                    if not month or not re.search(r"Financial Statistics Report|金融统计数据(?:报告|解读)", label, re.I):
                        continue
                    if "pbc.gov.cn" not in href.lower() or href in seen_urls:
                        continue
                    seen_urls.add(href)
                    out.append({"month": month, "label": label, "url": href})
            except Exception:
                continue
        if wanted and wanted.issubset({x.get("month") for x in out}):
            break
    return sorted(out, key=lambda x: x["month"], reverse=True)



def _extract_pbc_money_supply_levels(html: str) -> list[dict[str, Any]]:
    """Parse official PBC Money Supply tables into monthly M2 level rows.

    PBC annual tables expose many months in one HTML table, so three annual
    tables can provide enough history for YoY modelling with far fewer requests
    than fetching one Financial Statistics Report per month.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict[str, Any]] = {}
    month_re = re.compile(r"(20\d{2})[./-](0?[1-9]|1[0-2])")
    eng_month_re = re.compile(r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[.\s-]+(20\d{2})", re.I)
    cn_month_re = re.compile(r"(20\d{2})年\s*(1[0-2]|0?[1-9])月")
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [" ".join(td.stripped_strings).strip() for td in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if not rows:
            continue
        months: list[str] = []
        for cells in rows:
            found = []
            for cell in cells:
                for m in month_re.finditer(cell):
                    found.append(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}")
                for m in cn_month_re.finditer(cell):
                    found.append(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}")
                for m in eng_month_re.finditer(cell):
                    try:
                        mon=datetime.strptime(m.group(1)[:3].title(), "%b").month
                        found.append(f"{int(m.group(2)):04d}-{mon:02d}")
                    except Exception:
                        pass
            if len(found) >= 3:
                months = found
                break
        if not months:
            continue
        for cells in rows:
            joined = " ".join(cells)
            if not re.search(r"货币和准货币\s*[（(]?M2[）)]?|Money\s*&\s*Quasi[- ]money", joined, re.I):
                continue
            nums: list[float] = []
            for cell in cells:
                cleaned = cell.replace(",", "").replace(" ", "")
                for token in re.findall(r"(?<!\d)(\d{4,}(?:\.\d+)?)(?!\d)", cleaned):
                    try:
                        v = float(token)
                    except ValueError:
                        continue
                    if v > 10000:
                        nums.append(v)
            if len(nums) < 3:
                continue
            # Labels may occupy leading columns; numerical observations are in
            # chronological order. Pair the common tail to avoid layout offsets.
            n = min(len(months), len(nums))
            for mk, value in zip(months[-n:], nums[-n:]):
                out[mk] = {"date": mk + "-01", "level": value}
    return [out[k] for k in sorted(out)]


def _pbc_money_supply_table_candidates(session: requests.Session, headers: dict[str, str], seen_urls: set[str]) -> list[dict[str, str]]:
    """Discover recent official PBC annual Money Supply tables with a hard cap."""
    out: list[dict[str, str]] = []
    for query in ("货币供应量", "Money Supply"):
        for page in (1, 2):
            try:
                u = PBC_SEARCH.format(page=page, query=quote(query))
                r = _get_retry(session, u, headers=headers, attempts=1, timeout=(5, 10))
                soup = BeautifulSoup(r.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    context = " ".join((a.parent or a).stripped_strings).strip()
                    label = " ".join(a.stripped_strings).strip()
                    text = f"{label} {context}"
                    if not re.search(r"货币供应量|Money\s+Supply", text, re.I):
                        continue
                    href = urljoin(u, str(a.get("href") or ""))
                    if "pbc.gov.cn" not in href.lower() or href in seen_urls:
                        continue
                    seen_urls.add(href)
                    ym = re.search(r"(20\d{2})", text)
                    out.append({"year": ym.group(1) if ym else "", "label": label or context, "url": href})
            except Exception:
                continue
    # Prefer explicitly newer tables, then preserve discovery order.
    return sorted(out, key=lambda x: x.get("year") or "0000", reverse=True)


def _yoy_from_level_history(level_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by = {str(r.get("date"))[:7]: _num(r.get("level")) for r in level_rows if r.get("date")}
    out = []
    for mk in sorted(by):
        cur = by.get(mk)
        y, m = map(int, mk.split("-"))
        prev = by.get(f"{y-1:04d}-{m:02d}")
        if cur is None or prev in (None, 0):
            continue
        out.append({"date": mk + "-01", "value": (float(cur) / float(prev) - 1.0) * 100.0})
    return out

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
        candidates = _pbc_listing_candidates(_pbc_response_html(r), PBC_REPORTS_EN)
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
                if month and re.search(r"Financial Statistics Report|金融统计数据(?:报告|解读)", label, re.I) and "pbc.gov.cn" in href.lower():
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
    # IMPORTANT: cache is history/backfill only. It must never satisfy the live-fetch loop or
    # override the current observation selected from the fresh official PBC listing.
    fallback_candidates = [c for c in candidates if c not in selected][:4]
    cached_history = _read_cn_history_cache()
    live_observations: list[dict[str, Any]] = []
    bootstrap_observations: list[dict[str, Any]] = []
    bootstrap_calls = 0
    errors: list[str] = []
    for c in selected + fallback_candidates:
        if len(live_observations) >= 2:
            obs_months = sorted(str(o["date"])[:7] for o in live_observations if o.get("date"))
            if obs_months and _month_number(obs_months[-1]) - _month_number(obs_months[0]) >= 3:
                break
        try:
            rr = _get_retry(session, c["url"], headers=headers, attempts=2, timeout=(5, 12))
            parsed = _pbc_parse_candidate(_pbc_response_html(rr), c)
            if parsed and parsed.get("date") and _num(parsed.get("yoy_pct")) is not None:
                live_observations.append(parsed)
        except Exception as exc:
            errors.append(f"{c['month']}: {type(exc).__name__}: {str(exc)[:120]}")

    if not live_observations:
        raise ValueError("PBC recent Financial Statistics Report parse unavailable" + (" · " + " | ".join(errors[-2:]) if errors else ""))

    # One-time/early-run history bootstrap. Prefer official annual Money Supply
    # tables because one table contains many monthly M2 levels. Only if that
    # bounded low-call path cannot create a contiguous window do we fall back to
    # individual Financial Statistics Report articles.
    provisional = list(cached_history) + [{"date": o.get("date"), "value": o.get("yoy_pct")} for o in live_observations]
    contiguous_before, _ = _recent_contiguous_month_history(provisional)
    # First fill exact recent gaps from the already-fetched official report index.
    # This avoids four extra PBC search calls when the index itself already
    # contains the months required to reach the modelling window.
    targeted_listing_calls = 0
    if len(contiguous_before) < 18 and len(candidates) >= 6 and not _cn_bootstrap_complete():
        latest_bootstrap_month = max(str(o.get("date"))[:7] for o in live_observations if o.get("date"))
        missing = _missing_recent_months(provisional, latest_bootstrap_month, 18)
        by_month = {c.get("month"): c for c in candidates if c.get("month")}
        # Fetch only exact missing months and never more than the 17 historical
        # observations mathematically required for an 18-month window.
        for mk in missing:
            c = by_month.get(mk)
            if c is None:
                continue
            try:
                rr = _get_retry(session, c["url"], headers=headers, attempts=1, timeout=(5, 10))
                targeted_listing_calls += 1
                parsed = _pbc_parse_candidate(_pbc_response_html(rr), c)
                if parsed and parsed.get("date") and _num(parsed.get("yoy_pct")) is not None:
                    bootstrap_observations.append(parsed)
                    provisional.append({"date": parsed["date"], "value": parsed["yoy_pct"]})
            except Exception:
                targeted_listing_calls += 1
                continue
        contiguous_before, _ = _recent_contiguous_month_history(provisional)

    archive_listing_calls = 0
    archive_article_calls = 0
    if len(contiguous_before) < 18 and len(candidates) >= 6 and not _cn_bootstrap_complete():
        latest_bootstrap_month = max(str(o.get("date"))[:7] for o in live_observations if o.get("date"))
        missing_archive = set(_missing_recent_months(provisional, latest_bootstrap_month, 18))
        archive_candidates, archive_listing_calls = _pbc_cn_archive_candidates(session, headers, missing_archive)
        used = {str(r.get("date"))[:7] for r in provisional if r.get("date")}
        for c in sorted(archive_candidates, key=lambda x: x.get("month") or ""):
            if c.get("month") in used:
                continue
            try:
                rr = _get_retry(session, c["url"], headers=headers, attempts=1, timeout=(5, 10))
                archive_article_calls += 1
                parsed = _pbc_parse_candidate(_pbc_response_html(rr), c)
                if parsed and parsed.get("date") and _num(parsed.get("yoy_pct")) is not None:
                    mk = str(parsed["date"])[:7]
                    if mk not in used:
                        bootstrap_observations.append(parsed)
                        provisional.append({"date": parsed["date"], "value": parsed["yoy_pct"]})
                        used.add(mk)
            except Exception:
                archive_article_calls += 1
                continue
        contiguous_before, _ = _recent_contiguous_month_history(provisional)

    table_calls = 0
    table_new_points = 0
    table_level_rows: list[dict[str, Any]] = []
    if len(contiguous_before) < 18 and len(candidates) >= 6 and not _cn_bootstrap_complete():
        seen_urls = {c["url"] for c in candidates}
        table_candidates = _pbc_money_supply_table_candidates(session, headers, seen_urls)
        for c in table_candidates[:6]:
            provisional_now = list(cached_history) + [{"date": o.get("date"), "value": o.get("yoy_pct")} for o in live_observations]
            provisional_now += _yoy_from_level_history(table_level_rows)
            contiguous_now, _ = _recent_contiguous_month_history(provisional_now)
            if len(contiguous_now) >= 18:
                break
            try:
                rr = _get_retry(session, c["url"], headers=headers, attempts=1, timeout=(5, 10))
                table_calls += 1
                parsed_levels = _extract_pbc_money_supply_levels(_pbc_response_html(rr))
                existing = {str(x.get("date"))[:7] for x in table_level_rows if x.get("date")}
                for row in parsed_levels:
                    if str(row.get("date"))[:7] not in existing:
                        table_level_rows.append(row)
                        existing.add(str(row.get("date"))[:7])
            except Exception:
                table_calls += 1
                continue
        table_yoy = _yoy_from_level_history(table_level_rows)
        known = {str(r.get("date"))[:7] for r in cached_history if r.get("date")}
        known |= {str(o.get("date"))[:7] for o in live_observations if o.get("date")}
        for row in table_yoy:
            mk = str(row.get("date"))[:7]
            if mk and mk not in known:
                bootstrap_observations.append({"date": row["date"], "yoy_pct": row["value"], "level": None, "source_url": "PBC Money Supply annual table"})
                known.add(mk)
                table_new_points += 1

    provisional_after_tables = list(cached_history) + [{"date": o.get("date"), "value": o.get("yoy_pct")} for o in (live_observations + bootstrap_observations)]
    contiguous_after_tables, _ = _recent_contiguous_month_history(provisional_after_tables)
    if len(contiguous_after_tables) < 18 and len(candidates) >= 6 and not _cn_bootstrap_complete():
        seen_urls = {c["url"] for c in candidates}
        latest_bootstrap_month = max(str(o.get("date"))[:7] for o in live_observations if o.get("date"))
        provisional_for_gaps = list(cached_history) + [{"date": o.get("date"), "value": o.get("yoy_pct")} for o in (live_observations + bootstrap_observations)]
        missing_targets = set(_missing_recent_months(provisional_for_gaps, latest_bootstrap_month, 18))
        older = [c for c in candidates[1:] if c.get("month") in missing_targets]
        discovered = {c.get("month") for c in older if c.get("month")}
        unresolved = {mk for mk in missing_targets if mk not in discovered}
        if unresolved:
            older.extend(_pbc_history_bootstrap_candidates(session, headers, seen_urls, unresolved))
        older = sorted({c["url"]: c for c in older}.values(), key=lambda x: (x.get("month") not in missing_targets, x.get("month") or ""), reverse=False)
        cached_months = {str(r.get("date"))[:7] for r in cached_history if r.get("date")}
        live_months = {str(r.get("date"))[:7] for r in live_observations if r.get("date")}
        bootstrap_months = {str(r.get("date"))[:7] for r in bootstrap_observations if r.get("date")}
        used_months = set(cached_months) | set(live_months) | set(bootstrap_months)
        # Article fallback is intentionally smaller now that annual tables are
        # attempted first. This avoids the former search/article fan-out.
        max_article_backfill = min(17, max(0, len(missing_targets)))
        for c in older:
            if bootstrap_calls >= max_article_backfill:
                break
            provisional_now = list(cached_history) + [{"date": o.get("date"), "value": o.get("yoy_pct")} for o in (live_observations + bootstrap_observations)]
            contiguous_now, _ = _recent_contiguous_month_history(provisional_now)
            if len(contiguous_now) >= 18:
                break
            if c.get("month") in used_months or (missing_targets and c.get("month") not in missing_targets):
                continue
            try:
                rr = _get_retry(session, c["url"], headers=headers, attempts=1, timeout=(5, 10))
                bootstrap_calls += 1
                parsed = _pbc_parse_candidate(_pbc_response_html(rr), c)
                if parsed and parsed.get("date") and _num(parsed.get("yoy_pct")) is not None:
                    mk = str(parsed["date"])[:7]
                    if mk not in used_months:
                        bootstrap_observations.append(parsed)
                        used_months.add(mk)
            except Exception:
                bootstrap_calls += 1
                continue

    # The current value must come from this run's official PBC article, never silently from cache.
    live_dedup = {str(o["date"])[:7]: o for o in live_observations}
    live_rows = [live_dedup[k] for k in sorted(live_dedup)]
    latest = live_rows[-1]
    latest_m = str(latest["date"])[:7]

    # Merge cached history only for historical modelling / genuine 3m backfill, and cap it at
    # the live latest month so a stale/future cache entry cannot override a fresh listing fixture.
    cached_obs = [
        {"date": r["date"], "yoy_pct": float(r["value"]), "level": None, "source_url": None}
        for r in cached_history
        if r.get("date") and _num(r.get("value")) is not None and str(r["date"])[:7] <= latest_m
    ]
    dedup = {str(o["date"])[:7]: o for o in cached_obs}
    for o in bootstrap_observations:
        mk = str(o.get("date") or "")[:7]
        if mk and mk <= latest_m:
            dedup[mk] = o
    for o in live_rows:
        dedup[str(o["date"])[:7]] = o  # live wins for overlapping months
    rows = [dedup[k] for k in sorted(dedup)]

    target_num = _month_number(latest_m) - 3
    prior_candidates = [o for o in rows if str(o.get("date", ""))[:7] != latest_m and _month_number(str(o["date"])[:7]) <= target_num]
    if not prior_candidates:
        raise ValueError("PBC latest M2 obtained but genuine 3-month comparison observation unavailable")
    prior = prior_candidates[-1]

    hist=[
        {"date":str(o["date"])[:7]+"-01","value":float(o["yoy_pct"])}
        for o in rows
        if o.get("date")
        and _num(o.get("yoy_pct")) is not None
        and str(o["date"])[:7] <= latest_m
    ]
    _write_cn_history_cache(hist)
    return {
        "region": "CN", "label": "China M2", "date": latest.get("date"),
        "yoy_pct": float(latest["yoy_pct"]),
        "yoy_3m_ago_pct": float(prior["yoy_pct"]),
        "level": latest.get("level"),
        "source": "People's Bank of China Financial Statistics Reports",
        "source_url": latest.get("source_url"),
        "history_points": len(hist), "yoy_history": hist,
        "history_bootstrap": {
            "triggered": (table_calls + bootstrap_calls) > 0,
            "targeted_listing_calls": targeted_listing_calls,
            "archive_listing_calls": archive_listing_calls,
            "archive_article_calls": archive_article_calls,
            "table_calls": table_calls,
            "table_new_points": table_new_points,
            "article_calls": bootstrap_calls,
            "new_points": len(bootstrap_observations),
            "target_contiguous_points": 18,
            "contiguous_points": len(_recent_contiguous_month_history(hist)[0]),
            "complete": len(_recent_contiguous_month_history(hist)[0]) >= 18,
            "strategy": "exact missing months from official English index first; official PBC Chinese data-interpretation archive second; annual Money Supply/search fallback only for unresolved gaps",
        },
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




def _regional_confidence(code: str, audit: dict[str, Any]) -> dict[str, Any]:
    passed=bool((audit.get("quality_gate") or {}).get("passed"))
    samples=int(audit.get("samples") or audit.get("backtests") or 0)
    skill=float(audit.get("skill_pct") or 0.0)
    da=audit.get("direction_accuracy")
    score=35
    if passed:
        score=min(90,55+min(20,max(0,skill))+min(10,samples/12)+ (5 if da is not None and float(da)>=60 else 0))
    grade="HIGH" if score>=75 else "MEDIUM" if score>=55 else "LOW"
    return {"score":round(score),"grade":grade,"passed":passed,"samples":samples,"skill_pct":skill}

def _global_m2_composite_validation(components: dict[str, Any], horizon: int = 3) -> dict[str, Any]:
    """Walk-forward validation of the final fixed-weight composite itself.

    Requires contemporaneous monthly YoY history for all four strategic regions;
    no renormalized partial-history backtest is treated as equivalent to the
    production 40/30/20/10 definition.
    """
    by_region={}
    for code in WEIGHTS:
        hist=(components.get(code) or {}).get("yoy_history") or []
        by_region[code]={str(r.get("date"))[:7]:float(r.get("value")) for r in hist if r.get("date") and _num(r.get("value")) is not None}
    common=sorted(set.intersection(*(set(x) for x in by_region.values()))) if all(by_region.values()) else []
    if len(common) < 30+horizon:
        return {"available":False,"status":"INSUFFICIENT_HISTORY","samples":0,"required_common_months":30+horizon,"common_months":len(common),"benchmark":"global-composite persistence"}
    series=[sum(WEIGHTS[c]*by_region[c][m] for c in WEIGHTS) for m in common]
    model_err=[]; base_err=[]; hits=cases=0
    for i in range(24,len(series)-horizon):
        hist=series[:i+1]
        try: audit=_regional_yoy_forecast([{"date":common[j]+"-01","value":series[j]} for j in range(i+1)],horizon)
        except Exception: continue
        pred=float(audit.get("forecast") if _num(audit.get("forecast")) is not None else hist[-1]); actual=series[i+horizon]; base=hist[-1]
        model_err.append(pred-actual); base_err.append(base-actual)
        if abs(actual-base)>=0.05:
            cases+=1; hits+=int((pred-base>=0)==(actual-base>=0))
    if not model_err:
        return {"available":False,"status":"INSUFFICIENT_HISTORY","samples":0,"common_months":len(common)}
    r=_rmse(model_err); br=_rmse(base_err); skill=(1-r/br)*100 if br>0 else None; da=hits/cases*100 if cases else None
    passed=bool(len(model_err)>=18 and skill is not None and skill>=3 and da is not None and da>=55)
    return {"available":True,"status":"VALIDATED" if passed else "REFERENCE_ONLY","samples":len(model_err),"rmse":r,"baseline_rmse":br,"skill_pct":skill,"direction_accuracy":da,"direction_cases":cases,"quality_gate":{"passed":passed,"requirements":{"samples_min":18,"skill_pct_min":3.0,"direction_accuracy_min":55.0}},"benchmark":"global-composite persistence","common_months":len(common)}



def _global_m2_validated_subcomposite(components: dict[str, Any], horizon: int = 3) -> dict[str, Any]:
    """Validate the largest fixed-weight regional subset with adequate history.

    This does *not* replace the production US/CN/EA/JP 40/30/20/10 composite.
    It provides an honest OOS bridge while China's official-history archive is
    still too short for a four-region composite backtest. Original strategic
    weights are retained and renormalized only inside this explicitly labelled
    validation subset.
    """
    histories: dict[str, dict[str, float]] = {}
    eligible: list[str] = []
    for code in WEIGHTS:
        hist = (components.get(code) or {}).get("yoy_history") or []
        mapped = {str(r.get("date"))[:7]: float(r.get("value")) for r in hist if r.get("date") and _num(r.get("value")) is not None}
        histories[code] = mapped
        if len(mapped) >= 36:
            eligible.append(code)
    coverage = sum(WEIGHTS[c] for c in eligible)
    if coverage < 0.60 or len(eligible) < 2:
        return {"available": False, "status": "INSUFFICIENT_HISTORY", "samples": 0, "coverage_weight": round(coverage, 4), "regions": eligible, "benchmark": "validated-subcomposite persistence"}
    common = sorted(set.intersection(*(set(histories[c]) for c in eligible)))
    if len(common) < 30 + horizon:
        return {"available": False, "status": "INSUFFICIENT_HISTORY", "samples": 0, "coverage_weight": round(coverage, 4), "regions": eligible, "common_months": len(common), "benchmark": "validated-subcomposite persistence"}
    w = {c: WEIGHTS[c] / coverage for c in eligible}
    series = [sum(w[c] * histories[c][m] for c in eligible) for m in common]
    model_err: list[float] = []
    base_err: list[float] = []
    hits = cases = 0
    for i in range(24, len(series) - horizon):
        hist = [{"date": common[j] + "-01", "value": series[j]} for j in range(i + 1)]
        audit = _regional_yoy_forecast(hist, horizon)
        pred = float(audit.get("forecast") if _num(audit.get("forecast")) is not None else series[i])
        actual, base = series[i + horizon], series[i]
        model_err.append(pred - actual); base_err.append(base - actual)
        if abs(actual - base) >= 0.05:
            cases += 1; hits += int((pred - base >= 0) == (actual - base >= 0))
    if not model_err:
        return {"available": False, "status": "INSUFFICIENT_HISTORY", "samples": 0, "coverage_weight": round(coverage, 4), "regions": eligible}
    rmse = (sum(e * e for e in model_err) / len(model_err)) ** 0.5
    baseline = (sum(e * e for e in base_err) / len(base_err)) ** 0.5
    skill = (1 - rmse / baseline) * 100 if baseline > 0 else None
    da = hits / cases * 100 if cases else None
    passed = bool(len(model_err) >= 18 and skill is not None and skill >= 3 and da is not None and da >= 55)
    return {
        "available": True, "status": "VALIDATED" if passed else "REFERENCE_ONLY", "samples": len(model_err),
        "rmse": rmse, "baseline_rmse": baseline, "skill_pct": skill, "direction_accuracy": da, "direction_cases": cases,
        "coverage_weight": round(coverage, 4), "regions": eligible, "weights_used": {c: round(w[c], 6) for c in eligible},
        "common_months": len(common), "benchmark": "validated-subcomposite persistence",
        "quality_gate": {"passed": passed, "requirements": {"samples_min": 18, "skill_pct_min": 3.0, "direction_accuracy_min": 55.0, "coverage_weight_min": 0.60}},
        "production_definition_changed": False,
        "note": "Validation-only subset; the live Global M2 point forecast remains the full 40/30/20/10 strategic composite with benchmark-safe regional fallbacks.",
    }

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
        cur = float(c["yoy_pct"])
        audit: dict[str, Any]
        if k == "US" and _num(c.get("forecast_yoy_3m_pct")) is not None:
            fc = float(c["forecast_yoy_3m_pct"])
            method = "US Fed-engine history model"
            has_direct_us = True
            src_bt = c.get("forecast_validation") if isinstance(c.get("forecast_validation"), dict) else {}
            src_gate = c.get("forecast_quality_gate") if isinstance(c.get("forecast_quality_gate"), dict) else {}
            audit = {
                "skill_pct": src_bt.get("skill_pct"), "rmse_pct": src_bt.get("rmse_pct"),
                "baseline_rmse_pct": src_bt.get("baseline_rmse_pct"), "backtests": src_bt.get("backtests"),
                "fallback_used": bool(src_bt.get("fallback_used", False)),
                "quality_gate": src_gate or {"passed": not bool(src_bt.get("fallback_used", False))},
                "source_confidence": c.get("forecast_confidence"),
            }
        else:
            hist = c.get("yoy_history") if isinstance(c.get("yoy_history"), list) else []
            audit = _regional_yoy_forecast(hist, 3) if hist else {"forecast":cur,"model":"persistence_no_history","samples":0,"skill_pct":0.0,"fallback_used":True,"quality_gate":{"passed":False,"reason":"장기 월별 YoY 이력 미확보"}}
            fc = float(audit.get("forecast")) if _num(audit.get("forecast")) is not None else cur
            method = "validated regional YoY walk-forward model" if not audit.get("fallback_used") else "persistence safety fallback"
        component_forecasts[k] = {"current_yoy_pct": round(cur,6), "forecast_3m_yoy_pct": round(fc,6), "method": method, "validation": audit, "forecast_confidence": _regional_confidence(k, audit)}
    forecast = sum(norm_weights[k] * float(component_forecasts[k]["forecast_3m_yoy_pct"]) for k in usable) if has_direct_us else legacy_forecast
    composite_validation = _global_m2_composite_validation(components, 3)
    validated_subcomposite = _global_m2_validated_subcomposite(components, 3)
    regional_conf = {k: v.get("forecast_confidence") for k, v in component_forecasts.items()}
    weighted_region_conf = sum(norm_weights[k] * float((regional_conf.get(k) or {}).get("score") or 35) for k in usable)
    validation_bonus = 0.0
    if (validated_subcomposite.get("quality_gate") or {}).get("passed"):
        validation_bonus = min(8.0, 3.0 + max(0.0, float(validated_subcomposite.get("skill_pct") or 0.0)))
    aggregate_forecast_confidence = round(min(85.0, weighted_region_conf + validation_bonus))
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
        "forecast_model_detail": "US uses the Fed-engine history model; CN/EA/JP use benchmark-safe regional YoY forecasts and automatically fall back to persistence when skill is not positive.",
        "forecast_components": component_forecasts,
        "composite_forecast_validation_3m": composite_validation,
        "validated_subcomposite_forecast_validation_3m": validated_subcomposite,
        "forecast_confidence": {
            "score": aggregate_forecast_confidence,
            "grade": "HIGH" if aggregate_forecast_confidence >= 75 else ("MEDIUM" if aggregate_forecast_confidence >= 55 else "LOW"),
            "full_composite_oos_passed": bool((composite_validation.get("quality_gate") or {}).get("passed")),
            "validated_subcomposite_oos_passed": bool((validated_subcomposite.get("quality_gate") or {}).get("passed")),
            "validated_coverage_weight": validated_subcomposite.get("coverage_weight"),
            "score_semantics": "regional benchmark-safe confidence plus capped OOS evidence bonus; not a hit probability",
        },
        "forecast_confidence_by_region": regional_conf,
        "forward_liquidity_outlook": forward_liquidity,
        "us_dxy": ((forward_context.get("us_engine") or {}).get("dxy") or {}),
        "us_real_rate": ((forward_context.get("us_engine") or {}).get("real_rate") or {}),
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
        "methodology": "Weighted YoY broad-money growth composite. Current weights remain US/CN/EA/JP 40/30/20/10. US reuses the Fed-engine history model. CN/EA/JP use monthly YoY walk-forward models only when they beat persistence; otherwise they automatically remain at persistence. The prior composite-acceleration forecast is retained as legacy_forecast for audit. DXY and real-yield signals are reported separately and never alter the M2 level forecast.",
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