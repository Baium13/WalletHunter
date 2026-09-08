"""Existing product confirmation -> shared journal -> canonical execution.

No context construction authorizes execution. A persisted action/proposal is
bound to exact intent bytes before the gateway may grant it once.
"""
import json
import math
import time
from core.foundation.contracts import Scope, InstrumentId, OrderIntent, MarketSnapshot
from core.foundation.store import Store, digest


class CanonicalContextRequired(ValueError):
    pass


def build_context(client, *, tenant, coin, dex='', action='OPEN', source='manual',
                  settings=None, profile=None, journal=None, request=None, store_path=None):
    from core.foundation.data import account_snapshot
    from core.foundation.risk import RiskGateway, RiskPolicy
    from core.foundation.execution import ExecutionGateway
    from core.foundation.copy_execution import HyperliquidExecutionAdapter
    if settings is None or journal is None or profile is None:
        raise CanonicalContextRequired('Configured policy and shared journal required')
    address=str((profile.get('account') or {}).get('address','')).lower()
    scope=Scope(tenant=str(tenant),account=address,network=client.network)
    if client.address.lower()!=address or settings.hl_mode!=scope.network: raise ValueError('Account/network mismatch')
    request=dict(request or {})
    clock=lambda:int(time.time()*1000)
    store=Store(journal.path)
    try: revision=store.portfolio(scope).revision+1
    except ValueError: revision=1
    before=account_snapshot(client,scope,revision,clock,dex=dex,
        require_collateral=action not in {'REDUCE','CLOSE','PLACE_STOP','CANCEL_OWNED'})
    instrument=InstrumentId(network=scope.network,dex=dex,symbol=coin.split(':')[-1])
    now=clock()
    try:
        book=client.info.post('/info',{'type':'l2Book','coin':instrument.market_key.split('|')[0]})
        bid,ask=float(book['levels'][0][0]['px']),float(book['levels'][1][0]['px'])
        market=MarketSnapshot(instrument=instrument,exchange_ms=book['time'],received_ms=now,price=(bid+ask)/2,
            bid=bid,ask=ask,completeness='COMPLETE',freshness='FRESH',source='REST',source_version='confirmed-l2-v1')
    except Exception:
        price=None
        if action in {'REDUCE','CLOSE','PLACE_STOP'}:
            try:
                value=float(client.mid(coin,dex))
                if math.isfinite(value) and value>0: price=value
            except Exception: pass
        market=MarketSnapshot(instrument=instrument,exchange_ms=None,received_ms=now,price=price,bid=None,ask=None,
            completeness='UNKNOWN',freshness='FRESH',source='REST',source_version='confirmed-unavailable-v1')
    # Fresh AI proposal validation supplies its metadata-approved leverage cap.
    cap=int(request.get('approved_leverage_cap',settings.max_leverage))
    slip=min(float(settings.max_slippage_pct),float(request.get('slippage_pct',settings.max_slippage_pct)))
    policy=RiskPolicy(scope=scope,instrument=instrument,sources=(source,),enabled=bool(profile.get('account')),
        max_leverage=cap,min_notional=10.,max_notional=float(settings.max_total_exposure_usd),
        max_symbol_notional=float(settings.max_total_exposure_usd),max_total_notional=float(settings.max_total_exposure_usd),
        max_slippage_pct=slip,max_price_deviation_pct=float(settings.entry_price_tolerance_pct),fee_buffer_pct=.1,
        size_step=float(client.size_step(coin,dex)),max_market_age_ms=30000,max_portfolio_age_ms=30000,max_intent_age_ms=30000)
    gateway=ExecutionGateway(store,RiskGateway(policy),HyperliquidExecutionAdapter(client,scope,clock,before),clock)
    return dict(gateway=gateway,store=store,journal=journal,scope=scope,market=market,before=before,policy=policy,now=now,
        request=request,round_price=lambda price:client.round_price(coin,price,dex))


