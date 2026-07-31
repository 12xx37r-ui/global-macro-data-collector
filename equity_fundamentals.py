"""Collect resilient US equity index fundamentals for the Apps Script dashboard.

Runs in GitHub Actions, not at page load. It collects representative constituent
fundamentals from multiple public Yahoo endpoints and writes a compact JSON.
If a fresh run is partial, the last-good values are retained per metric.
"""
from __future__ import annotations

import json
import math
import time
import re
from html import unescape
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "public" / "data" / "equity_fundamentals.json"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
})

UNIVERSES = {
    "sp500": {
        "label": "S&P 500 대표 업종 대형주",
        "symbols": ["AAPL","MSFT","NVDA","AMZN","GOOGL","META","BRK-B","JPM","XOM","UNH","LLY","WMT","PG","HD","AVGO","V","MA","COST","KO","CVX"],
    },
    "nasdaq": {
        "label": "나스닥100 대표 성장주",
        "symbols": ["AAPL","MSFT","NVDA","AMZN","GOOGL","META","AVGO","TSLA","COST","NFLX","AMD","ADBE","CSCO","INTC","QCOM","AMGN","BKNG","PEP","TMUS","TXN"],
    },
}
METRICS = ("forward_pe", "trailing_pe", "price_sales", "eps_growth_pct")


def finite(v: Any) -> float | None:
    if isinstance(v, dict):
        v = v.get("raw", v.get("fmt"))
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def request_json(url: str, params: dict[str, str] | None = None, attempts: int = 3) -> dict:
    err = None
    for i in range(attempts):
        try:
            r = SESSION.get(url, params=params, timeout=(10, 30))
            if r.status_code == 200:
                return r.json()
            err = RuntimeError(f"HTTP {r.status_code}")
        except Exception as exc:  # network resilience
            err = exc
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(str(err or "request failed"))


