"""Read-only financial notification projection. Never grants execution authority.

Canonical receipts prove follower actions; research events cannot mint notices.
The durable product outbox owns delivery, independently of financial workers.
"""
import hashlib
import json
import math
import re
import sqlite3
from core.foundation.contracts import OrderIntent, ExecutionReceipt, PortfolioSnapshot
from core.foundation.store import scope_key

PREFERENCES = {k: True for k in ('OPEN','ADD','REDUCE','REVERSE','CLOSE','CRITICAL','LIVE_CONFIRM')}
FINANCIAL = {'POSITION_OPEN':'OPEN','POSITION_ADD':'ADD','POSITION_REDUCE':'REDUCE',
             'POSITION_REVERSE':'REVERSE','POSITION_CLOSE':'CLOSE'}
CRITICAL = {'EXECUTION_UNKNOWN','RECONCILIATION_REQUIRED','CRITICAL_TRADING_FAILURE'}


def execution_identity(data):
    """Known product/domain envelopes only; never infer identity from a market.

    Conflicting IDs are ambiguous and must not suppress another intent's alert.
    Legacy rows may lack mode; an explicit PAPER/LIVE distinction is preserved.
    """
    if not isinstance(data,dict):return None,None
    nodes=[data]
    for path in (('payload',),('evidence',),('evidence','payload')):
        node=data
        for part in path:node=node.get(part) if isinstance(node,dict) else None
        if isinstance(node,dict):nodes.append(node)
    ids={n['intent_id'] for n in nodes if isinstance(n.get('intent_id'),str) and n['intent_id']}
    mode=data.get('mode') or data.get('execution_mode')
    mode={'PAPER_AUTO':'PAPER','LIVE_CONFIRM':'LIVE'}.get(mode,mode)
    return (next(iter(ids)) if len(ids)==1 else None),mode


def classification(kind, data):
    """Positive allowlist, enforced at enqueue AND delivery (including old rows)."""
    if not isinstance(data,dict):return None
    if data.get('mode') == 'SHADOW': return None
    if kind in FINANCIAL and data.get('proof') == 'CANONICAL_RECONCILED_FILL' and data.get('mode') in {'PAPER','LIVE'}:
        return FINANCIAL[kind]
    if kind in CRITICAL: return 'CRITICAL'
    # Compatibility for already durable unknown alerts, not generic updates.
    if kind == 'EXECUTION_UPDATED' and data.get('status') in {'UNKNOWN','RECONCILIATION_REQUIRED'}:
        return 'CRITICAL'
    if kind == 'LIVE_CONFIRM_REQUIRED' and data.get('mode') == 'LIVE_CONFIRM': return 'LIVE_CONFIRM'
    return None


def _intent(db, scope, identity):
    row=db.execute('SELECT * FROM intents WHERE scope=? AND id=?',(scope_key(scope),identity)).fetchone()
    if not row or row['status']=='ARCHIVED_UNRESOLVED': return None
    intent=OrderIntent.model_validate_json(row['body'])
    if intent.scope != scope: raise ValueError('NOTICE_SCOPE_MISMATCH')
    receipt=ExecutionReceipt.model_validate_json(row['receipt']) if row['receipt'] else None
    if receipt and (receipt.scope != scope or receipt.intent_id != intent.intent_id): raise ValueError('NOTICE_RECEIPT_MISMATCH')
    pre=db.execute('SELECT body FROM intent_prestate WHERE id=?',(identity,)).fetchone()
    before=PortfolioSnapshot.model_validate_json(pre[0]) if pre else None
    if before and before.scope != scope: raise ValueError('NOTICE_PRESTATE_MISMATCH')
    return intent,receipt,before


def _proven(item):
    i,r,_=item
    return bool(r and r.status=='FILLED' and r.reconciliation=='CONFIRMED' and r.fills
        and r.provenance==('EXCHANGE' if i.execution_mode=='LIVE' else 'FAKE_EXCHANGE')
        and all(f.instrument==i.instrument and f.side==i.side for f in r.fills)
        and math.isclose(sum(f.size for f in r.fills),i.size,rel_tol=1e-7,abs_tol=1e-10))


