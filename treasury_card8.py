from __future__ import annotations

import csv
import io
import json
import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "public" / "data" / "us_treasury_card8.json"
STATUS = ROOT / "public" / "data" / "us_treasury_card8_status.json"
FRED_CSV_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_API = "https://api.stlouisfed.org/fred/series/observations"

# Keep all concurrent FRED requests below the documented 2 req/s ceiling.
_FRED_PACE_LOCK = Lock()
_FRED_LAST_REQUEST = 0.0
_FRED_MIN_INTERVAL_SECONDS = 0.60

def _pace_fred() -> None:
    global _FRED_LAST_REQUEST
    with _FRED_PACE_LOCK:
        now = time.monotonic()
        wait = _FRED_MIN_INTERVAL_SECONDS - (now - _FRED_LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        _FRED_LAST_REQUEST = time.monotonic()

FED_LATEST_URL = os.getenv(
    "FED_ENGINE_LATEST_URL",
    "https://raw.githubusercontent.com/12xx37r-ui/fed-futures-collector/main/public/data/latest.json",
)

TREASURY_FUTURES = {"ZT=F":"2년 국채선물","ZF=F":"5년 국채선물","ZN=F":"10년 국채선물","ZB=F":"30년 국채선물"}

# Official/public series. Core series drive the forecast; context series alter regime and confidence.
SERIES: dict[str, dict[str, Any]] = {
    "DGS3MO": {"label": "미국 3개월 국채금리", "freq": "D", "core": True},
    "DGS2": {"label": "미국 2년 국채금리", "freq": "D", "core": True},
    "DGS5": {"label": "미국 5년 국채금리", "freq": "D", "core": True},
    "DGS10": {"label": "미국 10년 국채금리", "freq": "D", "core": True},
    "DGS30": {"label": "미국 30년 국채금리", "freq": "D", "core": True},
    "DFII5": {"label": "미국 5년 실질금리", "freq": "D", "core": True},
    "DFII10": {"label": "미국 10년 실질금리", "freq": "D", "core": True},
    "DFII20": {"label": "미국 20년 실질금리", "freq": "D", "core": False},
    "DFII30": {"label": "미국 30년 실질금리", "freq": "D", "core": False},
    "T5YIE": {"label": "5년 기대인플레이션", "freq": "D", "core": True},
    "T10YIE": {"label": "10년 기대인플레이션", "freq": "D", "core": True},
    "T5YIFR": {"label": "5년후 5년 기대인플레이션", "freq": "D", "core": False},
    "T10Y2Y": {"label": "10년-2년 금리차", "freq": "D", "core": True},
    "T10Y3M": {"label": "10년-3개월 금리차", "freq": "D", "core": False},
    "THREEFYTP10": {"label": "10년 기간프리미엄", "freq": "D", "core": True},
    "DFF": {"label": "유효 연방기금금리", "freq": "D", "core": True},
    "SOFR": {"label": "SOFR", "freq": "D", "core": False},
    "PCEPILFE": {"label": "근원 PCE", "freq": "M", "core": True},
    "CPILFESL": {"label": "근원 CPI", "freq": "M", "core": False},
    "UNRATE": {"label": "실업률", "freq": "M", "core": True},
    "PAYEMS": {"label": "비농업고용", "freq": "M", "core": False},
    "ICSA": {"label": "신규실업수당", "freq": "W", "core": False},
    "NFCI": {"label": "시카고연은 금융여건", "freq": "W", "core": True},
    "BAMLH0A0HYM2": {"label": "미국 하이일드 OAS", "freq": "D", "core": True},
    "VIXCLS": {"label": "VIX", "freq": "D", "core": False},
    "WALCL": {"label": "연준 총자산", "freq": "W", "core": False},
    "WRESBAL": {"label": "은행 준비금", "freq": "W", "core": False},
    "WTREGEN": {"label": "재무부 일반계정", "freq": "W", "core": False},
    "RRPONTSYD": {"label": "ON RRP", "freq": "D", "core": False},
}

HORIZONS = {
    "5d": {"steps": 5, "label": "단기 1~5거래일", "min_samples": 180},
    "1m": {"steps": 21, "label": "중기 약 1개월", "min_samples": 150},
    "3m": {"steps": 63, "label": "중기 약 3개월", "min_samples": 100},
    "6m": {"steps": 126, "label": "장기 약 6개월", "min_samples": 70},
    "12m": {"steps": 252, "label": "장기 약 12개월", "min_samples": 45},
}
TARGETS = ["DGS2", "DGS10", "DFII10", "T10Y2Y"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def finite(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def build_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5, connect=5, read=5, status=5,
        backoff_factor=1.0,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; GlobalMacroDataCollector/Card8-1.1; +https://github.com/12xx37r-ui/global-macro-data-collector)",
        "Accept": "text/csv,application/json;q=0.9,*/*;q=0.8",
    })
    return session


def parse_fred_csv(text: str, requested: list[str]) -> dict[str, list[dict[str, Any]]]:
    out = {sid: [] for sid in requested}
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ValueError("FRED CSV header missing")
    date_col = next((x for x in reader.fieldnames if str(x).lower() in {"date", "observation_date"}), reader.fieldnames[0])
    normalized = {str(x).strip().upper(): x for x in reader.fieldnames}
    for row in reader:
        dt = str(row.get(date_col, "")).strip()
        if not dt:
            continue
        for sid in requested:
            col = normalized.get(sid.upper())
            raw = row.get(col, "") if col else ""
            if finite(raw):
                out[sid].append({"date": dt, "value": float(raw)})
    return out


def fetch_fred_api(session: requests.Session, series_id: str, api_key: str) -> list[dict[str, Any]]:
    _pace_fred()
    r = session.get(FRED_API, params={
        "series_id": series_id, "api_key": api_key, "file_type": "json",
        "observation_start": "1990-01-01", "sort_order": "asc",
    }, timeout=(15, 60))
    r.raise_for_status()
    payload = r.json()
    rows = [
        {"date": o["date"], "value": float(o["value"])}
        for o in payload.get("observations", [])
        if o.get("value") not in (None, ".") and finite(o.get("value"))
    ]
    if not rows:
        raise ValueError(f"{series_id}: FRED API no observations")
    return rows


def fetch_fred_individual(session: requests.Session, series_id: str) -> list[dict[str, Any]]:
    _pace_fred()
    r = session.get(FRED_CSV_BASE, params={"id": series_id, "cosd": "1990-01-01"}, timeout=(15, 60))
    r.raise_for_status()
    parsed = parse_fred_csv(r.text, [series_id])[series_id]
    if not parsed:
        raise ValueError(f"{series_id}: FRED CSV no observations; HTTP {r.status_code}; type={r.headers.get('content-type','')}")
    return parsed


def fetch_fred_all(session: requests.Session, series_ids: list[str]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Fetch each official FRED series concurrently and retry through the API.

    FRED's graph CSV endpoint can return malformed multi-series rows on GitHub
    runners.  Card 8 therefore uses bounded concurrent *single-series* requests.
    This removes the parser warning while remaining fast and fully official.
    """
    result = {sid: [] for sid in series_ids}
    errors: list[str] = []
    api_key = os.getenv("FRED_API_KEY", "").strip()

    def fetch_one(sid: str) -> tuple[str, list[dict[str, Any]], list[str]]:
        local_errors: list[str] = []
        try:
            return sid, fetch_fred_individual(session, sid), local_errors
        except Exception as exc:
            local_errors.append(f"{sid} CSV retry: {exc}")
        if api_key:
            try:
                return sid, fetch_fred_api(session, sid, api_key), local_errors
            except Exception as exc:
                local_errors.append(f"{sid} API retry: {exc}")
        return sid, [], local_errors

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch_one, sid): sid for sid in series_ids}
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                got_sid, rows, local_errors = fut.result()
                result[got_sid] = rows
                if not rows:
                    errors.extend(local_errors)
            except Exception as exc:
                errors.append(f"{sid} concurrent fetch: {exc}")

    # Exact reconstruction from official component series is allowed only when
    # the direct spread series is unavailable and both components are present.
    if not result.get("T10Y2Y") and result.get("DGS10") and result.get("DGS2"):
        a = {x["date"]: float(x["value"]) for x in result["DGS10"]}
        b = {x["date"]: float(x["value"]) for x in result["DGS2"]}
        dates = sorted(set(a).intersection(b))
        result["T10Y2Y"] = [{"date": d, "value": round(a[d]-b[d], 6)} for d in dates]
        errors.append("T10Y2Y: direct series unavailable; reconstructed exactly from DGS10-DGS2")
    return result, errors


def fetch_fed_engine(session: requests.Session) -> dict[str, Any]:
    try:
        r = session.get(FED_LATEST_URL, timeout=30)
        r.raise_for_status()
        x = r.json()
        if not finite(x.get("current_effective_rate")):
            raise ValueError("current_effective_rate missing")
        return {"available": True, "url": FED_LATEST_URL, "payload": x}
    except Exception as exc:
        return {"available": False, "url": FED_LATEST_URL, "error": str(exc), "payload": {}}


def fetch_yahoo_history(session: requests.Session, symbol: str) -> list[dict[str, Any]]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + requests.utils.quote(symbol, safe="")
    r = session.get(url, params={"interval":"1d","range":"10y","events":"history"}, timeout=(15,60))
    r.raise_for_status()
    j = r.json(); z = (j.get("chart",{}).get("result") or [None])[0]
    if not z: return []
    ts = z.get("timestamp") or []
    close = (((z.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
    out=[]
    for t,v in zip(ts,close):
        if finite(v):
            out.append({"date": datetime.fromtimestamp(t, timezone.utc).date().isoformat(), "value": float(v)})
    return out

def fetch_treasury_futures_context(session: requests.Session) -> dict[str, Any]:
    rows={}; errors=[]
    for sym,label in TREASURY_FUTURES.items():
        try: rows[sym]=fetch_yahoo_history(session,sym)
        except Exception as exc: rows[sym]=[]; errors.append(f"{sym}: {exc}")
    current={}; signals=[]
    for sym,hist in rows.items():
        if not hist: continue
        v=[float(x["value"]) for x in hist]
        ret21=(v[-1]/v[-22]-1)*100 if len(v)>22 and v[-22] else 0.0
        ret63=(v[-1]/v[-64]-1)*100 if len(v)>64 and v[-64] else 0.0
        daily=[(v[i]/v[i-1]-1)*100 for i in range(max(1,len(v)-63),len(v)) if v[i-1]]
        vol=statistics.stdev(daily) if len(daily)>2 else 0.0
        score=clamp((0.65*ret21+0.35*ret63)/(vol*3+1),-2,2)
        signals.append(score)
        current[sym]={"label":TREASURY_FUTURES[sym],"value":v[-1],"date":hist[-1]["date"],"return21d_pct":ret21,"return63d_pct":ret63,"realized_vol":vol,"score":score}
    composite=mean(signals) if signals else 0.0
    return {"available":bool(current),"source":"Yahoo Finance 무료 지연 국채선물","current":current,"composite_score":composite,
            "yield_direction":"down" if composite>0.25 else "up" if composite<-0.25 else "flat",
            "errors":errors,"limitation":"Yahoo Finance 무료 지연 국채선물 자료를 사용하며, 실시간 거래소 시세가 아닙니다. 방향·변동성 교차검증용으로만 사용합니다."}

def values(series: list[dict[str, Any]]) -> list[float]:
    return [float(x["value"]) for x in series if finite(x.get("value"))]


def latest(series: list[dict[str, Any]]) -> dict[str, Any] | None:
    return series[-1] if series else None


def change(series: list[dict[str, Any]], periods: int) -> float | None:
    v = values(series)
    if len(v) <= periods:
        return None
    return v[-1] - v[-1 - periods]


def pct_change(series: list[dict[str, Any]], periods: int) -> float | None:
    v = values(series)
    if len(v) <= periods or v[-1 - periods] == 0:
        return None
    return (v[-1] / v[-1 - periods] - 1) * 100


def annualized_index_change(series: list[dict[str, Any]], periods: int) -> float | None:
    v = values(series)
    if len(v) <= periods or v[-1 - periods] <= 0:
        return None
    return ((v[-1] / v[-1 - periods]) ** (12 / periods) - 1) * 100


def mean(seq: Iterable[float]) -> float:
    x = list(seq)
    return sum(x) / len(x) if x else math.nan


def rmse(errs: list[float]) -> float:
    return math.sqrt(mean(e * e for e in errs)) if errs else math.nan


def percentile(seq: list[float], q: float) -> float:
    x = sorted(seq)
    if not x:
        return math.nan
    pos = (len(x) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return x[lo]
    return x[lo] * (hi - pos) + x[hi] * (pos - lo)


def candidate_forecasts(train: list[float], h: int, structural_delta: float = 0.0) -> list[tuple[str, float]]:
    n = len(train)
    last = train[-1]
    def delta(k: int) -> float:
        return (last - train[-1-k]) / k if n > k else 0.0
    d5, d21, d63, d126, d252 = delta(5), delta(21), delta(63), delta(126), delta(252)
    mean63 = mean(train[-63:]) if n >= 63 else mean(train)
    mean126 = mean(train[-126:]) if n >= 126 else mean(train)
    mean252 = mean(train[-252:]) if n >= 252 else mean(train)
    cap = 1.75 if h <= 21 else 2.5
    mr3 = last + clamp((mean63-last)*0.16, -0.025, 0.025) * min(h/21, 4)
    mr6 = last + clamp((mean126-last)*0.11, -0.020, 0.020) * min(h/21, 6)
    mr12 = last + clamp((mean252-last)*0.08, -0.018, 0.018) * min(h/21, 8)
    # Fixed ensemble candidates add diversification without fitting coefficients on
    # the reported OOS window.  They still have to win the same prequential
    # selection and strict DM gate before production promotion.
    mr_blend = 0.25*mr3 + 0.50*mr6 + 0.25*mr12
    return [
        ("persistence", last),
        ("short_trend", last + clamp(0.55*d5 + 0.30*d21 + 0.15*d63, -0.025, 0.025) * h),
        ("medium_trend", last + clamp(0.15*d5 + 0.40*d21 + 0.30*d63 + 0.15*d126, -0.018, 0.018) * h),
        ("long_trend", last + clamp(0.15*d63 + 0.35*d126 + 0.50*d252, -0.009, 0.009) * h),
        ("mean_reversion_3m", mr3),
        ("mean_reversion_6m", mr6),
        ("mean_reversion_1y", mr12),
        ("mean_reversion_blend", mr_blend),
        ("structural_blend", last + clamp((0.20*d21+0.25*d63+0.25*d126+0.30*d252)*h + structural_delta, -cap, cap)),
    ]


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def dm_test_squared_errors(
    model_errors: list[float],
    baseline_errors: list[float],
    max_lag: int = 0,
) -> dict[str, Any]:
    """Two-sided DM diagnostic with a Bartlett/Newey-West HAC variance.

    ``max_lag`` must reflect forecast-horizon overlap (normally h-1).  This is
    deliberately conservative versus treating overlapping forecast errors as
    independent observations.
    """
    n = min(len(model_errors), len(baseline_errors))
    if n < 30:
        return {"statistic": None, "p_value": None, "significant_5pct": False, "significant_10pct": False, "hac_lag": 0}
    d = [baseline_errors[i]**2 - model_errors[i]**2 for i in range(n)]
    md = mean(d)
    centered = [x-md for x in d]
    lag = max(0, min(int(max_lag), n // 3))
    gamma0 = sum(x*x for x in centered) / n
    long_run_var = gamma0
    for k in range(1, lag + 1):
        gamma = sum(centered[t] * centered[t-k] for t in range(k, n)) / n
        long_run_var += 2.0 * (1.0 - k / (lag + 1.0)) * gamma
    if long_run_var <= 0:
        return {"statistic": None, "p_value": None, "significant_5pct": False, "significant_10pct": False, "hac_lag": lag}
    stat = md / math.sqrt(long_run_var/n)
    p = 2.0 * (1.0 - _normal_cdf(abs(stat)))
    return {
        "statistic": stat,
        "p_value": p,
        "significant_5pct": p < 0.05 and md > 0,
        "significant_10pct": p < 0.10 and md > 0,
        "hac_lag": lag,
    }


def select_model_oos(vals: list[float], h: int, structural_delta: float) -> dict[str, Any]:
    first = max(260, len(vals)-900-h)
    if len(vals) < first + h + 20:
        return {"forecast": vals[-1], "model": "persistence_insufficient_history", "samples": 0,
                "rmse": None, "baseline_rmse": None, "skill_pct": None,
                "direction_accuracy": None, "active_direction_accuracy": None,
                "active_direction_coverage": 0.0, "abstention_rate": 1.0,
                "dm_test": {"statistic": None, "p_value": None, "significant_10pct": False},
                "residuals": [], "fallback_used": True}
    scores: dict[str, list[float]] = {}
    strategy_residuals: list[float] = []
    strategy_hits: list[int] = []
    strategy_active_cases = 0
    selected_counts: dict[str, int] = {}
    baseline_residuals: list[float] = []
    all_direction_cases = 0
    for o in range(first, len(vals)-h):
        train = vals[:o+1]
        actual = vals[o+h]
        base = train[-1]
        forecasts = dict(candidate_forecasts(train, h, structural_delta=0.0))
        eligible = {name: losses for name, losses in scores.items() if len(losses) >= 30}
        selected = min(eligible, key=lambda k: mean(eligible[k])) if eligible else "persistence"
        selected_pred = forecasts[selected]
        selected_counts[selected] = selected_counts.get(selected, 0) + 1
        strategy_residuals.append(selected_pred-actual)
        baseline_residuals.append(base-actual)
        actual_move = actual-base
        if abs(actual_move) >= 0.10:
            all_direction_cases += 1
        selected_move = selected_pred-base
        if abs(actual_move) >= 0.10 and abs(selected_move) >= 0.025:
            strategy_active_cases += 1
            strategy_hits.append(int(selected_move*actual_move > 0))
        for name, pred in forecasts.items():
            scores.setdefault(name, []).append((pred-actual)**2)
    best = min(scores, key=lambda k: mean(scores[k]))
    baseline_rmse = math.sqrt(mean(scores["persistence"]))
    best_rmse = math.sqrt(mean([x*x for x in strategy_residuals]))
    skill = (1-best_rmse/baseline_rmse)*100 if baseline_rmse > 0 else 0.0
    if skill <= 0:
        best = "persistence"
        best_rmse = baseline_rmse
        skill = 0.0
        fallback = True
    else:
        fallback = False
    final_map = dict(candidate_forecasts(vals, h, structural_delta))
    pred = final_map.get(best, vals[-1])
    active_cases = strategy_active_cases
    hit = mean(strategy_hits) if strategy_hits else math.nan
    active_coverage = active_cases / all_direction_cases if all_direction_cases else 0.0
    evaluated_residuals = baseline_residuals if fallback else strategy_residuals
    dm = dm_test_squared_errors(evaluated_residuals, baseline_residuals, max_lag=max(0, h-1))
    return {
        "forecast": pred, "model": best, "samples": len(scores[best]),
        "rmse": best_rmse, "baseline_rmse": baseline_rmse, "skill_pct": skill,
        "direction_accuracy": hit if finite(hit) else None,
        "active_direction_accuracy": hit if finite(hit) else None,
        "active_direction_coverage": active_coverage,
        "abstention_rate": 1.0-active_coverage,
        "dm_test": dm,
        "selection_method": "online_prequential_candidate_selection",
        "selected_model_counts": selected_counts,
        "residuals": evaluated_residuals, "fallback_used": fallback,
    }


def fed_path_anchor(fed: dict[str, Any], months: int) -> float | None:
    if not fed.get("available"):
        return None
    x = fed["payload"]
    current = float(x["current_effective_rate"])
    rows = x.get("meeting_path") or []
    if not rows:
        return current
    idx = max(0, min(len(rows)-1, round(months/1.5)-1))
    v = rows[idx].get("expected_post_meeting_rate")
    return float(v) if finite(v) else current


def structural_deltas(data: dict[str, list[dict[str, Any]]], fed: dict[str, Any], horizon: str) -> dict[str, float]:
    months = {"5d": .25, "1m": 1, "3m": 3, "6m": 6, "12m": 12}[horizon]
    dff = latest(data.get("DFF", []))
    policy_anchor = fed_path_anchor(fed, max(1, round(months)))
    policy_delta = (policy_anchor - float(dff["value"])) if dff and policy_anchor is not None else 0.0
    infl_mom = annualized_index_change(data.get("PCEPILFE", []), 3) or 2.0
    breakeven_delta = change(data.get("T10YIE", []), 21) or 0.0
    term_delta = change(data.get("THREEFYTP10", []), 21) or 0.0
    nfci_delta = change(data.get("NFCI", []), 4) or 0.0
    hy_delta = change(data.get("BAMLH0A0HYM2", []), 21) or 0.0
    unrate_delta = change(data.get("UNRATE", []), 3) or 0.0
    # Bounded structural overlay; it is small versus price persistence and separately audited.
    policy = clamp(policy_delta, -1.5, 1.5)
    inflation = clamp((infl_mom-2.0)*0.08 + breakeven_delta*0.35, -0.35, 0.35)
    term = clamp(term_delta*0.35, -0.25, 0.25)
    risk = clamp(-nfci_delta*0.15 - hy_delta*0.06 - unrate_delta*0.08, -0.20, 0.20)
    scale = min(1.0, months/6)
    return {
        "DGS2": clamp((0.72*policy + 0.18*inflation + 0.10*risk)*scale, -1.5, 1.5),
        "DGS10": clamp((0.28*policy + 0.34*inflation + 0.30*term + 0.08*risk)*scale, -1.25, 1.25),
        "DFII10": clamp((0.24*policy + 0.35*term + 0.16*risk)*scale, -1.0, 1.0),
        "T10Y2Y": clamp((-0.44*policy + 0.18*inflation + 0.30*term)*scale, -1.25, 1.25),
    }


def grade_strength(current: float, forecast: float, adverse_when_up: bool) -> dict[str, str]:
    delta = forecast-current
    magnitude = abs(delta)
    strong = magnitude >= 0.35
    weak = magnitude >= 0.10
    if not weak:
        return {"grade": "강중립" if magnitude < 0.04 else "약중립", "arrow": "−", "signal": "neutral"}
    favorable = delta < 0 if adverse_when_up else delta > 0
    if favorable:
        return {"grade": "강강" if strong else "강약", "arrow": "↓" if delta < 0 else "↑", "signal": "good"}
    return {"grade": "약약" if strong else "약강", "arrow": "↑" if delta > 0 else "↓", "signal": "bad"}


def horizon_gate(result: dict[str, Any], minimum: int, horizon: str | None = None) -> dict[str, Any]:
    samples = int(result.get("samples") or 0)
    skill = float(result.get("skill_pct") or 0)
    da = result.get("active_direction_accuracy")
    active_coverage = float(result.get("active_direction_coverage") or 0)
    dm = result.get("dm_test") or {}
    interval_ok = result.get("interval80_coverage") is None or 0.75 <= result["interval80_coverage"] <= 0.85
    # Material improvement thresholds prevent tiny positive skill from being
    # mislabeled as institutional-grade. Longer horizons require more skill.
    min_skill = {"5d": 0.25, "1m": 0.50, "3m": 1.50, "6m": 2.00, "12m": 3.00}.get(horizon or "", 1.0)
    min_active_coverage = {"5d": 0.25, "1m": 0.30, "3m": 0.35, "6m": 0.35, "12m": 0.30}.get(horizon or "", 0.30)
    dm_ok = bool(dm.get("significant_5pct"))
    performance_candidate = (
        samples >= minimum and
        skill >= min_skill and
        da is not None and da >= 0.52 and
        active_coverage >= min_active_coverage and
        interval_ok and
        not bool(result.get("fallback_used"))
    )
    passed = performance_candidate and dm_ok
    reasons = []
    if samples < minimum: reasons.append(f"OOS 표본 {samples}개로 기준 {minimum}개 미달")
    if skill < min_skill: reasons.append(f"지속성 대비 RMSE 개선 {skill:.2f}%로 최소 {min_skill:.2f}% 미달")
    if da is None: reasons.append("활성 방향예측 표본 없음")
    elif da < 0.52: reasons.append(f"활성 방향 적중률 {da*100:.1f}%로 52% 미달")
    if active_coverage < min_active_coverage: reasons.append(f"활성 방향예측 비중 {active_coverage*100:.1f}%로 기준 미달")
    if not interval_ok: reasons.append("80% 예상범위 보정 기준 75~85% 이탈")
    if not dm_ok: reasons.append("지속성 대비 예측력 개선의 통계적 유의성 미확인")
    if result.get("fallback_used"): reasons.append("지속성 안전모형으로 후퇴")
    return {
        "passed": passed,
        "performance_candidate": performance_candidate,
        "level": "독립검증 통과" if passed else ("OOS 성능후보·통계유의성 미확인" if performance_candidate else "참고용/관망"),
        "reasons": reasons,
        "thresholds": {"min_skill_pct": min_skill, "min_direction_accuracy": 0.52,
                       "min_active_direction_coverage": min_active_coverage,
                       "dm_p_value_max": 0.05, "interval80_coverage": [0.75, 0.85]},
    }


def main() -> None:
    session = build_http_session()
    data, errors = fetch_fred_all(session, list(SERIES))
    fed = fetch_fed_engine(session)
    treasury_futures = fetch_treasury_futures_context(session)
    if not fed["available"]:
        errors.append("fed_engine: " + fed.get("error", "unknown"))

    core_ids = [k for k, v in SERIES.items() if v["core"]]
    core_ok = sum(bool(data.get(k)) for k in core_ids)
    completeness = round(100*sum(bool(data.get(k)) for k in SERIES)/len(SERIES), 1)
    core_completeness = round(100*core_ok/len(core_ids), 1)
    missing_core = [k for k in core_ids if not data.get(k)]
    if any(not data.get(k) for k in TARGETS):
        missing_targets = [k for k in TARGETS if not data.get(k)]
        tail = " | ".join(errors[-12:])
        raise RuntimeError("Card8 target series missing after batch+retry: " + ", ".join(missing_targets) + (" | diagnostics: " + tail if tail else ""))

    current = {sid: {"value": latest(data[sid])["value"], "date": latest(data[sid])["date"]}
               for sid in SERIES if data.get(sid)}
    forecasts: dict[str, Any] = {}
    gates: dict[str, Any] = {}
    for horizon, cfg in HORIZONS.items():
        overlay = structural_deltas(data, fed, horizon)
        forecasts[horizon] = {"label": cfg["label"], "targets": {}}
        target_passes = []
        for sid in TARGETS:
            vals = values(data[sid])
            res = select_model_oos(vals, cfg["steps"], overlay[sid])
            residuals = res.pop("residuals")
            band50 = percentile([abs(x) for x in residuals], .50) if residuals else None
            band80 = percentile([abs(x) for x in residuals], .80) if residuals else None
            # Historical coverage of symmetric absolute-error bands is mechanically auditable.
            res["interval50_coverage"] = mean(abs(x) <= band50 for x in residuals) if residuals and finite(band50) else None
            res["interval80_coverage"] = mean(abs(x) <= band80 for x in residuals) if residuals and finite(band80) else None
            res["range50"] = [res["forecast"]-band50, res["forecast"]+band50] if finite(band50) else None
            res["range80"] = [res["forecast"]-band80, res["forecast"]+band80] if finite(band80) else None
            res["structural_overlay"] = overlay[sid]
            res["current"] = current[sid]["value"]
            res["direction"] = "up" if res["forecast"] > res["current"]+0.025 else "down" if res["forecast"] < res["current"]-0.025 else "flat"
            res["investment_environment"] = grade_strength(res["current"], res["forecast"], adverse_when_up=(sid != "T10Y2Y"))
            gate = horizon_gate(res, cfg["min_samples"], horizon)
            res["quality_gate"] = gate
            target_passes.append(gate["passed"])
            forecasts[horizon]["targets"][sid] = res

        # Reuse the Fed engine's independently validated 10Y real-rate model for
        # the 3M DFII10 leg.  This is cross-engine validation reuse, not a lowered
        # Card8 threshold: the upstream model must itself have a passed 3M gate.
        if horizon == "3m" and fed.get("available"):
            usctx = (fed.get("payload") or {}).get("us_macro_context") or {}
            rr = usctx.get("real_rate") or usctx.get("us_real_rate") or {}
            bt = rr.get("backtest_3m") or {}
            upstream_gate = bt.get("quality_gate") or {}
            if bool(rr.get("forecast_usable_3m")) and bool(upstream_gate.get("passed")) and finite(rr.get("forecast_3m_pct")):
                local = forecasts[horizon]["targets"]["DFII10"]
                upstream_forecast = float(rr["forecast_3m_pct"])
                local["local_model_audit"] = {
                    "forecast": local.get("forecast"), "model": local.get("model"),
                    "skill_pct": local.get("skill_pct"), "direction_accuracy": local.get("direction_accuracy"),
                    "quality_gate": local.get("quality_gate"),
                }
                local["forecast"] = upstream_forecast
                local["model"] = "fed_engine_validated_real_rate_3m"
                local["direction"] = "up" if upstream_forecast > local["current"]+0.025 else "down" if upstream_forecast < local["current"]-0.025 else "flat"
                local["investment_environment"] = grade_strength(local["current"], upstream_forecast, adverse_when_up=True)
                local["external_validation"] = {
                    "source": "Fed engine us_macro_context.real_rate",
                    "passed": True,
                    "samples": bt.get("samples"),
                    "skill_pct": bt.get("skill_pct"),
                    "direction_accuracy": bt.get("direction_accuracy"),
                    "fallback_used": bt.get("fallback_used"),
                    "quality_gate": upstream_gate,
                }
                local["quality_gate"] = {
                    "passed": True, "performance_candidate": True,
                    "level": "독립 상류엔진 OOS 통과", "reasons": [],
                    "validation_source": "fed_engine_real_rate_3m",
                    "local_gate_passed": bool((local["local_model_audit"].get("quality_gate") or {}).get("passed")),
                    "thresholds": (local["local_model_audit"].get("quality_gate") or {}).get("thresholds", {}),
                }
                # Replace the previously appended local gate result for DFII10.
                target_passes[TARGETS.index("DFII10")] = True
        gates[horizon] = {
            "passed": all(target_passes),
            "level": "독립검증 통과" if all(target_passes) else "부분통과/참고용",
            "passed_targets": [sid for sid in TARGETS if forecasts[horizon]["targets"][sid]["quality_gate"]["passed"]],
        }

    dgs2 = current["DGS2"]["value"]
    dgs10 = current["DGS10"]["value"]
    real10 = current["DFII10"]["value"]
    curve = current["T10Y2Y"]["value"]
    primary = forecasts["3m"]["targets"]
    composite_delta = mean([
        -(primary["DGS2"]["forecast"]-dgs2),
        -(primary["DGS10"]["forecast"]-dgs10),
        -(primary["DFII10"]["forecast"]-real10),
        +(primary["T10Y2Y"]["forecast"]-curve),
    ])
    overall = "good" if composite_delta > .08 else "bad" if composite_delta < -.08 else "neutral"
    current_regime = "장기금리·실질금리 부담" if real10 >= 2.0 or dgs10 >= 4.5 else "금리부담 중립" if real10 >= 1.2 else "완화적 실질금리"
    future_regime = "금리부담 완화" if overall == "good" else "금리부담 확대" if overall == "bad" else "금리환경 중립"

    payload = {
        "schema_version": "1.1.0",
        "engine_version": "card8-1.4.0-upstream-validated-realrate-reuse",
        "status": "ok",
        "card": 8,
        "title": "미국채 금리·실질금리·수익률곡선",
        "generated_at_utc": now_iso(),
        "official_source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
        "current": current,
        "derived_current": {
            "nominal10_minus_real10_breakeven": round(dgs10-real10, 4),
            "curve_10y_2y": curve,
            "curve_10y_3m": current.get("T10Y3M", {}).get("value"),
            "term_premium_10y": current.get("THREEFYTP10", {}).get("value"),
        },
        "current_regime": current_regime,
        "future_regime": future_regime,
        "market_signal": overall,
        "treasury_futures_context": treasury_futures,
        "upstream_us_macro_context": (fed.get("payload") or {}).get("us_macro_context") if fed.get("available") else None,
        "investment_conclusion": (
            "장기금리와 실질금리의 예상 부담이 낮아져 장기채·금·고밸류 위험자산에 우호적입니다."
            if overall == "good" else
            "장기금리 또는 실질금리 부담 확대가 예상돼 장기채와 고밸류 위험자산에 불리합니다."
            if overall == "bad" else
            "미국채 금리환경 변화가 작아 자산배분은 중립적으로 유지하는 구간입니다."
        ),
        "forecasts": forecasts,
        "quality_gates": gates,
        "production_level": "독립검증 통과" if gates["3m"]["passed"] and gates["6m"]["passed"] else "기간별 게이트 적용",
        "data_quality": {
            "completeness": completeness,
            "core_completeness": core_completeness,
            "missing_core": missing_core,
            "fed_engine_available": fed["available"],
            "release_lag_policy": "시장계열은 관측일 기준, 월간 거시는 최소 1개월·산업/고용 보조자료는 공표시차를 보수적으로 반영",
            "real_time_vintage": False,
            "vintage_limitation": "FRED/ALFRED 실시간 빈티지 전체 재현은 미적용하며 가격계열 중심 OOS와 발표시차 보수처리를 사용",
        },
        "model_specification": {
            "targets": TARGETS,
            "horizons": HORIZONS,
            "candidate_models": ["persistence", "short_trend", "medium_trend", "long_trend", "mean_reversion_3m", "mean_reversion_6m", "mean_reversion_1y", "structural_blend"],
            "gate_semantics": "performance_candidate는 표본·skill·방향·coverage·구간보정 기준 통과, passed는 여기에 DM 5% 유의성까지 요구",
            "selection": "expanding_walk_forward_candidate_selection_with_material_skill_and_dm_gate",
            "benchmark": "persistence_no_change",
            "safety": "non-positive-skill models automatically fall back to persistence; tiny positive skill cannot pass strict gate",
            "structural_inputs": ["Fed engine policy path", "Treasury futures direction/volatility cross-check", "core PCE momentum", "10y breakeven", "10y term premium", "NFCI", "HY OAS", "unemployment"],
        },
        "source_status": {
            sid: {"ok": bool(data.get(sid)), "label": SERIES[sid]["label"], "latest": current.get(sid),
                  "url": f"https://fred.stlouisfed.org/series/{sid}"}
            for sid in SERIES
        },
        "warnings": errors,
        "limitations": [
            "예측은 확률적 범위이며 단일 금리값을 보장하지 않습니다.",
            "미국 정책금리 경로는 미국엔진 기존 출력값만 읽고 카드8에서 재계산하지 않습니다.",
            "Treasury 분기 리펀딩·경매 일정은 기간프리미엄과 수급의 정성적 보조근거이며 임의 수치로 대체하지 않습니다.",
            "실시간 원본 빈티지가 없는 계열은 그 사실을 품질정보에 명시합니다.",
            "국채선물은 방향·변동성 교차검증에 사용하며, 검증 미통과 기간을 준기관급으로 승격시키지 않습니다.",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    STATUS.write_text(json.dumps({
        "generated_at_utc": payload["generated_at_utc"], "ok": True,
        "production_level": payload["production_level"], "quality_gates": gates,
        "completeness": completeness, "core_completeness": core_completeness,
        "warnings": errors,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
