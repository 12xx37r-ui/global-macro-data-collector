from __future__ import annotations

import json, math, os, statistics, io, csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import xlrd
from openpyxl import load_workbook

from treasury_card8 import (
    build_http_session, fetch_fred_all, finite, latest, values, change,
    annualized_index_change, clamp, mean, percentile, rmse, dm_test_squared_errors,
)

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / 'public' / 'data'

# Card 9: employment and consumption. Mostly official US + OECD global proxies.
CARD9_SERIES = {
    'UNRATE': {'label':'미국 실업률','core':True,'adverse_up':True},
    'PAYEMS': {'label':'미국 비농업고용','core':True,'adverse_up':False},
    'ICSA': {'label':'미국 신규실업수당','core':True,'adverse_up':True},
    'CCSA': {'label':'미국 계속실업수당','core':False,'adverse_up':True},
    'JTSJOL': {'label':'미국 구인건수','core':False,'adverse_up':False},
    'CES0500000003': {'label':'미국 시간당임금','core':False,'adverse_up':False},
    'RSAFS': {'label':'미국 소매판매','core':True,'adverse_up':False},
    'PCE': {'label':'미국 개인소비지출','core':True,'adverse_up':False},
    'UMCSENT': {'label':'미시간 소비심리','core':False,'adverse_up':False},
    'TOTALSL': {'label':'미국 소비자신용','core':False,'adverse_up':False},
    'PSAVERT': {'label':'미국 개인저축률','core':False,'adverse_up':False},
    'OECDSLRTTO01IXOBSAM': {'label':'OECD 소매판매량','core':False,'adverse_up':False,'max_age_days':180},
}

# Card 10: commodities, energy, supply chain.
CARD10_SERIES = {
    'PALLFNFINDEXM': {'label':'IMF 글로벌 원자재 종합지수','core':True,'adverse_up':True},
    'PNRGINDEXM': {'label':'IMF 글로벌 에너지지수','core':True,'adverse_up':True},
    'DCOILWTICO': {'label':'WTI 유가','core':True,'adverse_up':True},
    'DCOILBRENTEU': {'label':'브렌트유','core':False,'adverse_up':True},
    'DHHNGSP': {'label':'미국 천연가스','core':False,'adverse_up':True},
    'PCOPPUSDM': {'label':'글로벌 구리가격','core':True,'adverse_up':False},
    'PALUMUSDM': {'label':'글로벌 알루미늄가격','core':False,'adverse_up':False},
    'PIORECRUSDM': {'label':'글로벌 철광석가격','core':False,'adverse_up':False},
    'PWHEAMTUSDM': {'label':'글로벌 밀가격','core':False,'adverse_up':True},
    'GSCPI': {'label':'뉴욕연은 글로벌 공급망압력','core':True,'adverse_up':True,'direct':True,'max_age_days':120},
    'PPIFIS': {'label':'미국 최종수요 생산자물가','core':True,'adverse_up':True},
}

# Card 12: futures market. Yahoo continuous contracts are delayed/free market inputs.
YAHOO_SYMBOLS = {
    'ES=F':'S&P500 선물','NQ=F':'나스닥100 선물','RTY=F':'러셀2000 선물',
    'ZT=F':'미국 2년 국채선물','ZF=F':'미국 5년 국채선물','ZN=F':'미국 10년 국채선물','ZB=F':'미국 30년 국채선물',
    'CL=F':'WTI 원유선물','GC=F':'금선물','HG=F':'구리선물','DX-Y.NYB':'미국 달러인덱스(현물 대체신호)','BTC=F':'비트코인선물',
}

HORIZONS = {'5d':5,'1m':21,'3m':63,'6m':126,'12m':252}


def now_iso(): return datetime.now(timezone.utc).isoformat()

GSCPI_XLSX_URL = "https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx"
GSCPI_FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GSCPI"
GSCPI_CACHE_PATH = OUT_DIR / "cache" / "gscpi_official.json"


XLSX_MAGIC = b"PK\x03\x04"
XLS_MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"


def _detect_excel_format(r: requests.Response) -> str:
    content_type = (r.headers.get("content-type") or "").lower()
    body = r.content or b""
    if len(body) < 1024:
        raise ValueError(f"download too small ({len(body)} bytes)")
    if body.startswith(XLSX_MAGIC):
        return "xlsx"
    if body.startswith(XLS_MAGIC):
        return "xls"
    preview = body[:80].decode("utf-8", errors="ignore").replace("\n", " ")
    raise ValueError(f"not a supported Excel response; content-type={content_type}; preview={preview!r}")


