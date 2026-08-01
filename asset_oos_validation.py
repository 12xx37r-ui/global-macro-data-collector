"""Daily asset-specific OOS validation for global cards 8 and 12.

The dashboard must not run historical validation at page load. This module runs in
GitHub Actions, writes one compact JSON, and the Apps Script only reads the result.
No current card-8/card-12 calculation is replaced.
"""
from __future__ import annotations

import csv
import io
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
from urllib.parse import quote

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "public" / "data" / "asset_oos_validation.json"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "baek-asset-oos-validator/1.9 (GitHub Actions; Treasury official data)"})

ASSETS = {
    "gold": {"ticker": "GC=F", "label": "금"},
    "bond": {"ticker": "TLT", "label": "미국 장기국채"},
    "bitcoin": {"ticker": "BTC-USD", "label": "비트코인"},
    "sp500": {"ticker": "SPY", "label": "S&P 500"},
    "nasdaq": {"ticker": "QQQ", "label": "나스닥100"},
    "reit": {"ticker": "VNQ", "label": "미국 리츠"},
    "highYield": {"ticker": "HYG", "label": "미국 하이일드 회사채"},
    "oil": {"ticker": "CL=F", "label": "원유"},
    "emerging": {"ticker": "EEM", "label": "신흥국 주식"},
    "investmentGrade": {"ticker": "LQD", "label": "미국 투자등급 회사채"},
    "cashShort": {"ticker": "SGOV", "label": "미국 단기국채"},
    "copper": {"ticker": "HG=F", "label": "구리"},
    "silver": {"ticker": "SI=F", "label": "은"},
}
GROUP_TICKERS = {
    "gold": ["GC=F", "SI=F"],
    "bond": ["ZN=F", "ZB=F"],
    "bitcoin": ["BTC=F", "BTC-USD"],
    "sp500": ["ES=F", "NQ=F"],
    "nasdaq": ["NQ=F", "ES=F"],
    "reit": ["ES=F"],
    "highYield": ["ES=F", "ZN=F"],
    "oil": ["CL=F"],
    "emerging": ["ES=F", "DX-Y.NYB"],
    "investmentGrade": ["ZN=F", "ZB=F", "ES=F"],
    "cashShort": ["ZT=F", "ZF=F"],
    "copper": ["HG=F", "CL=F"],
    "silver": ["SI=F", "GC=F", "HG=F"],
}
# Backward-compatible public name used by tests and downstream tooling.
# CARD12_TICKERS is the canonical alias for the card-12 futures/proxy mapping.
CARD12_TICKERS = GROUP_TICKERS
HORIZONS = {"1m": 21, "3m": 63}
REQUIRED_ASSETS = tuple(ASSETS)
STOOQ_TICKERS = {
    "TLT": "tlt.us", "SPY": "spy.us", "QQQ": "qqq.us", "VNQ": "vnq.us",
    "HYG": "hyg.us", "EEM": "eem.us", "LQD": "lqd.us", "SGOV": "sgov.us",
}



