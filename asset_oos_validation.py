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
from urllib.parse import quote

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "public" / "data" / "asset_oos_validation.json"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "baek-asset-oos-validator/1.0"})

ASSETS = {
    "gold": {"ticker": "GC=F", "label": "금"},
    "bond": {"ticker": "TLT", "label": "미국 장기국채"},
    "bitcoin": {"ticker": "BTC-USD", "label": "비트코인"},
    "sp500": {"ticker": "SPY", "label": "S&P 500"},
    "cashShort": {"ticker": "SGOV", "label": "미국 단기국채"},
    "copper": {"ticker": "HG=F", "label": "구리"},
    "silver": {"ticker": "SI=F", "label": "은"},
}
GROUP_TICKERS = {
    "gold": ["GC=F", "SI=F"],
    "bond": ["ZN=F", "ZB=F"],
    "bitcoin": ["BTC=F", "BTC-USD"],
    "sp500": ["ES=F", "NQ=F"],
    "cashShort": ["ZT=F", "ZF=F"],
    "copper": ["HG=F", "CL=F"],
    "silver": ["SI=F", "GC=F", "HG=F"],
}
HORIZONS = {"1m": 21, "3m": 63}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def yahoo_series(ticker: str, years: int = 12) -> dict[str, float]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker, safe='')}"
    params = {"range": f"{years}y", "interval": "1d", "events": "history", "includeAdjustedClose": "true"}
    r = SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
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


def fred_series(series_id: str) -> dict[str, float]:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    r = SESSION.get(url, params={"id": series_id}, timeout=25)
    r.raise_for_status()
    out: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        raw = row.get(series_id)
        if not raw or raw == ".":
            continue
        try:
            out[row["DATE"]] = float(raw)
        except (KeyError, ValueError):
            continue
    if len(out) < 300:
        raise ValueError(f"{series_id}: insufficient FRED history ({len(out)})")
    return out


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
    fred_ids = ["DGS10", "DFII10", "T10Y2Y", "DGS2"]
    fred = []
    for sid in fred_ids:
        fred.append(fred_series(sid))
        time.sleep(0.2)
    assets_out = {}
    for key, spec in ASSETS.items():
        try:
            asset_price = yahoo_series(spec["ticker"])
            group = []
            for ticker in GROUP_TICKERS[key]:
                group.append(yahoo_series(ticker))
                time.sleep(0.2)
            price, card8_x, card12_x = build_features(key, asset_price, fred, group)
            row = {"label": spec["label"], "ticker": spec["ticker"], "horizons": {}}
            for hname, days in HORIZONS.items():
                row["horizons"][hname] = {
                    "card8": rolling_oos(card8_x, price, days),
                    "card12": rolling_oos(card12_x, price, days),
                }
            assets_out[key] = row
        except Exception as exc:  # keep other assets alive
            errors.append(f"{key}: {type(exc).__name__}: {exc}")
            assets_out[key] = {"label": spec["label"], "ticker": spec["ticker"], "available": False, "error": str(exc)}
    payload = {
        "schema_version": "1.0.0", "engine_version": "asset-oos-v1.0",
        "generated_at_utc": now_iso(), "assets": assets_out,
        "weight_policy": {"A": 1.0, "B": 0.75, "C": 0.5, "D": 0.25, "F": 0.0, "unavailable": 0.25},
        "limitations": [
            "이 파일은 카드 8·12 신호가 각 자산 수익률에 전달되는 정도를 검증하며 카드 8·12 원본 계산을 대체하지 않습니다.",
            "무료 지연시세와 공개 금리자료를 사용합니다. 과거 성과가 미래 성과를 보장하지 않습니다.",
        ],
        "errors": errors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUT)
    print(f"wrote {OUT} ({len(assets_out)} assets, {len(errors)} errors)")


if __name__ == "__main__":
    main()
