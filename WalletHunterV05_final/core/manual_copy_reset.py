"""Explicit operator capital administration, NOT exchange reconciliation.

Archived rows retain their original UNKNOWN receipt, reservation evidence and
intent bytes. Only the operational classification changes. Immutable SQL
tombstones prevent all future writers (including old recovery code) reopening
the same identity. No public HTTP route or worker invokes this procedure.
"""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from core.foundation.contracts import OrderIntent
from core.foundation.store import scope_key

STATE = 'ARCHIVED_UNRESOLVED'
REASON = 'USER_REQUESTED_MANUAL_COPY_RESET'


def history(db, scope):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='manual_copy_abandonments'").fetchone():
        return []
    return [json.loads(r[0]) for r in db.execute(
        'SELECT body FROM manual_copy_abandonments WHERE scope=? ORDER BY created_ms', (scope_key(scope),))]


def collect_evidence(client, intent, clock=lambda: int(time.time()*1000)):
    """Read-only client only; negative lookup is NOT proof of non-submission."""
    from core.foundation.data import account_snapshot
    from core.foundation.live_reconciliation import client_order_id
    if (getattr(client, 'exchange', False) is not None or client.network != intent.scope.network
            or client.address.lower() != intent.scope.account):
        raise ValueError('READ_ONLY_SCOPED_CLIENT_REQUIRED')
    start = max(0, intent.created_ms-60000)
    status = client.query_order_by_cloid(client_order_id(intent))
    orders = {dex: client.frontend_open_orders(dex) for dex in ('', 'xyz')}
    historical = client.info.historical_orders(intent.scope.account)
    fills = client.info.user_fills_by_time(intent.scope.account, start, clock())
    if (any(not isinstance(v, list) for v in orders.values())
            or not isinstance(historical, list) or not isinstance(fills, list)):
        raise ValueError('EXCHANGE_HISTORY_UNAVAILABLE')
    # Require no recent account orders/fills, not just an unmatched local ID.
    if any(not isinstance(x, dict) or not isinstance(x.get('order'), dict)
           or type(x['order'].get('timestamp')) is not int for x in historical):
        raise ValueError('MALFORMED_ORDER_HISTORY')
    recent = [x for x in historical if x['order']['timestamp'] >= start]
    # Account history is shared by all instruments.  An unrelated BTC/ETH
    # order must not make a SOL UNKNOWN intent impossible to review.  Keep the
    # complete history for audit, but also persist an instrument-scoped view
    # used by the abandonment guard below.
    symbol = intent.instrument.symbol
    dex = intent.instrument.dex or ''
    def relevant_order(row):
        order = row.get('order') if isinstance(row, dict) else None
        if not isinstance(order, dict):
            return True
        coin = str(order.get('coin', '')).split(':')[-1]
        order_dex = order.get('dex') or ''
        return coin == symbol and order_dex == dex
    relevant_recent = [x for x in recent if relevant_order(x)]
    relevant_fills = [x for x in fills
                      if str(x.get('coin', '')).split(':')[-1] == symbol
                      and (x.get('dex') or '') == dex]
    portfolio = account_snapshot(client, intent.scope, 1, clock, dex='')
    return dict(scope=intent.scope.model_dump(mode='json'), intent_id=intent.intent_id,
        cloid=client_order_id(intent), cloid_result=status, open_orders=orders,
        recent_orders=recent, relevant_recent_orders=relevant_recent,
        fills=fills, relevant_fills=relevant_fills,
        portfolio=portfolio.model_dump(mode='json'),
        checked_ms=clock(), financial_outcome='UNKNOWN', leverage_mutation='UNKNOWN')