def get_with_retry(url: str, *, params: dict | None = None, attempts: int = 4) -> requests.Response:
    """HTTP GET with bounded exponential backoff for transient public-data outages."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = SESSION.get(url, params=params, timeout=(10, 45))
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"HTTP request failed after {attempts} attempts: {url}: {last_exc}")


def load_previous_payload() -> dict | None:
    try:
        if OUT.exists():
            data = json.loads(OUT.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("assets"), dict):
                return data
    except Exception:
        pass
    return None

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def yahoo_series(ticker: str, years: int = 12) -> dict[str, float]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker, safe='')}"
    params = {"range": f"{years}y", "interval": "1d", "events": "history", "includeAdjustedClose": "true"}
    r = get_with_retry(url, params=params)
    result = r.json()["chart"]["result"][0]
    stamps = result.get("timestamp") or []
    q = (result.get("indicators", {}).get("adjclose") or result.get("indicators", {}).get("quote") or [{}])[0]
    vals = q.get("adjclose") or q.get("close") or []
    out: dict[str, float] = {}
    for ts, value in zip(stamps, vals):
        if value is None:
            continue
        out[datetime.fromtimestamp(ts, timezone.utc).date().isoformat()] = float(value)
    if len(out) < 300:
        raise ValueError(f"{ticker}: insufficient Yahoo history ({len(out)})")
    return out


def stooq_series(ticker: str, years: int = 12) -> dict[str, float]:
    symbol = STOOQ_TICKERS.get(ticker)
    if not symbol:
        raise ValueError(f"{ticker}: no Stooq fallback mapping")
    end = datetime.now(timezone.utc).date()
    start = end.replace(year=max(1990, end.year - years))
    url = "https://stooq.com/q/d/l/"
    params = {"s": symbol, "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d"), "i": "d"}
    r = get_with_retry(url, params=params, attempts=3)
    out: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        day = (row.get("Date") or "").strip()
        raw = (row.get("Close") or "").strip()
        try:
            value = float(raw)
        except ValueError:
            continue
        if day and math.isfinite(value):
            out[day] = value
    if len(out) < 300:
        raise ValueError(f"{ticker}: insufficient Stooq history ({len(out)})")
    return out


def market_series(ticker: str, years: int = 12) -> tuple[dict[str, float], str, list[str]]:
    errors: list[str] = []
    try:
        return yahoo_series(ticker, years), "Yahoo Finance delayed", errors
    except Exception as exc:
        errors.append(f"Yahoo: {type(exc).__name__}: {exc}")
    try:
        return stooq_series(ticker, years), "Stooq daily fallback", errors
    except Exception as exc:
        errors.append(f"Stooq: {type(exc).__name__}: {exc}")
    raise RuntimeError(f"{ticker}: all market history routes failed: {' | '.join(errors)}")


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_treasury_xml(text: str, value_fields: tuple[str, ...]) -> dict[str, float]:
    """Parse Treasury Atom/OData XML without relying on a fixed namespace prefix."""
    root = ET.fromstring(text)
    out: dict[str, float] = {}
    for props in root.iter():
        if _xml_local_name(props.tag) != "properties":
            continue
        values = {_xml_local_name(child.tag): (child.text or "").strip() for child in props}
        raw_date = values.get("NEW_DATE") or values.get("Date") or values.get("record_date")
        if not raw_date:
            continue
        day = raw_date[:10]
        raw_value = next((values.get(name) for name in value_fields if values.get(name) not in (None, "")), None)
        if raw_value is None:
            continue
        try:
            out[day] = float(raw_value)
        except ValueError:
            continue
    return out


def _treasury_year(feed: str, year: int, fields: tuple[str, ...]) -> dict[str, float]:
    """Fetch one small official Treasury XML year, with xml/xmlview endpoint fallback."""
    base = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages"
    errors: list[str] = []
    for endpoint in ("xmlview", "xml"):
        url = f"{base}/{endpoint}"
        try:
            r = get_with_retry(
                url,
                params={"data": feed, "field_tdr_date_value": str(year)},
                attempts=3,
            )
            parsed = _parse_treasury_xml(r.text, fields)
            if parsed:
                return parsed
            errors.append(f"{endpoint}: empty/unknown schema")
        except Exception as exc:
            errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
    raise RuntimeError(f"Treasury {feed} {year} failed: {' | '.join(errors)}")


def treasury_rate_bundle(years: int = 13) -> tuple[list[dict[str, float]], list[str]]:
    """Return DGS10-equivalent, real10, 10y-2y curve, and DGS2-equivalent series.

    Uses only the U.S. Treasury official annual XML feeds. Annual requests are
    fetched concurrently, so one slow year does not block the whole history.
    """
    current_year = datetime.now(timezone.utc).year
    first_year = current_year - years + 1
    jobs: list[tuple[str, int, tuple[str, ...]]] = []
    for year in range(first_year, current_year + 1):
        jobs.append(("nominal", year, ("BC_2YEAR", "BC_2_YEAR", "TWO_YEAR")))
        jobs.append(("nominal10", year, ("BC_10YEAR", "BC_10_YEAR", "TEN_YEAR")))
        jobs.append(("real10", year, ("TC_10YEAR", "TC_10_YEAR", "BC_10YEAR")))

    results: dict[tuple[str, int], dict[str, float]] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {}
        for kind, year, fields in jobs:
            feed = "daily_treasury_real_yield_curve" if kind == "real10" else "daily_treasury_yield_curve"
            futures[pool.submit(_treasury_year, feed, year, fields)] = (kind, year)
        for fut in as_completed(futures):
            kind, year = futures[fut]
            try:
                results[(kind, year)] = fut.result()
            except Exception as exc:
                errors.append(f"{kind} {year}: {type(exc).__name__}: {exc}")

    dgs2: dict[str, float] = {}
    dgs10: dict[str, float] = {}
    real10: dict[str, float] = {}
    for year in range(first_year, current_year + 1):
        dgs2.update(results.get(("nominal", year), {}))
        dgs10.update(results.get(("nominal10", year), {}))
        real10.update(results.get(("real10", year), {}))

    common_curve_dates = set(dgs10) & set(dgs2)
    curve = {d: dgs10[d] - dgs2[d] for d in common_curve_dates}
    minimum = 300
    counts = {"DGS10_equivalent": len(dgs10), "DFII10_equivalent": len(real10), "T10Y2Y_derived": len(curve), "DGS2_equivalent": len(dgs2)}
    deficient = [f"{k}={v}" for k, v in counts.items() if v < minimum]
    if deficient:
        raise ValueError("insufficient Treasury history: " + ", ".join(deficient) + ("; fetch errors: " + " || ".join(errors[:6]) if errors else ""))
    return [dgs10, real10, curve, dgs2], errors


def aligned_matrix(target: dict[str, float], features: list[dict[str, float]]) -> tuple[list[str], np.ndarray, np.ndarray]:
    dates = sorted(target)
    feature_dates = [sorted(s) for s in features]
    idx = [0] * len(features)
    last = [None] * len(features)
    rows, prices, kept = [], [], []
    for d in dates:
        for j, sdates in enumerate(feature_dates):
            while idx[j] < len(sdates) and sdates[idx[j]] <= d:
                last[j] = features[j][sdates[idx[j]]]
                idx[j] += 1
        if all(v is not None and math.isfinite(float(v)) for v in last):
            rows.append([float(v) for v in last])
            prices.append(float(target[d]))
            kept.append(d)
    return kept, np.asarray(rows, dtype=float), np.asarray(prices, dtype=float)


def lag_change(a: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    if len(a) > n:
        out[n:] = a[n:] - a[:-n]
    return out


def pct_return(a: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    if len(a) > n:
        out[n:] = np.log(np.maximum(a[n:], 1e-12) / np.maximum(a[:-n], 1e-12))
    return out


def standardize_train_apply(x_train: np.ndarray, x_test: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mu = np.nanmean(x_train, axis=0)
    sd = np.nanstd(x_train, axis=0)
    sd[~np.isfinite(sd) | (sd < 1e-9)] = 1.0
    return (x_train - mu) / sd, (x_test - mu) / sd


def rolling_oos(x: np.ndarray, price: np.ndarray, horizon: int) -> dict:
    y = np.full(len(price), np.nan)
    y[:-horizon] = np.log(np.maximum(price[horizon:], 1e-12) / np.maximum(price[:-horizon], 1e-12))
    valid = np.all(np.isfinite(x), axis=1) & np.isfinite(y)
    candidates = np.where(valid)[0]
    min_train = 252 * 3
    preds, actual = [], []
    # Monthly anchors reduce overlapping-target distortion and runtime.
    for i in candidates:
        if i < min_train or i % 21 != 0:
            continue
        train_idx = candidates[candidates < i - horizon]
        if len(train_idx) < min_train:
            continue
        train_idx = train_idx[-252 * 8 :]
        xt, xs = standardize_train_apply(x[train_idx], x[i : i + 1])
        yt = y[train_idx]
        design = np.column_stack([np.ones(len(xt)), xt])
        ridge = np.eye(design.shape[1]) * 2.0
        ridge[0, 0] = 0.0
        beta = np.linalg.solve(design.T @ design + ridge, design.T @ yt)
        pred = float(np.r_[1.0, xs[0]] @ beta)
        preds.append(pred)
        actual.append(float(y[i]))
    p, a = np.asarray(preds), np.asarray(actual)
    n = len(a)
    if n < 36:
        return {"status": "insufficient", "samples": n, "grade": "미확인", "weight_multiplier": 0.25,
                "reasons": ["독립 OOS 표본 36개 미만"]}
    da = float(np.mean(np.sign(p) == np.sign(a)))
    corr = float(np.corrcoef(p, a)[0, 1]) if np.std(p) > 1e-12 and np.std(a) > 1e-12 else 0.0
    rmse = float(np.sqrt(np.mean((p - a) ** 2)))
    baseline = float(np.sqrt(np.mean(a ** 2)))
    skill = (1.0 - rmse / baseline) * 100.0 if baseline > 1e-12 else 0.0
    if n >= 60 and da >= 0.58 and corr >= 0.15 and skill > 0:
        grade, mult, status = "A", 1.0, "strong_pass"
    elif n >= 48 and da >= 0.54 and corr >= 0.08 and skill > -2:
        grade, mult, status = "B", 0.75, "pass"
    elif da >= 0.51 or corr > 0.03:
        grade, mult, status = "C", 0.50, "limited_pass"
    elif da >= 0.48 and corr > -0.05:
        grade, mult, status = "D", 0.25, "weak"
    else:
        grade, mult, status = "F", 0.0, "fail"
    return {
        "status": status, "samples": n, "grade": grade, "weight_multiplier": mult,
        "direction_accuracy": round(da, 4), "correlation": round(corr, 4),
        "rmse": round(rmse, 6), "zero_baseline_rmse": round(baseline, 6),
        "skill_pct": round(skill, 3),
        "method": "fixed-feature expanding/rolling ridge OOS; monthly anchors; no future data in training",
    }


def build_features(asset_key: str, asset_price: dict[str, float], fred: list[dict[str, float]], group: list[dict[str, float]]):
    dates, macro_raw, price = aligned_matrix(asset_price, fred)
    dgs10, real10, curve, dgs2 = macro_raw.T
    card8_x = np.column_stack([
        lag_change(dgs10, 21), lag_change(dgs10, 63),
        lag_change(real10, 21), lag_change(real10, 63),
        lag_change(curve, 21), lag_change(dgs2, 21),
    ])
    # Align futures-group data independently to the same asset dates.
    _, group_raw, group_price = aligned_matrix(asset_price, group)
    # aligned_matrix on the same target normally yields the same number of rows; trim defensively.
    n = min(len(price), len(group_price))
    price = price[-n:]
    card8_x = card8_x[-n:]
    group_raw = group_raw[-n:]
    f21 = np.column_stack([pct_return(group_raw[:, j], 21) for j in range(group_raw.shape[1])])
    f63 = np.column_stack([pct_return(group_raw[:, j], 63) for j in range(group_raw.shape[1])])
    card12_x = np.column_stack([f21, f63])
    return price, card8_x, card12_x


def main() -> None:
    errors: list[str] = []
    previous = load_previous_payload()
    previous_assets = (previous or {}).get("assets", {})

    rates: list[dict[str, float]] = []
    treasury_ready = True
    try:
        rates, treasury_errors = treasury_rate_bundle(years=13)
        errors.extend([f"Treasury partial: {msg}" for msg in treasury_errors])
    except Exception as exc:
        treasury_ready = False
        errors.append(f"Treasury rates: {type(exc).__name__}: {exc}")

    assets_out: dict[str, dict] = {}
    for key, spec in ASSETS.items():
        if not treasury_ready:
            prior = previous_assets.get(key)
            if isinstance(prior, dict) and prior.get("horizons"):
                reused = dict(prior)
                reused["stale"] = True
                reused["stale_reason"] = "미국 재무부 공식 금리자료 일시 장애로 마지막 정상 OOS 결과 재사용"
                assets_out[key] = reused
            else:
                assets_out[key] = {
                    "label": spec["label"], "ticker": spec["ticker"],
                    "available": False, "weight_multiplier": 0.25,
                    "error": "미국 재무부 공식 금리자료 일시 장애로 신규 검증 불가; 임시 25% 가중치",
                }
            continue
        try:
            asset_price, asset_source, asset_source_errors = market_series(spec["ticker"])
            group = []
            group_sources: list[str] = []
            group_errors: list[str] = []
            for ticker in GROUP_TICKERS[key]:
                series, source, source_errors = market_series(ticker)
                group.append(series)
                group_sources.append(f"{ticker}:{source}")
                group_errors.extend([f"{ticker}:{msg}" for msg in source_errors])
                time.sleep(0.2)
            price, card8_x, card12_x = build_features(key, asset_price, rates, group)
            row = {
                "label": spec["label"], "ticker": spec["ticker"], "horizons": {}, "stale": False,
                "source_status": {
                    "asset_history": asset_source, "group_histories": group_sources,
                    "fallback_diagnostics": asset_source_errors + group_errors,
                },
            }
            for hname, days in HORIZONS.items():
                row["horizons"][hname] = {
                    "card8": rolling_oos(card8_x, price, days),
                    "card12": rolling_oos(card12_x, price, days),
                }
            assets_out[key] = row
        except Exception as exc:  # keep other assets alive
            errors.append(f"{key}: {type(exc).__name__}: {exc}")
            prior = previous_assets.get(key)
            if isinstance(prior, dict) and prior.get("horizons"):
                reused = dict(prior)
                reused["stale"] = True
                reused["stale_reason"] = f"신규 수집 실패로 마지막 정상값 재사용: {type(exc).__name__}"
                assets_out[key] = reused
            else:
                assets_out[key] = {
                    "label": spec["label"], "ticker": spec["ticker"],
                    "available": False, "weight_multiplier": 0.25, "error": str(exc),
                }
    usable_assets = [
        key for key in REQUIRED_ASSETS
        if isinstance(assets_out.get(key), dict)
        and isinstance(assets_out[key].get("horizons"), dict)
        and all(h in assets_out[key]["horizons"] for h in HORIZONS)
    ]
    missing_assets = [key for key in REQUIRED_ASSETS if key not in usable_assets]
    payload = {
        "schema_version": "1.9.0", "engine_version": "asset-oos-v1.9-complete-13-assets-multisource",
        "generated_at_utc": now_iso(), "assets": assets_out,
        "source_status": {"treasury_ready": treasury_ready, "treasury_window_years": 13, "fred_dependency": False, "used_previous_results": any(bool(v.get("stale")) for v in assets_out.values())},
        "coverage": {
            "required_assets": len(REQUIRED_ASSETS), "usable_assets": len(usable_assets),
            "missing_assets": missing_assets, "complete": not missing_assets,
        },
        "weight_policy": {"A": 1.0, "B": 0.75, "C": 0.5, "D": 0.25, "F": 0.0, "unavailable": 0.25},
        "limitations": [
            "이 파일은 카드 8·12 신호가 각 자산 수익률에 전달되는 정도를 검증하며 카드 8·12 원본 계산을 대체하지 않습니다.",
            "무료 지연시세와 미국 재무부 공식 명목·실질 금리자료를 사용합니다. 과거 성과가 미래 성과를 보장하지 않습니다.",
            "공개자료 일시 장애 시 마지막 정상 검증값을 재사용하고 stale 상태를 명시합니다.",
        ],
        "errors": errors,
    }
    if missing_assets:
        payload["errors"].append("required asset validation rows missing: " + ", ".join(missing_assets))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUT)
    print(f"wrote {OUT} ({len(assets_out)} assets, {len(errors)} errors)")


if __name__ == "__main__":
    main()