def _details(item):
    i,r,before=item
    data={'intent_id':i.intent_id,'correlation_id':i.correlation_id,'action':i.action,
          'mode':'LIVE' if i.execution_mode=='LIVE' else 'PAPER','instrument':i.instrument.model_dump(mode='json'),
          'symbol':i.instrument.symbol,'leverage':i.leverage,'timestamp':r.received_ms if r else i.created_ms,
          'source':'COPY' if i.authorization=='COPY_POLICY' else 'AUTONOMOUS' if i.authorization=='PAPER_POLICY' else 'CONFIRMED_USER'}
    wallets=[c.source for c in i.source_contributions] or [i.source]
    data['leaders']=[w for w in wallets if re.fullmatch(r'0x[0-9a-f]{40}',w)]
    position=next((p for p in before.positions if p.instrument==i.instrument),None) if before else None
    data['side']=position.side if position and i.action in {'REDUCE','CLOSE'} else 'LONG' if i.side=='BUY' else 'SHORT'
    if _proven(item):
        size=sum(f.size for f in r.fills);price=sum(f.size*f.price for f in r.fills)/size
        data.update(proof='CANONICAL_RECONCILED_FILL',filled_size=size,price=price,order_ids=list(r.order_ids))
        # Not an exchange margin claim: explicit notional / leverage estimate.
        if i.action in {'OPEN','ADD'}: data['margin_estimate']=size*price/i.leverage
        if position and i.action in {'REDUCE','CLOSE'}:
            data['entry']=position.entry_price
            data['reduced_pct']=min(100.,size/position.size*100)
            data['remaining_size']=max(0.,position.size-size)
            data['gross_pnl']=(price-position.entry_price)*size*(1 if position.side=='LONG' else -1)
    return data


def _reverse(db, scope, item):
    """Only explicit durable parent evidence may pair reversal legs."""
    i,_,_=item; parent=i.parent_intent_id; ids=[]; envelope=None; terminal=False; declined=False
    if parent:
        try:
            op=db.execute('SELECT * FROM operations WHERE id=? AND account=?',(parent,scope.account)).fetchone()
            if op:
                e=json.loads(op['intent']); pre=e.get('before') or {}; target=e.get('target') or {}
                reverse=e.get('action')=='MANUAL_LEADER_REVERSE' or (pre.get('side') in {'LONG','SHORT'} and target.get('side') in {'LONG','SHORT'} and pre['side']!=target['side'])
                if e.get('network')==scope.network and reverse:
                    envelope=e;ids=e.get('canonical_intents',[]);terminal=op['status'] in {'CONFIRMED','REJECTED'}
        except sqlite3.OperationalError: pass # No legacy journal in PAPER store.
    try:
        job=db.execute('SELECT record FROM autonomous_jobs WHERE scope=? AND event_id=?',(scope_key(scope),i.correlation_id)).fetchone()
        e=json.loads(job[0]).get('parent_event') if job else None
        if e and e.get('action')=='REVERSE' and e.get('instrument')==i.instrument.model_dump(mode='json'):
            parent=e['event_id'];envelope=e
            correlations=[hashlib.sha256((parent+'|'+leg).encode()).hexdigest()[:48] for leg in ('close','open')]
            ids=[r[0] for r in db.execute("SELECT id FROM intents WHERE scope=? AND json_extract(body,'$.correlation_id') IN (?,?)",(scope_key(scope),*correlations))]
            decision=db.execute('SELECT intent FROM autonomous_decisions WHERE scope=? AND event_id=?',(scope_key(scope),correlations[1])).fetchone()
            declined=bool(decision and not decision[0])
    except sqlite3.OperationalError: pass
    if envelope is None: return None
    if len(ids)>8: raise ValueError('NOTICE_REVERSE_BOUND')
    items=[x for identity in ids if (x:=_intent(db,scope,identity)) and x[0].instrument==i.instrument]
    close=next((x for x in items if x[0].action=='CLOSE'),None)
    opening=next((x for x in items if x[0].action=='OPEN'),None)
    if close and opening and _proven(close) and _proven(opening) and close[0].side==opening[0].side:
        a,b=_details(close),_details(opening)
        if a['side']==b['side'] or min(f.exchange_ms for f in opening[1].fills)<max(f.exchange_ms for f in close[1].fills):
            raise ValueError('NOTICE_REVERSE_PROOF')
        b.update(previous_side=a['side'],gross_pnl=a.get('gross_pnl'),correlation_id=parent,action='REVERSE')
        if envelope.get('strategy')=='MANUAL_LEADER_COPY':b['source']='MANUAL_COPY'
        return parent,'POSITION_REVERSE',b
    if close and _proven(close) and (terminal or declined) and (not opening or (opening[1] and opening[1].status=='REJECTED')):
        # Proven close, aborted new leg: never announce a reversal.
        data=_details(close);data.update(reverse_incomplete=True,action='REVERSE',closed_proven=True)
        return parent,'CRITICAL_TRADING_FAILURE',data
    if any(x[1] and x[1].status in {'PARTIAL','UNKNOWN'} for x in items):
        data=_details(item);data.update(action='REVERSE',status='RECONCILIATION_REQUIRED')
        return parent,'RECONCILIATION_REQUIRED',data
    return () # Close proved but new leg not yet known. Defer the push.