def _execute(context, *, coin, dex, side, size, price, action, source, identity=None,
             expected_position=None, owned_order_id=None, owned_order_ids=(), operation_id=None,
             expires_ms=None, leverage=None, allocation_limit=0., exchange_client_id=None):
    request=dict(coin=coin,dex=dex,side=side,size=size,price=price,action=action,source=source,
        identity=identity,expected_position=expected_position,owned_order_ids=owned_order_ids,
        owned_order_id=owned_order_id,operation_id=operation_id,expires_ms=expires_ms,
        leverage=leverage,allocation_limit=allocation_limit,exchange_client_id=exchange_client_id)
    ctx=context(request) if callable(context) else context
    if not isinstance(ctx,dict) or 'journal' not in ctx: raise CanonicalContextRequired('Complete confirmation context required')
    request={**ctx.get('request',{}),**{k:v for k,v in request.items() if v is not None}}
    identity=request.get('identity')
    if not identity: raise ValueError('Persisted action/proposal identity required')
    now,scope,market=ctx['now'],ctx['scope'],ctx['market']
    lev=int(request.get('leverage') or (expected_position or {}).get('leverage',1))
    if price is None:
        if market.price is None: raise ValueError('Price unavailable')
        raw=market.price*(1+ctx['policy'].max_slippage_pct/100 if side=='BUY' else 1-ctx['policy'].max_slippage_pct/100)
        price=ctx['round_price'](raw)
        if (price>raw if side=='BUY' else price<raw): price=ctx['round_price'](market.price)
    journal=ctx['journal']; parent=request.get('operation_id')
    if parent is None:
        parent=journal.prepare(scope.account,market.instrument.market_key,dict(action=action,network=scope.network,
            action_id=identity,before=expected_position))
    intent=OrderIntent(version=4,intent_id=str(identity)+'-'+action.lower()+(('-'+str(owned_order_id)) if owned_order_id else ''),
        parent_intent_id=parent,correlation_id=str(identity),scope=scope,instrument=market.instrument,source=source,
        action=action,side=side,size=float(size),limit_price=float(price),leverage=lev,
        slippage_pct=ctx['policy'].max_slippage_pct,authorization='USER_CONFIRMED',execution_mode='LIVE',
        created_ms=now,expires_ms=min(now+30000,int(request.get('expires_ms') or now+30000)),configure_leverage=action=='OPEN',
        owned_order_id=str(owned_order_id) if owned_order_id else None,exchange_client_id=request.get('exchange_client_id'),
        order_type={'PLACE_STOP':'STOP_MARKET','CANCEL_OWNED':'CANCEL','LEVERAGE_UPDATE':'LEVERAGE'}.get(action,'IOC'))
    from core.foundation.confirmed_ledger import ConfirmedLedger
    ledger=ConfirmedLedger(ctx['before'],journal,parent,source,request.get('allocation_limit',0.),expected_position,owned_order_ids)
    with ctx['store'].transaction() as db:
        row=db.execute('SELECT account,status,intent FROM operations WHERE id=?',(parent,)).fetchone()
        envelope=json.loads(row['intent']) if row else {}
        if not row or row['account']!=scope.account or row['status']!='PREPARED' or envelope.get('network')!=scope.network:
            raise ValueError('Pending confirmed operation scope mismatch')
        old=envelope.setdefault('confirmed_intents',{}).get(intent.intent_id)
        if old and old!=digest(intent): raise ValueError('Existing action requires query recovery, not resubmission')
        envelope['confirmed_intents'][intent.intent_id]=digest(intent)
        envelope['confirmed_tenant']=scope.tenant
        db.execute('UPDATE operations SET intent=? WHERE id=?',(json.dumps(envelope),parent))
        ctx['store'].publish_portfolio_in(db,ctx['before'],str(identity))
    ctx['gateway'].authorize_confirmed(intent)
    receipt=ctx['gateway'].execute(intent,market,copy_ledger=ledger)
    if operation_id is None:
        journal.finish(parent,{'ok':receipt.status in {'FILLED','CONFIGURED'},'status':receipt.status,
            'execution_evidence':{'intent_id':receipt.intent_id,'network':scope.network}})
    return receipt


def execute_manual_request(context, *, coin, dex='', side='SELL', size, price=None, action='CLOSE', source='manual', **kwargs):
    return _execute(context,coin=coin,dex=dex,side=side,size=size,price=price,action=action,source=source,**kwargs)


def execute_confirmed_ai(context, *, coin, dex='', side='BUY', size, price, action='OPEN', source='ai', **kwargs):
    return _execute(context,coin=coin,dex=dex,side=side,size=size,price=price,action=action,source=source,**kwargs)


def manual_close(client, coin, dex, *, gateway=None, intent=None, market=None):
    if gateway is None or intent is None or market is None: raise CanonicalContextRequired('Canonical manual context required')
    return gateway.execute(intent,market)


def confirmed_ai_order(signer,payload,expires_ms,*,gateway=None,intent=None,market=None):
    if gateway is None or intent is None or market is None: raise CanonicalContextRequired('Canonical AI context required')
    return gateway.execute(intent,market)


confirmed_ai_position=confirmed_ai_order