def _validate_xlsx_response(r: requests.Response) -> bytes:
    """Backward-compatible integrity-test helper.

    Accepts only a genuine XLSX/ZIP response and returns its bytes.
    The production GSCPI collector uses ``_detect_excel_format`` so it can
    also accept the New York Fed's legacy XLS binary response.
    """
    excel_format = _detect_excel_format(r)
    if excel_format != "xlsx":
        raise ValueError(f"expected XLSX response, received {excel_format.upper()}")
    return r.content


def _coerce_date_text(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "date"):
        try:
            return value.date().isoformat()
        except Exception:
            pass
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y", "%b-%Y", "%b %Y", "%Y-%m", "%Y/%m"):
        try:
            return datetime.strptime(text, fmt).date().replace(day=1).isoformat() if "%d" not in fmt else datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _finalize_gscpi_rows(out: list[dict[str, Any]], source_label: str) -> list[dict[str, Any]]:
    dedup = {x["date"]: x for x in out if x.get("date") and finite(x.get("value"))}
    result = [dedup[k] for k in sorted(dedup)]
    if len(result) < 36:
        raise ValueError(f"{source_label} contained only {len(result)} usable observations")
    return result


def _parse_gscpi_xlsx(content: bytes) -> list[dict[str, Any]]:
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    out: list[dict[str, Any]] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            if not row or len(row) < 2:
                continue
            date_text = next((_coerce_date_text(x) for x in row if _coerce_date_text(x)), None)
            numeric_values = [x for x in row if finite(x)]
            if date_text is None or not numeric_values:
                continue
            out.append({"date": date_text, "value": float(numeric_values[-1])})
    return _finalize_gscpi_rows(out, "official XLSX workbook")


def _parse_gscpi_xls(content: bytes) -> list[dict[str, Any]]:
    book = xlrd.open_workbook(file_contents=content, on_demand=True)
    out: list[dict[str, Any]] = []
    try:
        for sheet in book.sheets():
            for row_index in range(sheet.nrows):
                cells = sheet.row(row_index)
                if len(cells) < 2:
                    continue
                date_text = None
                numeric_values: list[float] = []
                for cell in cells:
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            date_text = xlrd.xldate_as_datetime(cell.value, book.datemode).date().isoformat()
                        except Exception:
                            pass
                    elif cell.ctype == xlrd.XL_CELL_NUMBER and finite(cell.value):
                        numeric_values.append(float(cell.value))
                    elif cell.ctype == xlrd.XL_CELL_TEXT and date_text is None:
                        date_text = _coerce_date_text(cell.value)
                if date_text is None or not numeric_values:
                    continue
                out.append({"date": date_text, "value": numeric_values[-1]})
    finally:
        book.release_resources()
    return _finalize_gscpi_rows(out, "official XLS workbook")


def _parse_gscpi_workbook(content: bytes, excel_format: str) -> list[dict[str, Any]]:
    if excel_format == "xlsx":
        return _parse_gscpi_xlsx(content)
    if excel_format == "xls":
        return _parse_gscpi_xls(content)
    raise ValueError(f"unsupported Excel format: {excel_format}")


def _fetch_gscpi_fred_csv(session: requests.Session) -> list[dict[str, Any]]:
    r = session.get(GSCPI_FRED_CSV_URL, timeout=(15, 60), headers={"Accept": "text/csv,*/*"})
    r.raise_for_status()
    text = r.text
    if "DATE" not in text.upper() or "GSCPI" not in text.upper():
        raise ValueError("FRED fallback did not return a GSCPI CSV")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        value = row.get("GSCPI")
        if value not in (None, "", ".") and finite(value):
            out.append({"date": row.get("DATE"), "value": float(value)})
    if len(out) < 36:
        raise ValueError(f"FRED fallback contained only {len(out)} observations")
    return out


def _write_gscpi_cache(rows: list[dict[str, Any]]) -> None:
    GSCPI_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GSCPI_CACHE_PATH.write_text(json.dumps({"saved_at_utc": now_iso(), "rows": rows}, ensure_ascii=False), encoding="utf-8")