def financial_notice(db,scope,identity):
    item=_intent(db,scope,identity)
    if not item:return None
    i,r,_=item
    if not r:return None
    reversal=_reverse(db,scope,item)
    if reversal is not None:return reversal or None
    data=_details(item)
    if r.status in {'UNKNOWN','PARTIAL'}:
        data['status']='RECONCILIATION_REQUIRED' if r.status=='PARTIAL' else 'UNKNOWN'
        return i.intent_id,('RECONCILIATION_REQUIRED' if r.status=='PARTIAL' else 'EXECUTION_UNKNOWN'),data
    if not _proven(item) or i.action not in {'OPEN','ADD','REDUCE','CLOSE'}:return None
    try:
        row=db.execute('SELECT body FROM autonomous_predictions WHERE id=? AND scope=?',(identity,scope_key(scope))).fetchone()
        prediction=json.loads(row[0]) if row else {}
        leader=(prediction.get('event') or {}).get('wallet')
        if isinstance(leader,str) and re.fullmatch(r'0x[0-9a-f]{40}',leader):data['leaders']=[leader]
        confidence=(prediction.get('consensus') or {}).get('confidence')
        if isinstance(confidence,(float,int)) and 0<=confidence<=1:data['confidence']=confidence
        if row:data['source']='AUTONOMOUS'
        if i.action=='CLOSE':
            row=db.execute('SELECT o.body FROM episode_actions a JOIN autonomous_outcomes o ON json_extract(o.body,\'$.episode_id\')=a.episode WHERE a.id=? AND o.scope=? AND o.mode=?',(identity,scope_key(scope),data['mode'])).fetchone()
            outcome=json.loads(row[0]) if row else {}
            if outcome.get('version')=='outcome-v2' and identity in outcome.get('action_ids',[]):
                for name in ('net_pnl','gross_pnl','fees','duration_ms'):
                    value=outcome.get(name)
                    if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):data[name]=value
    except sqlite3.OperationalError:pass
    # Label the configured source from the linked journal, never from a leader event.
    if i.parent_intent_id:
        try:
            op=db.execute('SELECT intent FROM operations WHERE id=? AND account=?',(i.parent_intent_id,scope.account)).fetchone()
            e=json.loads(op[0]) if op else {}
            if e.get('network')==scope.network and (e.get('strategy')=='MANUAL_LEADER_COPY' or e.get('action','').startswith('MANUAL_LEADER_')):data['source']='MANUAL_COPY'
        except sqlite3.OperationalError:pass
    return i.intent_id,'POSITION_'+i.action,data


