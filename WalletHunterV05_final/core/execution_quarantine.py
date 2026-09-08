"""Operator quarantine overlays UNKNOWN; never changes a financial outcome.

No retry/release API. Explicit evidence reconciliation remains possible through
the canonical query-only gateway. Removing a quarantine requires a separately
reviewed operator procedure, even if later terminal evidence is obtained.
"""
import hashlib
import json
import sqlite3
import math
from contextlib import closing
from core.foundation.store import scope_key


def active_in(db, scope):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='execution_quarantines'").fetchone():
        return []
    return [json.loads(r[0]) for r in db.execute(
        'SELECT body FROM execution_quarantines WHERE scope=? ORDER BY intent_id', (scope_key(scope),))]


def require_unblocked(db, scope):
    if active_in(db, scope):
        raise ValueError('MANUAL_COPY_QUARANTINED_OPERATOR_REVIEW_REQUIRED')


def quarantine(path, intent_id, *, evidence_sha256, now_ms):
    """Atomic scope lock + immutable audit overlay; reservation is not rewritten."""
    from core.foundation.contracts import OrderIntent
    if len(evidence_sha256) != 64 or any(c not in '0123456789abcdef' for c in evidence_sha256):
        raise ValueError('Evidence digest required')
    with closing(sqlite3.connect(path, timeout=10)) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT body,status,reservation FROM intents WHERE id=?',(intent_id,)).fetchone()
        if not row or row[1]!='UNKNOWN':raise ValueError('UNKNOWN required')
        intent=OrderIntent.model_validate_json(row[0])
        parent=db.execute('SELECT account,status,intent FROM operations WHERE id=?',(intent.parent_intent_id,)).fetchone()
        if (intent.execution_mode!='LIVE' or not parent or parent[0]!=intent.scope.account
                or parent[1] not in {'PREPARED','UNKNOWN'}
                or json.loads(parent[2]).get('strategy')!='MANUAL_LEADER_COPY'
                or json.loads(parent[2]).get('network')!=intent.scope.network):
            raise ValueError('Manual copy identity required')
        config=db.execute('SELECT body FROM manual_leader_configs WHERE scope=?',(intent.scope.model_dump_json(),)).fetchone()
        if not config or json.loads(config[0])['enabled']:raise ValueError('Pause Manual Copy first')
        if db.execute('SELECT 1 FROM grants WHERE scope=?',(scope_key(intent.scope),)).fetchone():
            raise ValueError('Outstanding grant')
        reservation=json.loads(row[2])
        if reservation.get('intent_id')!=intent_id:raise ValueError('Reservation identity mismatch')
        if any(type(reservation.get(k)) not in (int,float) or not math.isfinite(reservation[k]) or reservation[k]<0
               for k in ('margin','account_capacity')):raise ValueError('Invalid reservation')
        body=dict(version=1,state='QUARANTINED_UNKNOWN',intent_id=intent_id,parent_id=intent.parent_intent_id,
            scope=intent.scope.model_dump(mode='json'),created_ms=now_ms,evidence_sha256=evidence_sha256,
            intent_sha256=hashlib.sha256(row[0].encode()).hexdigest(),reservation=reservation,
            parent_sha256=hashlib.sha256(parent[2].encode()).hexdigest(),
            retry_allowed=False,operator_review_required=True)
        db.execute('CREATE TABLE IF NOT EXISTS execution_quarantines(intent_id TEXT PRIMARY KEY,scope TEXT NOT NULL,body TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO execution_quarantines VALUES(?,?,?)',
            (intent_id,scope_key(intent.scope),json.dumps(body,sort_keys=True,allow_nan=False)))
        db.commit()
        return active_in(db,intent.scope)


def read_only_deployment_gate(db, profiles):
    """UI/notification/source deployment only; NEVER live authorization.

    Pending records must all have intact paused quarantines. Unclassified
    PREPARED, live grants, enabled copy or changed identity fail closed.
    """
    from core.foundation.contracts import OrderIntent
    if any(p.get('copy_enabled') for p in profiles.values()):raise ValueError('LEGACY_COPY_ENABLED')
    if any(json.loads(r[0])['enabled'] for r in db.execute('SELECT body FROM manual_leader_configs')):
        raise ValueError('MANUAL_COPY_ENABLED')
    if db.execute('SELECT 1 FROM grants').fetchone():raise ValueError('LIVE_GRANT')
    parents=set()
    for identity,body,status,reservation in db.execute("SELECT id,body,status,reservation FROM intents WHERE status IN ('SUBMITTING','UNKNOWN','PARTIAL')"):
        i=OrderIntent.model_validate_json(body)
        q=next((q for q in active_in(db,i.scope) if q['intent_id']==identity),None)
        if (status!='UNKNOWN' or not q or q['intent_sha256']!=hashlib.sha256(body.encode()).hexdigest()
                or q['reservation']!=json.loads(reservation)):
            raise ValueError('UNCLASSIFIED_LIVE_EXECUTION')
        parent=db.execute('SELECT intent FROM operations WHERE id=?',(q['parent_id'],)).fetchone()
        if not parent or q['parent_sha256']!=hashlib.sha256(parent[0].encode()).hexdigest():
            raise ValueError('QUARANTINE_PARENT_CHANGED')
        parents.add(q['parent_id'])
    for identity, in db.execute("SELECT id FROM operations WHERE status IN ('PREPARED','UNKNOWN')"):
        if identity not in parents:raise ValueError('UNCLASSIFIED_PREPARED')
    return {'read_only_deployment_safe':True,'live_manual_copy_safe':False,'quarantined':len(parents)}
