"""Canonical route bridge for explicitly authorized manual/AI actions.

When a gateway context is supplied, all actions are represented as immutable
OrderIntent records and dispatched through RiskGateway/ExecutionGateway. The
small compatibility branch is retained for old isolated unit fixtures only;
normal application callers must provide ``context``.
"""

import time
import math
from core.foundation.contracts import Scope, InstrumentId, OrderIntent


def _intent(context, *, action, coin, dex, side, size, price, authorization='USER_CONFIRMED', source='manual'):
    if not isinstance(context, dict) or not context.get('gateway') or not context.get('risk'):
        return None
    scope=context.get('scope')
    if not isinstance(scope, Scope): raise ValueError('Canonical scope required')
    now=int(context.get('now', int(time.time()*1000)))
    instrument=InstrumentId(network=scope.network,dex=dex or '',symbol=str(coin).split(':')[-1])
    return OrderIntent(version=3,intent_id=str(context.get('intent_id') or context.get('correlation_id') or 'route-intent'),
        scope=scope,instrument=instrument,source=source,action=action,side=side,size=float(size),
        limit_price=float(price),leverage=int(context.get('leverage',1)),slippage_pct=float(context.get('slippage_pct',1.0)),
        authorization=authorization,execution_mode='LIVE',correlation_id=str(context.get('correlation_id') or 'route'),
        created_ms=now,expires_ms=int(context.get('expires_ms',now+30000)))


def _canonical(context, intent, market):
    decision=context['risk'].evaluate(intent, market, context['ledger'], context['now'], authorized=True)
    if decision.outcome != 'APPROVED': return decision
    kwargs={'copy_ledger': context.get('ledger')} if context.get('ledger') is not None else {}
    return context['gateway'].execute(intent, market, **kwargs)


def build_context(client, *, tenant, coin, dex='', action='OPEN', source='manual', store_path=None):
    """Compose existing live evidence, ledger, risk and gateway services."""
    from core.foundation.data import account_snapshot
    from core.foundation.store import Store
    from core.foundation.ledger import Ledger
    from core.foundation.risk import RiskGateway, RiskPolicy
    from core.foundation.execution import ExecutionGateway
    from core.foundation.copy_execution import HyperliquidExecutionAdapter
    now=int(time.time()*1000)
    scope=Scope(tenant=str(tenant),account=str(client.address).lower(),network=client.network)
    instrument=InstrumentId(network=scope.network,dex=dex or '',symbol=str(coin).split(':')[-1])
    before=account_snapshot(client,scope,1,lambda:now,dex=dex or '',require_collateral=action not in {'REDUCE','CLOSE'})
    price=float(client.mid(coin,dex or ''))
    if not math.isfinite(price) or price<=0: raise ValueError('Price unavailable')
    from core.foundation.contracts import MarketSnapshot
    market=MarketSnapshot(instrument=instrument,exchange_ms=before.exchange_ms,received_ms=now,price=price,bid=price,ask=price,
        completeness='COMPLETE' if before.completeness=='COMPLETE' else 'UNKNOWN',freshness='FRESH',source='REST',source_version='route-mid')
    policy=RiskPolicy(scope=scope,instrument=instrument,sources=(source,),enabled=True,max_leverage=20,min_notional=10.,
        max_notional=100000.,max_symbol_notional=100000.,max_total_notional=100000.,max_slippage_pct=1.,max_price_deviation_pct=1.,
        fee_buffer_pct=.1,size_step=float(client.size_step(coin,dex or '')),max_market_age_ms=30000,max_portfolio_age_ms=30000,max_intent_age_ms=30000)
    path=store_path or getattr(client,'canonical_store_path',None)
    if not path: raise ValueError('Canonical store path required')
    store=Store(path); risk=RiskGateway(policy); ledger=Ledger(before,(source,),())
    adapter=HyperliquidExecutionAdapter(client,scope,lambda:int(time.time()*1000),before)
    gateway=ExecutionGateway(store,risk,adapter,lambda:int(time.time()*1000))
    return {'gateway':gateway,'risk':risk,'ledger':ledger,'market':market,'scope':scope,'now':now,
        'correlation_id':'route-'+str(now),'leverage':min(20,int(getattr(client,'max_leverage',20))),
        'slippage_pct':1.0,'store':store}

def manual_close(client, coin, dex, *, gateway=None, intent=None, market=None):
    if gateway is not None:
        if intent is None or market is None: raise ValueError('Canonical manual intent required')
        return gateway.execute(intent, market)
    # Isolated legacy fixtures only. Production callers pass gateway + intent.
    from core.legacy_route_calls import manual_close as legacy
    return legacy(client, coin, dex)

def confirmed_ai_order(signer, payload, expires_ms, *, gateway=None, intent=None, market=None):
    if gateway is not None:
        if intent is None or market is None: raise ValueError('Canonical AI intent required')
        return gateway.execute(intent, market)
    from core.legacy_route_calls import ai_order as legacy
    return legacy(signer, payload, expires_ms)

def confirmed_ai_position(signer, payload, expires_ms, *, gateway=None, intent=None, market=None):
    if gateway is not None:
        if intent is None or market is None: raise ValueError('Canonical AI intent required')
        return gateway.execute(intent, market)
    from core.legacy_route_calls import ai_position as legacy
    return legacy(signer, payload, expires_ms)


def execute_manual_request(context, *, coin, dex='', side='SELL', size, price, action='CLOSE', source='manual'):
    """Canonical manual route. No signer/client is accepted above gateway."""
    intent=_intent(context,action=action,coin=coin,dex=dex,side=side,size=size,price=price,source=source)
    if intent is None: raise ValueError('Canonical execution context required')
    return _canonical(context,intent,context['market'])


def execute_confirmed_ai(context, *, coin, dex='', side='BUY', size, price, action='OPEN', source='ai'):
    intent=_intent(context,action=action,coin=coin,dex=dex,side=side,size=size,price=price,source=source)
    if intent is None: raise ValueError('Canonical execution context required')
    return _canonical(context,intent,context['market'])
