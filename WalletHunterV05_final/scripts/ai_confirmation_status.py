"""Sanitised, public-only postflight for AI forms; never confirms an order."""
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.settings import load
from core.storage import Storage
from core.ai_user_orders import AiUserOrders
from core.ai_position_actions import AiPositionActions
from core.ai_entry_policy import ENTRY_MARGIN_FRACTION, MAX_ENTRY_LEVERAGE

settings=load()
storage=Storage(str(ROOT),settings.master_key)
now=int(time.time()*1000)
entries,actions=AiUserOrders(str(ROOT)),AiPositionActions(str(ROOT))
result=[]
for uid,p in storage.load()['profiles'].items():
    if not p.get('account'):continue
    entry=entries.summary(uid,p,now)
    change=actions.summary(uid,p,now)
    def forms(rows):
        return [{'coin':r['payload']['coin'],'action':r['payload']['action'],'status':r['status'],
                 'expires_in_s':round((r['expires_ms']-now)/1000),
                 'notified':r.get('notified'),
                 'roe':r['payload'].get('position_before',{}).get('roe')} for r in rows]
    result.append({'user_suffix':uid[-3:],'copy_enabled':p.get('copy_enabled'),
        'ai_slot_selected':p.get('ai_slot_selected'),'ai_trader_enabled':p.get('ai_trader_enabled'),
        'ai_review_enabled':p.get('ai_review_enabled',True),'profile_max_leverage':p.get('max_leverage'),
        'entries':{'status':entry['status'],'reason':entry.get('reason'),'pending':forms(entry['pending'])},
        'position_actions':{'pending':forms(change['pending']),'availability':change['availability'],
                            'held_markets':[r['market'] for r in change['holds']]}})
print(json.dumps({'profiles':result,'entry_pct_of_ai_third':100*ENTRY_MARGIN_FRACTION,
    'max_entry_leverage':MAX_ENTRY_LEVERAGE,'automatic_real_execution':False,
    'orders_sent_by_audit':0},ensure_ascii=False))
