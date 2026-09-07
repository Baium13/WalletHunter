"""Canonical route bridge for explicitly authorized manual/AI actions.

When a gateway context is supplied, all actions are represented as immutable
OrderIntent records and dispatched through RiskGateway/ExecutionGateway. The
small compatibility branch is retained for old isolated unit fixtures only;
normal application callers must provide ``context``.
"""

import time
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

def manual_close(client, coin, dex, *, gateway=None, intent=None, market=None):
    if gateway is not None:
        if intent is None or market is None: raise ValueError('Canonical manual intent required')
        return gateway.execute(intent, market)
    # Isolated legacy fixtures only. Production callers pass gateway + intent.
    return client.market_close(coin, dex)

def confirmed_ai_order(signer, payload, expires_ms, *, gateway=None, intent=None, market=None):
    if gateway is not None:
        if intent is None or market is None: raise ValueError('Canonical AI intent required')
        return gateway.execute(intent, market)
    return signer.submit_user_ioc(payload['coin'], payload['direction'] == 'LONG', payload['size'],
        payload['limit_price'], payload['leverage'], payload['cloid'], expires_ms=expires_ms)

def confirmed_ai_position(signer, payload, expires_ms, *, gateway=None, intent=None, market=None):
    if gateway is not None:
        if intent is None or market is None: raise ValueError('Canonical AI intent required')
        return gateway.execute(intent, market)
    return signer.submit_position_ioc(payload['coin'], payload['is_buy'], payload['size'], payload['limit_price'],
        payload['reduce_only'], payload['cloid'], payload['dex'], expires_ms=expires_ms,
        expected_position=payload.get('expected_position'))


def execute_manual_request(context, *, coin, dex='', side='SELL', size, price, action='CLOSE', source='manual'):
    """Canonical manual route. No signer/client is accepted above gateway."""
    intent=_intent(context,action=action,coin=coin,dex=dex,side=side,size=size,price=price,source=source)
    if intent is None: raise ValueError('Canonical execution context required')
    return _canonical(context,intent,context['market'])


def execute_confirmed_ai(context, *, coin, dex='', side='BUY', size, price, action='OPEN', source='ai'):
    intent=_intent(context,action=action,coin=coin,dex=dex,side=side,size=size,price=price,source=source)
    if intent is None: raise ValueError('Canonical execution context required')
    return _canonical(context,intent,context['market'])
