from datetime import datetime, timezone
import macro_cards_9_12 as m


def test_v218_card12_current_prefers_market_snapshot(monkeypatch):
    def fake(session,symbol):
        return ([{'date':'2026-08-14','value':100.0+i} for i in range(70)],
                {'symbol':symbol,'price':222.0,'market_time_utc':'2026-08-16T04:00:00+00:00','source':'Yahoo Finance chart metadata'})
    monkeypatch.setattr(m,'yahoo_history_with_snapshot',fake)
    monkeypatch.setattr(m,'build_futures_forecasts',lambda histories:{'quality_gate':{'passed':True}})
    out=m.build_card12(object())
    assert all(v['value']==222.0 for v in out['current'].values())
    assert out['freshness_contract']['network_refetch_each_workflow'] is True