def yahoo_quote_batch(symbols: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    joined = ",".join(symbols)
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            j = request_json(f"https://{host}/v7/finance/quote", {"symbols": joined})
            for q in (((j or {}).get("quoteResponse") or {}).get("result") or []):
                sym = str(q.get("symbol") or "")
                if not sym:
                    continue
                growth = finite(q.get("earningsGrowth"))
                if growth is not None and abs(growth) <= 2:
                    growth *= 100
                out[sym] = {
                    "forward_pe": finite(q.get("forwardPE")),
                    "trailing_pe": finite(q.get("trailingPE")),
                    "price_sales": finite(q.get("priceToSalesTrailing12Months")),
                    "eps_growth_pct": growth,
                    "source": f"Yahoo quote {host}",
                }
            if out:
                break
        except Exception:
            continue
    return out


def yahoo_summary(symbol: str) -> dict:
    modules = "summaryDetail,defaultKeyStatistics,financialData,earningsTrend"
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            j = request_json(f"https://{host}/v10/finance/quoteSummary/{symbol}", {"modules": modules})
            q = ((((j or {}).get("quoteSummary") or {}).get("result") or [None])[0] or {})
            if not q:
                continue
            sd, dk, fd = q.get("summaryDetail") or {}, q.get("defaultKeyStatistics") or {}, q.get("financialData") or {}
            growth = finite(fd.get("earningsGrowth"))
            if growth is not None and abs(growth) <= 2:
                growth *= 100
            if growth is None:
                trends = (q.get("earningsTrend") or {}).get("trend") or []
                tr = next((x for x in trends if x.get("period") == "+1y"), None) or next((x for x in trends if x.get("period") == "0y"), None)
                if tr:
                    growth = finite(tr.get("growth")) or finite((tr.get("earningsEstimate") or {}).get("growth"))
                    if growth is not None and abs(growth) <= 2:
                        growth *= 100
            return {
                "forward_pe": finite(sd.get("forwardPE")) or finite(dk.get("forwardPE")),
                "trailing_pe": finite(sd.get("trailingPE")) or finite(dk.get("trailingPE")),
                "price_sales": finite(sd.get("priceToSalesTrailing12Months")) or finite(dk.get("priceToSalesTrailing12Months")),
                "eps_growth_pct": growth,
                "source": f"Yahoo summary {host}",
            }
        except Exception:
            continue
    return {}




def finviz_snapshot(symbol: str) -> dict:
    """Independent HTML fallback for constituent fundamentals.

    Finviz exposes the four fields used here in the quote snapshot table. The
    parser is deliberately label-driven so layout changes do not silently map
    a value to the wrong metric.
    """
    url = "https://finviz.com/quote.ashx"
    try:
        r = SESSION.get(url, params={"t": symbol, "p": "d"}, timeout=(10, 30), headers={
            "User-Agent": SESSION.headers.get("User-Agent", "Mozilla/5.0"),
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://finviz.com/",
        })
        if r.status_code != 200:
            return {}
        text = unescape(r.text)
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\s+", " ", clean)
        def field(label: str) -> float | None:
            m = re.search(r"(?:^|\s)" + re.escape(label) + r"\s+(-?\d+(?:\.\d+)?%?|-)\s", clean, re.I)
            if not m or m.group(1) == "-":
                return None
            v = m.group(1)
            pct = v.endswith("%")
            n = finite(v.rstrip("%"))
            return n if n is None or not pct else n
        return {
            "forward_pe": field("Forward P/E"),
            "trailing_pe": field("P/E"),
            "price_sales": field("P/S"),
            "eps_growth_pct": field("EPS next Y"),
            "source": "Finviz quote snapshot",
        }
    except Exception:
        return {}

def merge_metrics(base: dict, extra: dict) -> dict:
    for k in METRICS:
        if finite(base.get(k)) is None and finite(extra.get(k)) is not None:
            base[k] = finite(extra[k])
    if extra.get("source"):
        base.setdefault("sources", []).append(extra["source"])
    return base


def aggregate(rows: dict[str, dict], previous: dict | None) -> dict:
    values: dict[str, float | None] = {}
    counts: dict[str, int] = {}
    for metric in METRICS:
        vals = [finite(r.get(metric)) for r in rows.values()]
        vals = [x for x in vals if x is not None and x > 0]
        values[metric] = round(median(vals), 4) if vals else None
        counts[metric] = len(vals)
    stale_metrics: list[str] = []
    prev_vals = (previous or {}).get("values") or {}
    for metric in METRICS:
        if values[metric] is None and finite(prev_vals.get(metric)) is not None:
            values[metric] = finite(prev_vals[metric])
            stale_metrics.append(metric)
    usable = sum(v is not None for v in values.values())
    return {
        "available": usable >= 3,
        "values": values,
        "metric_counts": counts,
        "sample_count": len([1 for r in rows.values() if any(finite(r.get(m)) is not None for m in METRICS)]),
        "symbols_used": sorted([s for s, r in rows.items() if any(finite(r.get(m)) is not None for m in METRICS)]),
        "stale_metrics": stale_metrics,
        "method": "representative constituent median; Yahoo v7 batch + quoteSummary + independent Finviz snapshot fallback; last-good per-metric retention",
    }


def main() -> None:
    previous = {}
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    all_symbols = sorted({s for u in UNIVERSES.values() for s in u["symbols"]})
    batch = yahoo_quote_batch(all_symbols)
    rows: dict[str, dict] = {s: dict(batch.get(s) or {}) for s in all_symbols}
    # Only request slower summaries for symbols whose batch response lacks at least two metrics.
    for s in all_symbols:
        have = sum(finite(rows[s].get(m)) is not None for m in METRICS)
        if have < 3:
            rows[s] = merge_metrics(rows[s], yahoo_summary(s))
            have = sum(finite(rows[s].get(m)) is not None for m in METRICS)
            if have < 3:
                rows[s] = merge_metrics(rows[s], finviz_snapshot(s))
            time.sleep(0.12)
    indices = {}
    for key, cfg in UNIVERSES.items():
        subset = {s: rows.get(s, {}) for s in cfg["symbols"]}
        agg = aggregate(subset, ((previous.get("indices") or {}).get(key)))
        agg["label"] = cfg["label"]
        indices[key] = agg
    output = {
        "schema_version": "1.0.0",
        "engine_version": "equity-fundamentals-v1.1-yahoo-finviz",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "indices": indices,
        "source_status": {
            "yahoo_batch_symbols": len(batch),
            "total_symbols": len(all_symbols),
            "fresh": all(x.get("available") for x in indices.values()),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT} (sp500={indices['sp500']['sample_count']}, nasdaq={indices['nasdaq']['sample_count']})")


if __name__ == "__main__":
    main()