def notice_text(event,identity=None,en=True):
    """Public text intentionally excludes all audit IDs and raw error strings."""
    d=event.get('data',{});kind=event.get('type');category=classification(kind,d)
    if not category: return ''
    tr=lambda a,b:a if en else b
    mode='⚠️ LIVE' if d.get('mode') in {'LIVE','LIVE_CONFIRM'} else '🧪 PAPER' if d.get('mode')=='PAPER' else tr('MODE UNVERIFIED','РЕЖИМ НЕ ПОДТВЕРЖДЁН')
    if category=='CRITICAL':
        if d.get('reverse_incomplete'):
            return '\n'.join(['⚠️ '+tr('REVERSAL NOT COMPLETED','РАЗВОРОТ НЕ ЗАВЕРШЁН'),mode,d.get('symbol',''),
                tr('Old side closed. Opposite entry was not completed. Review the position in the app.','Прежняя позиция закрыта. Вход в другую сторону не выполнен. Проверьте позицию в приложении.')])
        if kind=='CRITICAL_TRADING_FAILURE':
            return '\n'.join(['⚠️ '+tr('CRITICAL TRADING DATA FAILURE','КРИТИЧЕСКАЯ ОШИБКА ТОРГОВЫХ ДАННЫХ'),mode,
                tr('Financial state needs review. Check System Health before taking action.','Финансовое состояние требует проверки. Откройте Систему перед следующим действием.')])
        title=tr('EXECUTION STATUS UNKNOWN','СТАТУС ИСПОЛНЕНИЯ НЕИЗВЕСТЕН') if kind in {'EXECUTION_UNKNOWN','EXECUTION_UPDATED'} else tr('RECONCILIATION REQUIRED','ТРЕБУЕТСЯ СВЕРКА')
        return '\n'.join(['⚠️ '+title,mode,' · '.join(str(d[k]) for k in ('symbol','action') if d.get(k)),
            tr('Completion is not yet proven. Check System Health.','Завершение операции пока не подтверждено. Проверьте Систему.'),
            tr('No automatic retry is authorized.','Автоматический повтор не разрешён.')])
    if category=='LIVE_CONFIRM':
        symbol=d.get('symbol','')
        symbol=symbol if isinstance(symbol,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,40}',symbol) else ''
        return tr('⚠️ LIVE · Confirmation required','⚠️ LIVE · Требуется подтверждение')+(' · '+symbol if symbol else '')+'\n'+tr('Review the proposal in the app. No order has been authorized.','Проверьте предложение в приложении. Ордер ещё не разрешён.')
    words={'OPEN':('OPENED','ОТКРЫТА'),'ADD':('INCREASED','УВЕЛИЧЕНА'),'REDUCE':('REDUCED','СОКРАЩЕНА'),'REVERSE':('REVERSED','РАЗВОРОТ'),'CLOSE':('CLOSED','ЗАКРЫТА')}
    icon={'OPEN':'🟢' if d.get('side')=='LONG' else '🔴','ADD':'➕','REDUCE':'➖','REVERSE':'🔄','CLOSE':'✅'}[category]
    rows=[f"{icon} {d.get('symbol','')} · {d.get('side','')} {tr(*words[category])}",mode,'']
    if category=='REVERSE':rows.append(f"{d.get('previous_side','')} → {d.get('side','')}")
    fields=[('entry','Entry','Вход','$'),('price','Exit' if category=='CLOSE' else 'Price','Выход' if category=='CLOSE' else 'Цена','$'),
        ('filled_size','Filled size','Исполненный объём',''),('margin_estimate','Margin (estimate)','Маржа (оценка)','$'),
        ('leverage','Leverage','Плечо','×'),('reduced_pct','Reduced','Сокращено','%'),('remaining_size','Remaining size','Осталось',''),
        ('net_pnl','Net PnL','Чистый PnL','$'),('gross_pnl','Gross PnL (before fees)','PnL до комиссий','$')]
    for key,a,b,unit in fields:
        value=d.get(key)
        if not isinstance(value,(float,int)) or isinstance(value,bool) or not math.isfinite(value):continue
        if key=='gross_pnl' and d.get('net_pnl') is not None:continue
        number=f'{value:,.2f}' if unit=='$' else f'{value:.6g}'
        rows.append(f"{tr(a,b)}  {('$'+number) if unit=='$' else number+unit}")
    duration=d.get('duration_ms')
    if isinstance(duration,(float,int)) and math.isfinite(duration) and duration>=0:
        minutes=int(duration/60000);rows.append(tr('Duration','Длительность')+f'  {minutes//60}h {minutes%60}m')
    confidence=d.get('confidence')
    if isinstance(confidence,(float,int)) and 0<=confidence<=1:rows.append(tr('Consensus','Консенсус')+f'  {confidence:.0%}')
    sources={'AUTONOMOUS':('Autonomous AI','Автономный AI'),'COPY':('Copy','Копирование'),'MANUAL_COPY':('Manual Copy','Ручное копирование'),'CONFIRMED_USER':('User confirmed','Подтверждено пользователем')}
    rows.extend(['',tr('Source','Источник')+'  '+tr(*sources.get(d.get('source'),('Confirmed action','Подтверждённое действие')))])
    for wallet in d.get('leaders',[])[:3]:
        if re.fullmatch(r'0x[0-9a-f]{40}',wallet):rows.append(tr('Leader','Лидер')+'  '+wallet[:6]+'…'+wallet[-4:])
    if d.get('reverse_incomplete'):rows.append(tr('Old side closed. Opposite entry was not completed.','Прежняя позиция закрыта. Вход в другую сторону не выполнен.'))
    return '\n'.join(rows)
