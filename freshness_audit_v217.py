from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'public'/'data'

def read(name:str)->dict[str,Any]:
    try:
        x=json.loads((DATA/name).read_text(encoding='utf-8'))
        return x if isinstance(x,dict) else {}
    except Exception:
        return {}

def parse_dt(v:Any):
    t=str(v or '').strip()
    if not t:return None
    try:
        d=datetime.fromisoformat(t.replace('Z','+00:00'))
        if d.tzinfo is None:d=d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:return None

def age_hours(v:Any):
    d=parse_dt(v)
    return None if d is None else max(0,(datetime.now(timezone.utc)-d).total_seconds()/3600)

def main():
    status=read('source_status.json'); card12=read('card12.json'); bundle=read('cards_8_12_bundle.json')
    snaps=card12.get('market_snapshots') if isinstance(card12.get('market_snapshots'),dict) else {}
    market=[]
    for sym,s in snaps.items():
        s=s if isinstance(s,dict) else {}; age=age_hours(s.get('market_time_utc'))
        market.append({'symbol':sym,'status':'LIVE' if age is not None and age<=3 else 'CACHE' if s.get('price') is not None else 'UNAVAILABLE',
                       'market_time_utc':s.get('market_time_utc'),'age_hours':round(age,2) if age is not None else None,'price':s.get('price'),'source':s.get('source')})
    ism_latest=status.get('latest_date')
    payload={
      'schema_version':'1.0.0','patch_version':'V217-additive-freshness-contract','generated_at_utc':datetime.now(timezone.utc).isoformat(),
      'bundle_generated_at_utc':bundle.get('generated_at_utc'),'compatibility':{'existing_keys_removed':False,'existing_field_semantics_changed':False,'new_network_calls':0,'model_formulas_changed':False},
      'sources':{'ism':{'status':'LIVE' if status.get('ok') else 'LKG' if status.get('last_good_reused') else 'UNAVAILABLE','observation':ism_latest,'network_skipped_fresh':status.get('network_skipped_fresh')},
                 'card12_market':market},
      'summary':{'market_live':sum(x['status']=='LIVE' for x in market),'market_cache':sum(x['status']=='CACHE' for x in market),'market_unavailable':sum(x['status']=='UNAVAILABLE' for x in market)}
    }
    (DATA/'freshness_status.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(payload['summary']),flush=True)
if __name__=='__main__':main()