def _read_gscpi_cache() -> list[dict[str, Any]]:
    try:
        payload = json.loads(GSCPI_CACHE_PATH.read_text(encoding="utf-8"))
        rows = payload.get("rows") or []
        return rows if len(rows) >= 36 else []
    except Exception:
        return []


def fetch_gscpi_official(session: requests.Session) -> tuple[list[dict[str, Any]], str]:
    errors = []
    try:
        r = session.get(
            GSCPI_XLSX_URL,
            timeout=(15, 60),
            allow_redirects=True,
            headers={
                "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/octet-stream;q=0.9,*/*;q=0.5",
                "Referer": "https://www.newyorkfed.org/research/policy/gscpi",
            },
        )
        r.raise_for_status()
        excel_format = _detect_excel_format(r)
        rows = _parse_gscpi_workbook(r.content, excel_format)
        _write_gscpi_cache(rows)
        return rows, f"New York Fed official {excel_format.upper()} workbook"
    except Exception as exc:
        errors.append(f"official workbook: {exc}")
    try:
        rows = _fetch_gscpi_fred_csv(session)
        _write_gscpi_cache(rows)
        return rows, "FRED CSV fallback"
    except Exception as exc:
        errors.append(f"FRED CSV fallback: {exc}")
    cached = _read_gscpi_cache()
    if cached:
        return cached, "local cache from last successful official collection"
    raise RuntimeError("; ".join(errors))


def fetch_card10_data(session: requests.Session) -> tuple[dict[str, list[dict[str, Any]]], list[str], dict[str, str]]:
    fred_ids = [sid for sid, meta in CARD10_SERIES.items() if not meta.get("direct")]
    data, errors = fetch_fred_all(session, fred_ids)
    source_overrides: dict[str, str] = {}
    try:
        data["GSCPI"], source_overrides["GSCPI"] = fetch_gscpi_official(session)
    except Exception as exc:
        data["GSCPI"] = []
        source_overrides["GSCPI"] = "New York Fed official workbook (collection failed)"
        errors.append(f"GSCPI collection failed after official, fallback and cache attempts: {exc}")
    return data, errors, source_overrides

