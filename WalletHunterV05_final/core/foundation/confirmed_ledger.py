"""Read-through confirmed-route accounting; no second reservation store."""
import json
import math
from contextlib import closing


def signed_weight(row):
    # Two existing journal schemas, not inferred provenance. If both are
    # present they must agree; malformed legacy data remains reconciliation.
    value=float(row['signed_notional'] if 'signed_notional' in row else row['signed'])
    if not math.isfinite(value) or value==0:raise ValueError('attribution')
    if 'signed_notional' in row and 'signed' in row and not math.isclose(value,float(row['signed']),rel_tol=1e-8):
        raise ValueError('conflicting attribution')
    return value


class ConfirmedLedger:
    def __init__(self, portfolio, journal, operation, source, allocation_limit, expected_position=None, owned_order_ids=()):
        self.portfolio = portfolio
        self.expected_position = expected_position
        self.owned_order_ids = tuple(str(x) for x in owned_order_ids)
        self.errors = []
        self.available_source = 0.
        with closing(journal.connect()) as db:
            pending = db.execute("SELECT id,intent FROM operations WHERE account=? AND id<>? AND status IN ('PREPARED','UNKNOWN')",
                                 (portfolio.scope.account,operation)).fetchall()
        # Pending envelopes with unavailable allocation are not zero capital.
        self.pending_conflict = bool(pending)
        try:
            limit = float(allocation_limit)
            if not math.isfinite(limit) or limit < 0: raise ValueError('allocation')
            owned = journal.owned(portfolio.scope.account)
            committed = 0.
            for p in portfolio.positions:
                record = owned.get(p.instrument.market_key, {})
                old = record.get('position') or {}
                if not (record.get('managed') and record.get('network') == portfolio.scope.network
                        and record.get('side') == p.side and math.isclose(float(record.get('size',-1)),p.size,rel_tol=1e-8)
                        and math.isclose(float(old.get('entry_price',-1)),p.entry_price,rel_tol=1e-8)):
                    self.errors.append('OWNERSHIP_UNPROVEN'); continue
                weights = record.get('source_targets') or []
                total = math.fsum(abs(signed_weight(x)) for x in weights)
                share = math.fsum(abs(signed_weight(x)) for x in weights if x['wallet'] == source)
                if not math.isfinite(total) or total <= 0 or p.margin is None: raise ValueError('attribution')
                committed += p.margin*share/total
            self.available_source = max(0.,limit-committed)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            self.errors.append('ALLOCATION_UNKNOWN')

    def matches_position(self, position):
        p = self.expected_position
        if not isinstance(p,dict): return False
        try:
            return (p['side'] == position.side and p.get('dex','') == position.instrument.dex
                    and p['coin'].split(':')[-1] == position.instrument.symbol
                    and math.isclose(float(p['size']),position.size,rel_tol=1e-9)
                    and math.isclose(float(p['entry_price']),position.entry_price,rel_tol=1e-9)
                    and float(p['leverage']) == position.leverage)
        except (KeyError, TypeError, ValueError): return False


def project_receipt_in(db,intent,before,after,receipt):
    """Preserve old source weights; a manual reduction never adopts exposure."""
    import time
    from .contracts import Contribution
    key=intent.instrument.market_key
    old=db.execute('SELECT record FROM ownership WHERE account=? AND market=?',(intent.scope.account,key)).fetchone()
    record=json.loads(old[0]) if old else None
    b=next((p for p in before.positions if p.instrument==intent.instrument),None)
    a=next((p for p in after.positions if p.instrument==intent.instrument),None)
    if intent.action=='OPEN' and b is None:
        record={'strategy':'CONFIRMED_AI','source_targets':[{'wallet':intent.source,'signed_notional':a.notional*(1 if a.side=='LONG' else -1),'margin':a.margin}]}
    else:
        if not record or record.get('network')!=intent.scope.network or not record.get('managed') or b is None:return after
        saved=record.get('position') or {}
        if not (record.get('side')==b.side and float(record.get('size',-1))==b.size and float(saved.get('entry_price',-1))==b.entry_price):return after
        total=math.fsum(abs(signed_weight(x)) for x in record['source_targets'])
        if a:
            weights=[]
            for x in record['source_targets']:
                share=abs(signed_weight(x))/total
                weight=dict(x,signed_notional=share*a.notional*(1 if a.side=='LONG' else -1),margin=share*a.margin)
                if 'signed' in x:weight['signed']=weight['signed_notional']
                weights.append(weight)
            record['source_targets']=weights
    position=None if a is None else dict(coin=key.split('|')[0],dex=intent.instrument.dex,side=a.side,size=a.size,
        entry_price=a.entry_price,leverage=a.leverage,position_value=a.notional,margin_used=a.margin,snapshot_started_ms=after.received_ms)
    record.update(managed=a is not None,size=a.size if a else 0.,side=a.side if a else b.side,position=position,
        network=intent.scope.network,verified_at_ms=after.received_ms,
        execution_evidence={'intent_id':intent.intent_id,'order_ids':list(receipt.order_ids),'trade_ids':[f.trade_id for f in receipt.fills],'network':intent.scope.network})
    db.execute('INSERT OR REPLACE INTO ownership VALUES(?,?,?,?,?)',(intent.scope.account,key,time.time(),intent.parent_intent_id,json.dumps(record)))
    if a:
        proven=a.model_copy(update={'evidence':'VERIFIED','order_ids':receipt.order_ids,
            'contributions':tuple(Contribution(source=x['wallet'],notional=abs(float(x['signed_notional']))) for x in record['source_targets'])})
        after=after.model_copy(update={'positions':tuple(proven if p.instrument==a.instrument else p for p in after.positions)})
    return after