def abandon(path, intent_id, *, operator, operator_requested_abandonment,
            evidence, now_ms, reason):
    """Call under the account lock with its writer stopped. Idempotent by ID.

    Scope operator identity must match the tenant. Only a flat, paused account
    without grants or other unresolved operations is eligible. Absence of
    exchange evidence permits the USER's administrative release; it does not
    establish a rejected or unsubmitted order.
    """
    from core.execution_quarantine import active_in
    from core.foundation.contracts import PortfolioSnapshot
    if operator_requested_abandonment is not True or reason != REASON:
        raise ValueError('EXPLICIT_OPERATOR_ABANDONMENT_REQUIRED')
    with closing(sqlite3.connect(path, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM intents WHERE id=?', (intent_id,)).fetchone()
        if not row: raise ValueError('EXACT_INTENT_REQUIRED')
        intent = OrderIntent.model_validate_json(row['body'])
        if str(operator) != intent.scope.tenant: raise ValueError('OPERATOR_SCOPE_MISMATCH')
        previous = next((a for a in history(db, intent.scope) if a['intent_id']==intent_id), None)
        if previous: return previous  # Must not erase a subsequently created generation.
        q = next((q for q in active_in(db, intent.scope) if q['intent_id']==intent_id), None)
        parent = db.execute('SELECT * FROM operations WHERE id=?', (intent.parent_intent_id,)).fetchone()
        if (row['status']!='UNKNOWN' or not q or not parent
                or q['intent_sha256']!=hashlib.sha256(row['body'].encode()).hexdigest()
                or q['parent_sha256']!=hashlib.sha256(parent['intent'].encode()).hexdigest()
                or q['reservation']!=json.loads(row['reservation'])):
            raise ValueError('QUARANTINE_IDENTITY_CHANGED')
        portfolio = PortfolioSnapshot.model_validate(evidence['portfolio'])
        if (evidence.get('intent_id')!=intent_id or evidence.get('scope')!=intent.scope.model_dump(mode='json')
                or portfolio.scope!=intent.scope or portfolio.evidence!='EXCHANGE'
                or portfolio.completeness!='COMPLETE' or portfolio.equity is None
                or portfolio.available_collateral is None or portfolio.positions or portfolio.orders
                or any(type(t) is not int or not 0<=now_ms-t<=30000 for t in
                    (evidence.get('checked_ms'), portfolio.received_ms, portfolio.exchange_ms))
                or evidence.get('cloid_result')!={'status':'unknownOid'}
                or evidence.get('open_orders')!={'':[], 'xyz':[]}
                or (evidence.get('relevant_recent_orders', evidence.get('recent_orders')) != [])
                or (evidence.get('relevant_fills', evidence.get('fills')) != [])):
            raise ValueError('FRESH_FLAT_ACCOUNT_EVIDENCE_REQUIRED')
        if db.execute('SELECT 1 FROM grants WHERE scope=?',(scope_key(intent.scope),)).fetchone():
            raise ValueError('OUTSTANDING_LIVE_GRANT')
        if db.execute("SELECT 1 FROM intents WHERE scope=? AND id<>? AND status IN ('UNKNOWN','SUBMITTING','PARTIAL')",
                      (scope_key(intent.scope), intent_id)).fetchone(): raise ValueError('OTHER_UNRESOLVED_INTENT')
        if db.execute("SELECT 1 FROM operations WHERE account=? AND id<>? AND status IN ('UNKNOWN','PREPARED')",
                      (intent.scope.account, parent['id'])).fetchone(): raise ValueError('OTHER_PENDING_OPERATION')
        key = intent.scope.model_dump_json()
        config = db.execute('SELECT body FROM manual_leader_configs WHERE scope=?',(key,)).fetchone()
        if not config or json.loads(config[0])['enabled']: raise ValueError('PAUSE_FIRST')
        runtime = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='manual_copy_runtime'").fetchone():
            runtime = db.execute('SELECT body FROM manual_copy_runtime WHERE scope=?',(key,)).fetchone()
        audit = dict(version=1,state=STATE,intent_id=intent_id,parent_id=parent['id'],
            scope=intent.scope.model_dump(mode='json'),created_ms=now_ms,operator=str(operator),
            operator_requested_abandonment=True,reason=reason,financial_outcome='UNKNOWN',
            retry_allowed=False,capital_administration='RELEASED_BY_OPERATOR',
            released_margin=q['reservation']['margin'],released_capacity=q['reservation']['account_capacity'],
            original_intent=dict(row),original_operation=dict(parent),quarantine=q,
            retired_config=json.loads(config[0]),retired_runtime=json.loads(runtime[0]) if runtime else None,
            evidence=evidence,reset_epoch=intent_id+':'+str(now_ms))
        db.execute('CREATE TABLE IF NOT EXISTS manual_copy_abandonments(intent_id TEXT PRIMARY KEY,scope TEXT NOT NULL,created_ms INTEGER NOT NULL,body TEXT NOT NULL)')
        db.execute('INSERT INTO manual_copy_abandonments VALUES(?,?,?,?)',
                   (intent_id,scope_key(intent.scope),now_ms,json.dumps(audit,sort_keys=True,allow_nan=False)))
        # Operational classification only. Receipt/outcome/intent/reservation
        # payloads stay byte-identical and are preserved in the audit as well.
        db.execute('UPDATE intents SET status=? WHERE id=?',(STATE,intent_id))
        db.execute('UPDATE operations SET status=? WHERE id=?',(STATE,parent['id']))
        db.execute('DELETE FROM manual_leader_configs WHERE scope=?',(key,))
        if runtime:
            db.execute('UPDATE manual_copy_runtime SET body=? WHERE scope=?',(json.dumps(dict(
                status='OFF',heartbeat_ms=now_ms,account_evidence=evidence['portfolio'],
                committed=0.,reserved_unknown=False,available=0.,allocation_limit=0.,
                allocatable_capital=portfolio.sizing_capital,monitored=[],reset_epoch=audit['reset_epoch'])),key))
        # Tombstones protect against stale queue/old binaries/operator mistakes.
        for table, field, lookup in [('intents','id','intent_id'),('operations','id',"json_extract(body,'$.parent_id')"),
                                     ('execution_quarantines','intent_id','intent_id'),
                                     ('intent_prestate','id','intent_id')]:
            for action in ('UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS retired_{table}_{action} BEFORE {action} ON {table} "
                    f"WHEN OLD.{field} IN (SELECT {lookup} FROM manual_copy_abandonments) "
                    "BEGIN SELECT RAISE(ABORT,'ARCHIVED_EXECUTION_IMMUTABLE'); END")
        for action in ('UPDATE','DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS abandonment_{action} BEFORE {action} ON manual_copy_abandonments "
                       "BEGIN SELECT RAISE(ABORT,'OPERATOR_AUDIT_IMMUTABLE'); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS retired_grant BEFORE INSERT ON grants "
                   "WHEN NEW.id IN (SELECT intent_id FROM manual_copy_abandonments) "
                   "BEGIN SELECT RAISE(ABORT,'ARCHIVED_EXECUTION_IMMUTABLE'); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS retired_parent BEFORE INSERT ON intents "
                   "WHEN json_extract(NEW.body,'$.parent_intent_id') IN "
                   "(SELECT json_extract(body,'$.parent_id') FROM manual_copy_abandonments) "
                   "BEGIN SELECT RAISE(ABORT,'ARCHIVED_EXECUTION_IMMUTABLE'); END")
        db.commit()
        return audit
