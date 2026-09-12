"""Immutable, mode-separated evaluation of proven executions (not closure markers)."""
import json
import math
import time
from core.foundation.store import scope_key


def calculate(executions,direction):
    """Weighted-average cost basis matches canonical PAPER position accounting."""
    units=cost=entered=entry_cost=gross=fees=exited=exit_cost=slippage=0.
    fees_known=slippage_known=True;first=last=None;receipts=[]
    for action in executions:
        receipts.append(action['intent_id'])
        for fill in action['fills']:
            size,price=float(fill['size']),float(fill['price'])
            if not all(math.isfinite(v) and v>0 for v in (size,price)):raise ValueError('Invalid outcome fill')
            stamp=fill['exchange_ms'];first=stamp if first is None else min(first,stamp);last=max(last or stamp,stamp)
            if action['action'] in {'OPEN','ADD'}:
                units+=size;cost+=size*price;entered+=size;entry_cost+=size*price
            else:
                if units<=0 or size>units+1e-9:raise ValueError('Outcome inventory inconsistent')
                average=cost/units
                gross+=(price-average)*size*(1 if direction=='LONG' else -1)
                units-=size;cost-=average*size;exited+=size;exit_cost+=size*price
            fee=fill.get('fee')
            if fee is None:fees_known=False
            elif not math.isfinite(float(fee)) or fee<0:raise ValueError('Invalid fee evidence')
            else:fees+=fee
            reference=action.get('reference_price')
            if reference is not None:slippage+=(price-reference)*size*(1 if fill['side']=='BUY' else -1)
            else:slippage_known=False
    if not executions or entered<=0 or abs(units)>1e-8:raise ValueError('Closed inventory not proven')
    net=gross-fees if fees_known else None
    return dict(entry_ms=first,exit_ms=last,duration_ms=last-first,entry_price=entry_cost/entered,
        exit_price=exit_cost/exited,filled_size=entered,exit_size=exited,gross_pnl=gross,fees=fees if fees_known else None,
        net_pnl=net,return_pct=net/entry_cost*100 if net is not None else None,
        return_basis='TRADED_ENTRY_NOTIONAL',slippage_vs_reference=slippage if slippage_known else None,
        mae=None,mfe=None,leader_outcome=None,action_ids=receipts)


def record_outcome(db,episode):
    oid=episode.episode_id+'-outcome-v2'
    if db.execute('SELECT 1 FROM autonomous_outcomes WHERE id=?',(oid,)).fetchone():return
    rows=db.execute('SELECT a.id,p.body AS prediction,i.body AS intent,i.receipt,i.decision AS risk,h.body AS hypothetical '
        'FROM episode_actions a JOIN autonomous_predictions p ON p.id=a.id LEFT JOIN intents i ON i.id=a.id '
        'LEFT JOIN hypothetical_executions h ON h.id=a.id WHERE a.episode=? ORDER BY a.rowid',(episode.episode_id,)).fetchall()
    executions=[];predictions=[];evidence=[];recorded=0
    for row in rows:
        prediction=json.loads(row['prediction']);predictions.append(dict(id=row['id'],prediction=prediction,
            execution_risk=json.loads(row['risk']) if row['risk'] else prediction.get('risk')))
        if row['hypothetical']:
            raw=json.loads(row['hypothetical']);intent=raw['intent'];fills=raw['fills'];costs=raw['cost_model'];recorded=max(recorded,raw['recorded_ms'])
        elif row['receipt']:
            receipt=json.loads(row['receipt'])
            if receipt['status'] not in {'FILLED','PARTIAL'}:continue
            intent=json.loads(row['intent']);fills=receipt['fills'];costs=prediction.get('cost_model');recorded=max(recorded,receipt['received_ms'])
            evidence.append(receipt['provenance'])
        else:continue
        normalized=[]
        for fill in fills:
            # Live fees are unavailable in the current canonical fill contract.
            # Never substitute simulated fees or zero for missing exchange fees.
            fee=fill['size']*fill['price']*costs['fee_bps']/10000 if episode.mode not in ('LIVE_CONFIRM','LIVE_AUTO') and costs else None
            normalized.append(dict(fill,fee=fee))
        executions.append(dict(intent_id=row['id'],action=intent['action'],fills=normalized,
            reference_price=prediction['market']['price']))
    direction='LONG' if json.loads(rows[0]['prediction'])['event']['side']=='BUY' else 'SHORT'
    mode={'PAPER_AUTO':'PAPER','SHADOW':'SHADOW','LIVE_CONFIRM':'LIVE','LIVE_AUTO':'LIVE'}[episode.mode]
    if mode=='LIVE' and any(x!='EXCHANGE' for x in evidence):raise ValueError('Live evidence mismatch')
    result=calculate(executions,direction)
    outcome=dict(result,version='outcome-v2',mode=mode,operating_mode=episode.mode,episode_id=episode.episode_id,
        leader=episode.leader,instrument=episode.instrument.model_dump(mode='json'),direction=direction,
        recorded_ms=recorded,processed_ms=int(time.time()*1000),prediction_ids=[p['id'] for p in predictions],
        evidence={'PAPER':'SIMULATED','SHADOW':'HYPOTHETICAL','LIVE':'EXCHANGE'}[mode],
        cost_model=predictions[0]['prediction'].get('cost_model'))
    text=json.dumps(outcome,sort_keys=True,allow_nan=False)
    db.execute('INSERT INTO autonomous_outcomes VALUES(?,?,?,?)',(oid,scope_key(episode.scope),mode,text))
    label=None if outcome['net_pnl'] is None else outcome['net_pnl']>0
    evaluation={'version':'evaluation-v2','outcome_id':oid,'mode':mode,'sample_count':1,
        'positive_net':label,'net_pnl':outcome['net_pnl'],'decisions':[
            {'prediction_id':p['id'],'leader':p['prediction']['leader'],'agents':p['prediction']['agents'],
             'consensus':p['prediction']['consensus'],'risk':p['execution_risk'],
             'confidence_is_not_a_calibrated_probability':True} for p in predictions],
        'promotion':'DISABLED','skip_opportunity_cost':None}
    db.execute('INSERT INTO calibration_records VALUES(?,?,?)',(oid,scope_key(episode.scope),json.dumps(evaluation,sort_keys=True,allow_nan=False)))
