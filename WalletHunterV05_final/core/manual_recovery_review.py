"""Read-only operator assessment, never a grant or reservation release.

The present canonical gateway holds an entire scope for an UNKNOWN execution.
Arithmetic headroom is not an isolation proof. A generation ID cannot change
that fact. This assessment deliberately has no enable/retry/terminal-write API.
"""
import hashlib
import json
import math
from core.foundation.contracts import PortfolioSnapshot
from core.foundation.store import scope_key
from core.execution_quarantine import active_in


def assess(db, scope, portfolio, allocation_limit, now):
    result=dict(version=1,state='OPERATOR_REVIEW_REQUIRED',new_operations_allowed=False,
        generation_status='NOT_CREATED',retry_allowed=False,available_for_new=None,
        arithmetic_headroom=None,committed=None,reserved=None,quarantined_reserve=None,
        allocation_limit=allocation_limit if type(allocation_limit) in (int,float) and math.isfinite(allocation_limit) and allocation_limit>=0 else None,reasons=[],checked_ms=now)
    reasons=result['reasons'];key=scope_key(scope)
    try:
        p=PortfolioSnapshot.model_validate(portfolio)
        if (p.scope!=scope or p.evidence!='EXCHANGE' or p.completeness!='COMPLETE'
                or type(p.exchange_ms) is not int or not 0<=now-p.exchange_ms<120000
                or not 0<=now-p.received_ms<120000):raise ValueError('ACCOUNT_EVIDENCE_STALE_OR_SCOPE_UNKNOWN')
        if type(allocation_limit) not in (int,float) or not math.isfinite(allocation_limit) or allocation_limit<0:
            raise ValueError('ALLOCATION_UNAVAILABLE')
        result['account_balance']=p.equity
        result['account_capacity']=p.available_collateral
        result['account_evidence_ms']=p.exchange_ms
        if p.positions:reasons.append('POSITION_ATTRIBUTION_REVIEW_REQUIRED')
        if p.orders:reasons.append('OPEN_ORDER_REVIEW_REQUIRED')
        quarantines=active_in(db,scope)
        if not quarantines:raise ValueError('NO_HISTORICAL_QUARANTINE')
        held=[];capacity=[];parents=set();known=set()
        for q in quarantines:
            row=db.execute('SELECT body,status,reservation FROM intents WHERE id=? AND scope=?',(q['intent_id'],key)).fetchone()
            parent=db.execute('SELECT account,intent FROM operations WHERE id=?',(q['parent_id'],)).fetchone()
            if (not row or not parent or parent[0]!=scope.account or row[1]!='UNKNOWN'
                    or q['state']!='QUARANTINED_UNKNOWN' or q['retry_allowed'] is not False
                    or q['scope']!=scope.model_dump(mode='json')
                    or hashlib.sha256(row[0].encode()).hexdigest()!=q['intent_sha256']
                    or hashlib.sha256(parent[1].encode()).hexdigest()!=q['parent_sha256']
                    or json.loads(row[2])!=q['reservation']):raise ValueError('QUARANTINE_IDENTITY_MISMATCH')
            r=q['reservation']
            if r['intent_id']!=q['intent_id'] or any(type(r.get(k)) not in (int,float) or not math.isfinite(r[k]) or r[k]<0 for k in ('margin','account_capacity')) or r['account_capacity']<r['margin']:
                raise ValueError('QUARANTINE_RESERVATION_INVALID')
            held.append(r['margin']);capacity.append(r['account_capacity']);parents.add(q['parent_id']);known.add(q['intent_id'])
        pending=list(db.execute("SELECT id FROM intents WHERE scope=? AND status IN ('SUBMITTING','UNKNOWN','PARTIAL')",(key,)))
        extra=[r[0] for r in pending if r[0] not in known]
        parent_extra=[r[0] for r in db.execute("SELECT id FROM operations WHERE account=? AND status IN ('PREPARED','UNKNOWN')",(scope.account,)) if r[0] not in parents]
        grants=db.execute('SELECT count(*) FROM grants WHERE scope=?',(key,)).fetchone()[0]
        result.update(outstanding_grants=grants,unclassified_pending=len(extra)+len(parent_extra),historical_count=len(known),
            quarantined_reserve=math.fsum(held),quarantined_capacity=math.fsum(capacity))
        if grants:reasons.append('OUTSTANDING_LIVE_GRANT')
        if extra or parent_extra:reasons.append('OTHER_UNRESOLVED_EXECUTION')
        # A fresh exchange-flat account is valid zero, not a missing value.
        # Parent and child are one reservation: never subtract both.
        if not p.positions and not p.orders and not extra and not parent_extra:
            result.update(committed=0.,reserved=0.,arithmetic_headroom=max(0.,min(
                allocation_limit-result['quarantined_reserve'],p.available_collateral-result['quarantined_capacity'])))
        reasons.append('CANONICAL_SCOPE_UNRESOLVED_EXECUTION')
        result['isolation_proof']='UNAVAILABLE'
    except (ValueError,KeyError,TypeError,OverflowError):
        reasons.append('ACCOUNT_OR_RESERVATION_EVIDENCE_UNAVAILABLE')
    return result