def aligned_group_index(histories: dict[str, list[dict[str, Any]]], symbols: list[str]) -> list[dict[str, Any]]:
    maps = {}
    for sym in symbols:
        rows = histories.get(sym, [])
        if len(rows) < 260:
            continue
        vals = [float(x["value"]) for x in rows]
        rets = {}
        for i in range(63, len(rows)):
            r21 = (vals[i] / vals[i-21] - 1) * 100 if vals[i-21] else 0.0
            r63 = (vals[i] / vals[i-63] - 1) * 100 if vals[i-63] else 0.0
            daily = [(vals[j] / vals[j-1] - 1) * 100 for j in range(max(1, i-62), i+1) if vals[j-1]]
            vol = statistics.stdev(daily) if len(daily) > 2 else 1.0
            rets[rows[i]["date"]] = clamp((0.6*r21 + 0.4*r63) / (vol*3 + 1), -3, 3)
        maps[sym] = rets
    dates = sorted(set().union(*[set(m) for m in maps.values()])) if maps else []
    out = []
    for d in dates:
        xs = [m[d] for m in maps.values() if d in m]
        if len(xs) >= max(1, len(maps)//2):
            out.append({"date": d, "value": 50 + 10*clamp(mean(xs), -3, 3)})
    return out

def build_futures_forecasts(histories: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    groups = {
        "equity": ["ES=F", "NQ=F", "RTY=F"],
        "rates": ["ZT=F", "ZF=F", "ZN=F", "ZB=F"],
        "commodities": ["CL=F", "GC=F", "HG=F"],
        "crypto": ["BTC=F"],
    }
    group_forecasts = {}
    for group, syms in groups.items():
        idx = aligned_group_index(histories, syms)
        vals = values(idx)
        if len(vals) < 320:
            group_forecasts[group] = {"quality_gate": {"passed": False, "level": "참고용"}, "reason": "장기 검증표본 부족"}
            continue
        horizon_out = {}
        for key, steps, minimum in (("5d", 5, 180), ("1m", 21, 150), ("3m", 63, 100)):
            horizon_out[key] = walk_forward(vals, steps, minimum)
        group_forecasts[group] = {"current_index": vals[-1], "forecasts": horizon_out}
    passed = []
    for g, obj in group_forecasts.items():
        for h, fc in obj.get("forecasts", {}).items():
            if fc.get("quality_gate", {}).get("passed"):
                passed.append(f"{g}:{h}")
    return {"groups": group_forecasts, "passed_horizons": passed, "quality_gate": {"passed": bool(passed), "level": "기간별 검증 통과" if passed else "현재신호 중심"}}

def pctchg(vals:list[float], n:int)->float|None:
    if len(vals)<=n or vals[-1-n]==0: return None
    return (vals[-1]/vals[-1-n]-1)*100

def zscore(vals:list[float], window:int=120)->float:
    if len(vals)<10: return 0.0
    x=vals[-window:]
    m=statistics.mean(x); s=statistics.stdev(x) if len(x)>1 else 1.0
    return 0.0 if s==0 else (vals[-1]-m)/s

def monthly_last(rows:list[dict[str,Any]])->list[dict[str,Any]]:
    by={}
    for r in rows:
        if finite(r.get('value')) and r.get('date'): by[str(r['date'])[:7]]=r
    return [by[k] for k in sorted(by)]

def composite_history(data:dict[str,list[dict[str,Any]]], spec:dict[str,dict[str,Any]], mode:str)->list[dict[str,Any]]:
    # Monthly aligned rolling-z composite. Inputs are transformed to growth/momentum first.
    series_map={}
    for sid,meta in spec.items():
        rows=monthly_last(data.get(sid,[]))
        if len(rows)<36: continue
        vals=[float(x['value']) for x in rows]
        transformed=[]
        for i,r in enumerate(rows):
            if mode=='employment':
                if sid in ('UNRATE','ICSA','CCSA','PSAVERT'):
                    v=-(vals[i]-vals[max(0,i-3)])
                elif sid in ('PAYEMS','JTSJOL','RSAFS','PCE','TOTALSL','OECDSLRTTO01IXOBSAM'):
                    v=(vals[i]/vals[max(0,i-3)]-1)*100 if vals[max(0,i-3)] else 0
                else:
                    v=(vals[i]/vals[max(0,i-3)]-1)*100 if vals[max(0,i-3)] else 0
            else:
                # Pressure: price/supply pressure up is adverse; copper/industrial metals retain growth signal.
                if sid in ('PCOPPUSDM','PALUMUSDM','PIORECRUSDM'):
                    v=(vals[i]/vals[max(0,i-3)]-1)*100 if vals[max(0,i-3)] else 0
                else:
                    v=-((vals[i]/vals[max(0,i-3)]-1)*100 if vals[max(0,i-3)] else 0)
            transformed.append(v)
        zs=[]
        for i,v in enumerate(transformed):
            w=transformed[max(0,i-60):i+1]
            m=statistics.mean(w); s=statistics.stdev(w) if len(w)>2 else 1
            zs.append(0 if s==0 else (v-m)/s)
        series_map[sid]={rows[i]['date'][:7]:zs[i] for i in range(len(rows))}
    dates=sorted(set().union(*[set(v) for v in series_map.values()])) if series_map else []
    out=[]
    for d in dates:
        vals=[]; weights=[]
        for sid,m in series_map.items():
            if d in m:
                vals.append(m[d]); weights.append(1.5 if spec[sid].get('core') else 1.0)
        if len(vals)>=3:
            score=sum(v*w for v,w in zip(vals,weights))/sum(weights)
            out.append({'date':d+'-01','value':50+clamp(score,-3,3)*10})
    return out

def candidates(train:list[float], h:int)->dict[str,float]:
    last=train[-1]
    def d(k): return (last-train[-1-k])/k if len(train)>k else 0
    m12=mean(train[-12:]); m24=mean(train[-24:]);
    return {
        'persistence':last,
        'short_trend':last+clamp(.65*d(1)+.35*d(3),-1.5,1.5)*h,
        'medium_trend':last+clamp(.25*d(1)+.45*d(3)+.30*d(6),-1.0,1.0)*h,
        'mean_reversion_12m':last+clamp((m12-last)*.18,-1.2,1.2)*min(h,6),
        'mean_reversion_24m':last+clamp((m24-last)*.12,-1.0,1.0)*min(h,8),
    }

def walk_forward(vals:list[float], h:int, min_samples:int=60)->dict[str,Any]:
    first=max(36,len(vals)-180-h)
    if len(vals)<first+h+15:
        return {'forecast':vals[-1],'model':'persistence','samples':0,'skill_pct':0,'direction_accuracy':None,'fallback_used':True,'range80':None}
    errs={}; hits={}; cases={}; base_err=[]
    for o in range(first,len(vals)-h):
        tr=vals[:o+1]; actual=vals[o+h]; base=tr[-1]; base_err.append(base-actual)
        for name,p in candidates(tr,h).items():
            errs.setdefault(name,[]).append(p-actual)
            if abs(actual-base)>=1 and abs(p-base)>=.5:
                cases[name]=cases.get(name,0)+1
                hits.setdefault(name,[]).append(int((p-base)*(actual-base)>0))
    br=rmse(base_err); best=min(errs,key=lambda k:rmse(errs[k])); mr=rmse(errs[best]); skill=(1-mr/br)*100 if br else 0
    fallback=skill<=0
    if fallback: best='persistence'; mr=br; skill=0
    final=candidates(vals,h)[best]
    res=errs.get(best,base_err); b80=percentile([abs(x) for x in res],.8)
    da=mean(hits.get(best,[])) if hits.get(best) else None
    dm=dm_test_squared_errors(res,base_err)
    passed=len(res)>=min_samples and skill>=1.5 and da is not None and da>=.53 and dm.get('significant_10pct') and not fallback
    return {'forecast':final,'model':best,'samples':len(res),'rmse':mr,'baseline_rmse':br,'skill_pct':skill,
            'direction_accuracy':da,'fallback_used':fallback,'range80':[final-b80,final+b80] if finite(b80) else None,
            'quality_gate':{'passed':passed,'level':'준기관급' if passed else '참고용','dm_test':dm}}

def build_card9(session:requests.Session)->dict[str,Any]:
    data,errors=fetch_fred_all(session,list(CARD9_SERIES))
    hist=composite_history(data,CARD9_SERIES,'employment')
    vals=values(hist)
    if len(vals)<60: raise RuntimeError('Card9 composite history insufficient')
    forecasts={}
    for key,months in [('1m',1),('3m',3),('6m',6),('12m',12)]:
        forecasts[key]=walk_forward(vals,months,min_samples=60 if months<=6 else 45)
    current=vals[-1]
    f3=forecasts['3m']['forecast']; delta=f3-current
    signal='good' if delta>.8 else 'bad' if delta<-.8 else 'neutral'
    return {
        'schema_version':'1.0','card':9,'title':'글로벌 고용·소비 경기','generated_at_utc':now_iso(),
        'current':current,'current_date':hist[-1]['date'],'forecast_3m':f3,'forecast_6m':forecasts['6m']['forecast'],
        'forecast_range80_3m':forecasts['3m']['range80'],'future_direction':'up' if delta>.4 else 'down' if delta<-.4 else 'flat',
        'market_signal':signal,'current_regime':'고용·소비 강함' if current>=55 else '고용·소비 약함' if current<45 else '고용·소비 보통',
        'future_regime':'회복' if signal=='good' else '둔화' if signal=='bad' else '보합',
        'investment_conclusion':'고용과 소비의 향후 방향을 경기민감주·내수·신용위험 판단에 반영합니다.',
        'forecasts':forecasts,'data_quality':quality(data,CARD9_SERIES,errors),
        'source_status':source_status(data,CARD9_SERIES),
        'model_specification':{'selection':'expanding walk-forward','benchmark':'persistence','inputs':list(CARD9_SERIES)},
    }

def build_card10(session:requests.Session)->dict[str,Any]:
    data,errors,source_overrides=fetch_card10_data(session)
    favorable_hist=composite_history(data,CARD10_SERIES,'commodity')
    # Public card semantics: higher value means greater commodity/energy/supply-chain pressure.
    hist=[{'date':x['date'],'value':100-float(x['value'])} for x in favorable_hist]
    vals=values(hist)
    if len(vals)<60: raise RuntimeError('Card10 composite history insufficient')
    forecasts={}
    for key,months in [('1m',1),('3m',3),('6m',6),('12m',12)]: forecasts[key]=walk_forward(vals,months,60 if months<=6 else 45)
    current=vals[-1]; f3=forecasts['3m']['forecast']; delta=f3-current
    # Pressure index semantics: a rise is adverse, a decline is favorable.
    signal='bad' if delta>.8 else 'good' if delta<-.8 else 'neutral'
    return {
        'schema_version':'1.0','card':10,'title':'원자재·에너지·공급망 압력','generated_at_utc':now_iso(),
        'current':current,'current_date':hist[-1]['date'],'forecast_3m':f3,'forecast_6m':forecasts['6m']['forecast'],
        'forecast_range80_3m':forecasts['3m']['range80'],'future_direction':'up' if delta>.4 else 'down' if delta<-.4 else 'flat',
        'market_signal':signal,'current_regime':'공급·원가 부담' if current>=55 else '공급·원가 우호' if current<45 else '중립',
        'future_regime':'압력완화' if signal=='good' else '압력확대' if signal=='bad' else '보합',
        'investment_conclusion':'원가압력과 산업수요를 함께 봐 인플레이션·마진·원자재 자산 환경을 판단합니다.',
        'forecasts':forecasts,'data_quality':quality(data,CARD10_SERIES,errors),'source_status':source_status(data,CARD10_SERIES,source_overrides),
        'model_specification':{'selection':'expanding walk-forward','benchmark':'persistence','inputs':list(CARD10_SERIES)},
    }

def yahoo_history(session:requests.Session,symbol:str)->list[dict[str,Any]]:
    url='https://query1.finance.yahoo.com/v8/finance/chart/'+requests.utils.quote(symbol,safe='')
    r=session.get(url,params={'interval':'1d','range':'10y','events':'history'},timeout=(15,60)); r.raise_for_status()
    j=r.json(); z=j.get('chart',{}).get('result',[None])[0]
    if not z: return []
    ts=z.get('timestamp') or []; close=((z.get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
    out=[]
    for t,v in zip(ts,close):
        if finite(v): out.append({'date':datetime.fromtimestamp(t,timezone.utc).date().isoformat(),'value':float(v)})
    return out

def build_card12(session:requests.Session)->dict[str,Any]:
    histories={}; errors=[]
    for sym in YAHOO_SYMBOLS:
        try: histories[sym]=yahoo_history(session,sym)
        except Exception as e: histories[sym]=[]; errors.append(f'{sym}: {e}')
    current={sym:{'value':latest(rows)['value'],'date':latest(rows)['date']} for sym,rows in histories.items() if rows}
    groups={'equity':['ES=F','NQ=F','RTY=F'],'rates':['ZT=F','ZF=F','ZN=F','ZB=F'],'commodities':['CL=F','GC=F','HG=F'],'dollar':['DX-Y.NYB'],'crypto':['BTC=F']}
    group_scores={}
    for g,syms in groups.items():
        comps=[]
        for symbol in syms:
            v=values(histories.get(symbol,[]))
            if len(v)>63:
                mom=(pctchg(v,21) or 0)*.6+(pctchg(v,63) or 0)*.4
                daily=[(v[i]/v[i-1]-1)*100 for i in range(max(1,len(v)-63),len(v)) if v[i-1]]
                vol=statistics.stdev(daily) if len(daily)>2 else 0
                comps.append(clamp(mom/(vol*3+1),-2,2))
        group_scores[g]=mean(comps) if comps else None
    composite=mean([x for x in [group_scores.get('equity'),group_scores.get('rates'),group_scores.get('commodities')] if finite(x)])
    signal='good' if composite>.25 else 'bad' if composite<-.25 else 'neutral'
    predictive=build_futures_forecasts(histories)
    return {'schema_version':'1.1','card':12,'title':'선물시장 현재신호·검증형 방향예측','generated_at_utc':now_iso(),
            'current':current,'group_scores':group_scores,'market_signal':signal,
            'current_regime':'위험선호 우세' if signal=='good' else '위험회피 우세' if signal=='bad' else '선물시장 혼조',
            'future_regime':'검증 통과 기간만 방향예측 사용',
            'market_implied_interpretation':'현재 선물가격·최근 추세·변동성이 암시하는 방향이며 미래 가격 자체가 아닙니다.',
            'predictive_validation':predictive,
            'investment_conclusion':'현재 선물시장 신호와 장기 순차 OOS를 통과한 기간별 방향예측을 분리해 사용합니다.',
            'data_quality':{'completeness':round(100*sum(bool(v) for v in histories.values())/len(histories),1),'warnings':errors},
            'source_status':{sym:{'ok':bool(histories[sym]),'label':YAHOO_SYMBOLS[sym],'latest':current.get(sym),'source':('Yahoo Finance delayed index proxy' if sym=='DX-Y.NYB' else 'Yahoo Finance delayed futures')} for sym in histories},
            'limitations':['무료 지연 선물자료와 달러인덱스 대체신호를 사용하며 거래소 실시간 유료 시세를 대체하지 않습니다.','선물가격은 미래 현물가격의 단순 예측값이 아닙니다.','검증 미통과 기간은 현재신호 또는 참고값으로만 사용합니다.']}

def _age_days(date_text: str | None) -> int | None:
    try:
        d = datetime.fromisoformat(str(date_text)[:10]).date()
        return (datetime.now(timezone.utc).date() - d).days
    except Exception:
        return None


def quality(data, spec, errors):
    core = [k for k, v in spec.items() if v.get('core')]
    stale = []
    fresh_ok = []
    core_fresh_ok = []
    for sid, meta in spec.items():
        row = latest(data.get(sid, []))
        age = _age_days((row or {}).get('date')) if row else None
        max_age = int(meta.get('max_age_days', 120))
        is_fresh = bool(row) and age is not None and age <= max_age
        fresh_ok.append(is_fresh)
        if meta.get('core'):
            core_fresh_ok.append(is_fresh)
        if row and not is_fresh:
            stale.append({'series': sid, 'latest_date': row.get('date'), 'age_days': age, 'max_age_days': max_age})
    warnings = list(errors)
    warnings.extend([f"{x['series']} 최신성이 기준 초과: {x['latest_date']} ({x['age_days']}일 경과)" for x in stale])
    return {
        'completeness': round(100 * sum(fresh_ok) / len(spec), 1),
        'raw_collection_completeness': round(100 * sum(bool(data.get(k)) for k in spec) / len(spec), 1),
        'core_completeness': round(100 * sum(core_fresh_ok) / len(core), 1),
        'missing_core': [k for k in core if not data.get(k)],
        'stale_series': stale,
        'warnings': warnings,
        'release_lag_policy': '월간·주간 자료는 공표시차와 계열별 최신성 기준을 함께 반영',
        'real_time_vintage': False,
    }


def source_status(data, spec, source_overrides=None):
    source_overrides = source_overrides or {}
    out = {}
    for sid, meta in spec.items():
        row = latest(data.get(sid, []))
        age = _age_days((row or {}).get('date')) if row else None
        max_age = int(meta.get('max_age_days', 120))
        out[sid] = {
            'ok': bool(data.get(sid)),
            'fresh': bool(row) and age is not None and age <= max_age,
            'age_days': age,
            'max_age_days': max_age,
            'label': meta['label'],
            'latest': row,
            'url': GSCPI_XLSX_URL if sid == 'GSCPI' else f'https://fred.stlouisfed.org/series/{sid}',
            'collection_source': source_overrides.get(sid, 'FRED'),
        }
    return out

def load_json(path:Path)->dict[str,Any]:
    try:return json.loads(path.read_text(encoding='utf-8'))
    except:return {}

def _card8_validation(card8: dict[str, Any]) -> dict[str, Any]:
    gates = card8.get('quality_gates') or {}
    passed = []
    for horizon, gate in gates.items():
        targets = gate.get('passed_targets') or []
        if targets:
            passed.append({'horizon': horizon, 'targets': targets})
    return {'passed': bool(passed), 'passed_horizons': passed, 'reason': '3·6개월 핵심금리 검증 통과 여부'}


def _generic_forecast_validation(card: dict[str, Any]) -> dict[str, Any]:
    passed = [h for h, fc in (card.get('forecasts') or {}).items() if (fc.get('quality_gate') or {}).get('passed')]
    return {'passed': bool(passed), 'passed_horizons': passed}


def _card12_validation(card12: dict[str, Any]) -> dict[str, Any]:
    pv = card12.get('predictive_validation') or {}
    passed = pv.get('passed_horizons') or []
    return {'passed': bool(passed), 'passed_horizons': passed}


def build_card11(card8, card9, card10, card12) -> dict[str, Any]:
    validations = {
        'card8': _card8_validation(card8),
        'card9': _generic_forecast_validation(card9),
        'card10': _generic_forecast_validation(card10),
        'card12': _card12_validation(card12),
    }
    signals = []
    weights = {'card8': 1.2, 'card9': 1.2, 'card10': 1.0, 'card12': 0.8}
    cards = {'card8': card8, 'card9': card9, 'card10': card10, 'card12': card12}
    for key, c in cards.items():
        s = c.get('market_signal')
        direction = 1 if s == 'good' else -1 if s == 'bad' else 0
        # A card contributes at full weight only when it has at least one validated horizon.
        confidence = 1.0 if validations[key]['passed'] else 0.5
        signals.append((direction, weights[key] * confidence))
    denom = sum(w for _, w in signals) or 1.0
    score = sum(s * w for s, w in signals) / denom * 100
    signal = 'good' if score >= 20 else 'bad' if score <= -20 else 'neutral'

    quality_checks = {
        'card8_validation': validations['card8']['passed'],
        'card9_validation': validations['card9']['passed'],
        'card10_validation': validations['card10']['passed'],
        'card12_validation': validations['card12']['passed'],
        'card8_data': card8.get('data_quality', {}).get('core_completeness', 0) >= 85,
        'card9_data': card9.get('data_quality', {}).get('core_completeness', 0) >= 85,
        'card10_data': card10.get('data_quality', {}).get('core_completeness', 0) >= 85,
        'card12_data': card12.get('data_quality', {}).get('completeness', 0) >= 80,
    }
    quality_passed = all(quality_checks.values())
    quality_level = '준기관급 통합판정' if quality_passed else '조건부 통합판정'

    input_details = {
        'card8': {'label': '미국채 금리·실질금리', 'signal': card8.get('market_signal'), 'validation': validations['card8']},
        'card9': {'label': '글로벌 고용·소비', 'signal': card9.get('market_signal'), 'validation': validations['card9']},
        'card10': {'label': '원자재·에너지·공급망 압력', 'signal': card10.get('market_signal'), 'validation': validations['card10']},
        'card12': {'label': '선물시장 현재신호·방향예측', 'signal': card12.get('market_signal'), 'validation': validations['card12']},
    }
    return {
        'schema_version': '1.1', 'card': 11, 'title': '글로벌 경기국면 최종판정·투자환경', 'generated_at_utc': now_iso(),
        'score': round(score, 1), 'market_signal': signal,
        'current_regime': '확장·위험선호' if signal == 'good' else '수축·위험회피' if signal == 'bad' else '혼합·중립',
        'future_regime': '우호적 투자환경' if signal == 'good' else '불리한 투자환경' if signal == 'bad' else '중립적 투자환경',
        'asset_environment': {
            'global_equities': '우호' if signal == 'good' else '불리' if signal == 'bad' else '중립',
            'long_treasuries': '우호' if card8.get('market_signal') == 'good' else '중립',
            'gold': '우호' if card8.get('market_signal') == 'good' or card10.get('market_signal') == 'bad' else '중립',
            'commodities': '우호' if card10.get('market_signal') == 'good' and card9.get('market_signal') == 'good' else '중립',
            'cash_short_duration': '우호' if signal == 'bad' else '중립',
        },
        'inputs': {k: v.get('market_signal') for k, v in cards.items()},
        'input_details': input_details,
        'quality_gate': {'passed': quality_passed, 'level': quality_level, 'checks': quality_checks},
        'investment_conclusion': '검증을 통과한 하위 카드 신호와 최신성·완전성을 함께 반영해 자산군의 상대적 우호도를 판단합니다.',
    }

def main():
    session=build_http_session(); OUT_DIR.mkdir(parents=True,exist_ok=True)
    card9=build_card9(session); card10=build_card10(session); card12=build_card12(session)
    card8=load_json(OUT_DIR/'us_treasury_card8.json')
    card11=build_card11(card8,card9,card10,card12)
    for n,obj in [(9,card9),(10,card10),(11,card11),(12,card12)]:
        (OUT_DIR/f'card{n}.json').write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT_DIR/'cards_8_12_bundle.json').write_text(json.dumps({'generated_at_utc':now_iso(),'cards':{'8':card8,'9':card9,'10':card10,'11':card11,'12':card12}},ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__': main()
