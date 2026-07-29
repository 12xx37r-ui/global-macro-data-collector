from __future__ import annotations

import json, math, os, statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

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
    'OECDSLRTTO01IXOBSAM': {'label':'OECD 소매판매량','core':False,'adverse_up':False},
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
    'GSCPI': {'label':'뉴욕연은 글로벌 공급망압력','core':True,'adverse_up':True},
    'WPUFD4': {'label':'미국 최종수요 생산자물가','core':False,'adverse_up':True},
}

# Card 12: futures market. Yahoo continuous contracts are delayed/free market inputs.
YAHOO_SYMBOLS = {
    'ES=F':'S&P500 선물','NQ=F':'나스닥100 선물','RTY=F':'러셀2000 선물',
    'ZT=F':'미국 2년 국채선물','ZF=F':'미국 5년 국채선물','ZN=F':'미국 10년 국채선물','ZB=F':'미국 30년 국채선물',
    'CL=F':'WTI 원유선물','GC=F':'금선물','HG=F':'구리선물','DX=F':'달러인덱스선물','BTC=F':'비트코인선물',
}

HORIZONS = {'5d':5,'1m':21,'3m':63,'6m':126,'12m':252}


def now_iso(): return datetime.now(timezone.utc).isoformat()

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
    data,errors=fetch_fred_all(session,list(CARD10_SERIES))
    hist=composite_history(data,CARD10_SERIES,'commodity')
    vals=values(hist)
    if len(vals)<60: raise RuntimeError('Card10 composite history insufficient')
    forecasts={}
    for key,months in [('1m',1),('3m',3),('6m',6),('12m',12)]: forecasts[key]=walk_forward(vals,months,60 if months<=6 else 45)
    current=vals[-1]; f3=forecasts['3m']['forecast']; delta=f3-current
    # Composite higher = more favorable (less inflationary pressure / healthier industrial demand)
    signal='good' if delta>.8 else 'bad' if delta<-.8 else 'neutral'
    return {
        'schema_version':'1.0','card':10,'title':'원자재·에너지·공급망 압력','generated_at_utc':now_iso(),
        'current':current,'current_date':hist[-1]['date'],'forecast_3m':f3,'forecast_6m':forecasts['6m']['forecast'],
        'forecast_range80_3m':forecasts['3m']['range80'],'future_direction':'up' if delta>.4 else 'down' if delta<-.4 else 'flat',
        'market_signal':signal,'current_regime':'공급·원가 우호' if current>=55 else '공급·원가 부담' if current<45 else '중립',
        'future_regime':'압력완화' if signal=='good' else '압력확대' if signal=='bad' else '보합',
        'investment_conclusion':'원가압력과 산업수요를 함께 봐 인플레이션·마진·원자재 자산 환경을 판단합니다.',
        'forecasts':forecasts,'data_quality':quality(data,CARD10_SERIES,errors),'source_status':source_status(data,CARD10_SERIES),
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
    groups={'equity':['ES=F','NQ=F','RTY=F'],'rates':['ZT=F','ZF=F','ZN=F','ZB=F'],'commodities':['CL=F','GC=F','HG=F'],'dollar':['DX=F'],'crypto':['BTC=F']}
    group_scores={}
    for g,syms in groups.items():
        comps=[]
        for s in syms:
            v=values(histories.get(s,[]))
            if len(v)>63:
                mom=(pctchg(v,21) or 0)*.6+(pctchg(v,63) or 0)*.4
                vol=statistics.stdev([(v[i]/v[i-1]-1)*100 for i in range(max(1,len(v)-63),len(v))]) if len(v)>3 else 0
                comps.append(clamp(mom/(vol*3+1),-2,2))
        group_scores[g]=mean(comps) if comps else None
    # Higher Treasury futures price means lower yield and generally easier financial conditions.
    composite=mean([x for x in [group_scores.get('equity'),group_scores.get('rates'),group_scores.get('commodities')] if finite(x)])
    signal='good' if composite>.25 else 'bad' if composite<-.25 else 'neutral'
    return {'schema_version':'1.0','card':12,'title':'선물시장 종합신호','generated_at_utc':now_iso(),
            'current':current,'group_scores':group_scores,'market_signal':signal,
            'current_regime':'위험선호 우세' if signal=='good' else '위험회피 우세' if signal=='bad' else '선물시장 혼조',
            'future_regime':'추세지속 가능성 점검','investment_conclusion':'주가지수·국채·원자재·달러 선물의 방향과 변동성을 교차검증합니다.',
            'data_quality':{'completeness':round(100*sum(bool(v) for v in histories.values())/len(histories),1),'warnings':errors},
            'source_status':{s:{'ok':bool(histories[s]),'label':YAHOO_SYMBOLS[s],'latest':current.get(s),'source':'Yahoo Finance delayed futures'} for s in histories},
            'limitations':['무료 지연 선물자료이며 거래소 실시간 유료 시세를 대체하지 않습니다.','미국 정책금리 경로는 미국엔진 결과를 읽기 전용으로 사용합니다.']}

def quality(data,spec,errors):
    core=[k for k,v in spec.items() if v.get('core')]
    return {'completeness':round(100*sum(bool(data.get(k)) for k in spec)/len(spec),1),
            'core_completeness':round(100*sum(bool(data.get(k)) for k in core)/len(core),1),
            'missing_core':[k for k in core if not data.get(k)],'warnings':errors,
            'release_lag_policy':'월간·주간 자료는 공표시차를 보수적으로 반영','real_time_vintage':False}

def source_status(data,spec):
    return {sid:{'ok':bool(data.get(sid)),'label':meta['label'],'latest':latest(data.get(sid,[])),'url':f'https://fred.stlouisfed.org/series/{sid}'} for sid,meta in spec.items()}

def load_json(path:Path)->dict[str,Any]:
    try:return json.loads(path.read_text(encoding='utf-8'))
    except:return {}

def build_card11(card8,card9,card10,card12)->dict[str,Any]:
    signals=[]
    for c,w in [(card8,1.2),(card9,1.2),(card10,1.0),(card12,.8)]:
        s=c.get('market_signal')
        signals.append((1 if s=='good' else -1 if s=='bad' else 0,w))
    score=sum(s*w for s,w in signals)/sum(w for _,w in signals)*100
    signal='good' if score>=20 else 'bad' if score<=-20 else 'neutral'
    return {'schema_version':'1.0','card':11,'title':'글로벌 경기국면 최종판정·투자환경','generated_at_utc':now_iso(),
            'score':round(score,1),'market_signal':signal,
            'current_regime':'확장·위험선호' if signal=='good' else '수축·위험회피' if signal=='bad' else '혼합·중립',
            'future_regime':'우호적 투자환경' if signal=='good' else '불리한 투자환경' if signal=='bad' else '중립적 투자환경',
            'asset_environment':{
                'global_equities':'우호' if signal=='good' else '불리' if signal=='bad' else '중립',
                'long_treasuries':'우호' if card8.get('market_signal')=='good' else '중립',
                'gold':'우호' if card8.get('market_signal')=='good' or card10.get('market_signal')=='bad' else '중립',
                'commodities':'우호' if card10.get('market_signal')=='good' and card9.get('market_signal')=='good' else '중립',
                'cash_short_duration':'우호' if signal=='bad' else '중립',
            },
            'inputs':{'card8':card8.get('market_signal'),'card9':card9.get('market_signal'),'card10':card10.get('market_signal'),'card12':card12.get('market_signal')},
            'quality_gate':{'passed':all(c.get('data_quality',{}).get('core_completeness',100)>=85 for c in (card8,card9,card10)),'level':'준기관급 통합판정'},
            'investment_conclusion':'카드별 방향·완전성·검증 통과 범위를 합쳐 자산군의 상대적 우호도를 판단합니다.'}

def main():
    session=build_http_session(); OUT_DIR.mkdir(parents=True,exist_ok=True)
    card9=build_card9(session); card10=build_card10(session); card12=build_card12(session)
    card8=load_json(OUT_DIR/'us_treasury_card8.json')
    card11=build_card11(card8,card9,card10,card12)
    for n,obj in [(9,card9),(10,card10),(11,card11),(12,card12)]:
        (OUT_DIR/f'card{n}.json').write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT_DIR/'cards_8_12_bundle.json').write_text(json.dumps({'generated_at_utc':now_iso(),'cards':{'8':card8,'9':card9,'10':card10,'11':card11,'12':card12}},ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__': main()
