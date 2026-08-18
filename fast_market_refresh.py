from __future__ import annotations
import csv, io, json, math, random, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import requests

ROOT=Path(__file__).resolve().parent
DATA=ROOT/"public"/"data"
CARD12=DATA/"card12.json"
BUNDLE=DATA/"cards_8_12_bundle.json"
STATUS=DATA/"fast_market_refresh_status.json"
SYMBOLS=["DX-Y.NYB","^VIX","ES=F","NQ=F","CL=F","GC=F","HG=F"]
LABELS={"DX-Y.NYB":"DXY","^VIX":"VIX","ES=F":"S&P500 futures","NQ=F":"Nasdaq100 futures",
        "CL=F":"WTI futures","GC=F":"Gold futures","HG=F":"Copper futures"}
FRED={"DGS2":"US 2Y","DGS10":"US 10Y","BAMLH0A0HYM2":"HY OAS"}
TIMEOUT=(3,8)

def now_iso(): return datetime.now(timezone.utc).isoformat()
def read(path):
    try:
        x=json.loads(path.read_text(encoding="utf-8")); return x if isinstance(x,dict) else {}
    except Exception:return {}
def write(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8"); tmp.replace(path)
def num(x):
    try:
        v=float(x); return v if math.isfinite(v) else None
    except Exception:return None
def yahoo(session,symbol):
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    last=None
    for a in range(2):
        try:
            r=session.get(url,params={"range":"1d","interval":"5m","includePrePost":"true","events":"history","_ts":int(time.time())},
                          timeout=TIMEOUT,headers={"Cache-Control":"no-cache","Pragma":"no-cache"})
            if r.status_code==429:
                time.sleep(min(10,1.5*(2**a)+random.random())); continue
            r.raise_for_status()
            node=((((r.json() or {}).get("chart") or {}).get("result") or [None])[0] or {})
            m=node.get("meta") or {}; p=num(m.get("regularMarketPrice")); ts=m.get("regularMarketTime")
            if p is None or ts is None: raise ValueError("price/time unavailable")
            return {"symbol":symbol,"label":LABELS[symbol],"price":p,
                    "market_time_utc":datetime.fromtimestamp(int(ts),tz=timezone.utc).isoformat(),
                    "retrieved_at_utc":now_iso(),"exchange":m.get("exchangeName"),
                    "source":"Yahoo Finance chart metadata"}
        except Exception as e:
            last=e
            if a<1: time.sleep(1+random.random())
    return {"symbol":symbol,"error":f"{type(last).__name__}: {str(last)[:120]}"}

def fred_csv(session,sid):
    url=f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
    r=session.get(url,timeout=TIMEOUT); r.raise_for_status()
    rows=list(csv.DictReader(io.StringIO(r.text)))
    for row in reversed(rows):
        v=num(row.get(sid))
        if v is not None:
            return {"series":sid,"value":v,"date":row.get("DATE"),"source":"FRED fredgraph CSV"}
    raise ValueError("no usable observation")

def update_card12(card,quotes):
    card.setdefault("market_snapshots",{})
    card.setdefault("current",{})
    for sym,q in quotes.items():
        if sym=="^VIX": continue
        if sym not in card["market_snapshots"]: continue
        card["market_snapshots"][sym]={"symbol":sym,"price":q["price"],"market_time_utc":q["market_time_utc"],
            "exchange":q.get("exchange"),"market_state":None,"source":q["source"],
            "retrieved_at_utc":q["retrieved_at_utc"],"refetch_policy":"fast_market_2h"}
        card["current"][sym]={"value":q["price"],"date":q["market_time_utc"][:10]}
    card["fast_market_refresh"]={"version":"V230","updated_at_utc":now_iso(),"model_forecasts_recomputed":False}

def update_card8(card8,fred):
    cur=card8.get("current")
    if not isinstance(cur,dict): return
    for sid,q in fred.items():
        if sid in ("DGS2","DGS10"):
            cur[sid]={"value":q["value"],"date":q["date"]}
    card8["fast_market_refresh"]={"version":"V230","updated_at_utc":now_iso(),"official_daily_series":True}

def parse_dt(v):
    try:
        d=datetime.fromisoformat(str(v or "").replace("Z","+00:00"))
        if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None

def main():
    st=read(STATUS)
    card12=read(CARD12); bundle=read(BUNDLE)
    prev_snaps=card12.get("market_snapshots") if isinstance(card12.get("market_snapshots"),dict) else {}
    s=requests.Session(); s.headers.update({"User-Agent":"global-macro-fast-refresh/1.0"})
    attempted=[yahoo(s,x) for x in SYMBOLS]
    newer={}
    for q in attempted:
        if "price" not in q: continue
        old=prev_snaps.get(q["symbol"]) if isinstance(prev_snaps,dict) else None
        old_dt=parse_dt((old or {}).get("market_time_utc")) if isinstance(old,dict) else None
        new_dt=parse_dt(q.get("market_time_utc"))
        if old_dt is None or (new_dt is not None and new_dt > old_dt):
            newer[q["symbol"]]=q
    fred={}
    today=datetime.now(timezone.utc).date().isoformat()
    if datetime.now(timezone.utc).hour>=23 and st.get("fred_checked_utc_date")!=today:
        for sid in FRED:
            try: fred[sid]=fred_csv(s,sid)
            except Exception as e: fred[sid]={"error":f"{type(e).__name__}: {str(e)[:120]}"}
    fred_new={}
    cards=bundle.get("cards") if isinstance(bundle.get("cards"),dict) else {}
    c8=cards.get("8") if isinstance(cards.get("8"),dict) else {}
    cur8=c8.get("current") if isinstance(c8.get("current"),dict) else {}
    for sid,q in fred.items():
        if "value" not in q: continue
        old=cur8.get(sid) if isinstance(cur8,dict) else None
        if not isinstance(old,dict) or old.get("date") != q.get("date") or num(old.get("value")) != num(q.get("value")):
            fred_new[sid]=q
    if not newer and not fred_new:
        print(json.dumps({"status":"NO_CHANGE","market_network_calls":len(SYMBOLS),
                          "fred_network_calls_this_run":len(fred)}, ensure_ascii=False))
        return
    if card12 and newer:
        update_card12(card12,newer); write(CARD12,card12)
    if isinstance(cards.get("12"),dict) and newer:
        update_card12(cards["12"],newer)
    if fred_new and isinstance(cards.get("8"),dict):
        update_card8(cards["8"],fred_new)
    bundle["fast_market_snapshot"]={"version":"V230","generated_at_utc":now_iso(),
        "quotes":newer,"vix":newer.get("^VIX"),"official_daily":fred_new,"full_models_recomputed":False}
    if bundle: write(BUNDLE,bundle)
    st.update({"schema_version":"1.0","generated_at_utc":now_iso(),"status":"UPDATED",
               "market_symbols_attempted":SYMBOLS,"market_symbols_updated":sorted(newer),
               "market_success_count":sum(1 for q in attempted if "price" in q),
               "market_network_calls":len(SYMBOLS),"fred_network_calls_this_run":len(fred),
               "fred_checked_utc_date":today if fred else st.get("fred_checked_utc_date"),
               "max_market_calls_per_run":7,"model_formulas_changed":False})
    write(STATUS,st)
if __name__=="__main__": main()
