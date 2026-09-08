"""Durable, scoped authority. Consensus is evidence, never a signing grant."""
import json
from typing import Literal
from pydantic import Field
from .contracts import Contract,Scope,Name,Millis
from .store import encoded,digest,scope_key
from core.intelligence.models import ConsensusDecision,LeaderTradeEvent


class AuthorizationPolicy(Contract):
    scope: Scope
    mode: Literal['OBSERVE','PAPER_AUTO','SHADOW','LIVE_CONFIRM']='OBSERVE'
    policy_id: Name='authorization-v1'
    max_signal_age_ms: int=Field(default=60000,strict=True,gt=0,le=300000)


class AuthorizationDecision(Contract):
    decision_id: Name
    scope: Scope
    event_id: Name
    consensus_hash: Name
    policy_hash: Name
    mode: Literal['OBSERVE','PAPER_AUTO','SHADOW','LIVE_CONFIRM']
    outcome: Literal['OBSERVE','AUTHORIZED','HYPOTHETICAL','CONFIRMATION_REQUIRED','REJECTED']
    execution_mode: Literal['NONE','PAPER','LIVE']
    created_ms: Millis
    expires_ms: Millis
    reasons: tuple[Name,...]


class AuthorizationService:
    def __init__(self,store):
        self.store=store
        with store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS authorization_requests(
                    id TEXT PRIMARY KEY,scope TEXT,event_id TEXT,body TEXT,policy TEXT,consensus TEXT,
                    UNIQUE(scope,event_id));
                CREATE TABLE IF NOT EXISTS authorization_confirmations(id TEXT PRIMARY KEY,scope TEXT,body TEXT);
                CREATE TABLE IF NOT EXISTS authorization_rejections(id TEXT PRIMARY KEY,scope TEXT,created_ms INTEGER);
                CREATE TRIGGER IF NOT EXISTS auth_no_update BEFORE UPDATE ON authorization_requests BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS auth_no_delete BEFORE DELETE ON authorization_requests BEGIN SELECT RAISE(ABORT,'immutable'); END;
            ''')
            if 'event' not in {r[1] for r in db.execute('PRAGMA table_info(authorization_requests)')}:
                db.execute('ALTER TABLE authorization_requests ADD COLUMN event TEXT')

    def decide(self,policy,event,consensus,now):
        policy=AuthorizationPolicy.model_validate_json(policy.model_dump_json())
        event=LeaderTradeEvent.model_validate_json(event.model_dump_json())
        consensus=ConsensusDecision.model_validate_json(consensus.model_dump_json())
        reasons=[]
        if type(now) is not int or now<0: raise ValueError('CLOCK_INVALID')
        if event.instrument.network!=policy.scope.network or consensus.event_id!=event.event_id:
            reasons.append('SCOPE_MISMATCH')
        if not 0<=now-event.exchange_ms<=policy.max_signal_age_ms or not 0<=now-consensus.created_ms<=policy.max_signal_age_ms:
            reasons.append('STALE_DECISION')
        expected='COPY_LONG' if event.side=='BUY' else 'COPY_SHORT'
        if consensus.decision!=expected or consensus.blockers: reasons.append('CONSENSUS_NOT_APPROVED')
        key=digest(policy.scope)+event.event_id
        import hashlib
        decision_id=hashlib.sha256(key.encode()).hexdigest()
        outcome,execution={'OBSERVE':('OBSERVE','NONE'),'PAPER_AUTO':('AUTHORIZED','PAPER'),
            'SHADOW':('HYPOTHETICAL','NONE'),'LIVE_CONFIRM':('CONFIRMATION_REQUIRED','NONE')}[policy.mode]
        if reasons: outcome,execution='REJECTED','NONE'
        decision=AuthorizationDecision(decision_id=decision_id,scope=policy.scope,event_id=event.event_id,
            consensus_hash=digest(consensus),policy_hash=digest(policy),mode=policy.mode,outcome=outcome,
            execution_mode=execution,created_ms=now,expires_ms=max(now,event.exchange_ms+policy.max_signal_age_ms),reasons=tuple(reasons))
        with self.store.transaction() as db:
            self.store.bind(db,policy.scope)
            previous=db.execute('SELECT body,event FROM authorization_requests WHERE id=?',(decision_id,)).fetchone()
            if previous:
                old=AuthorizationDecision.model_validate_json(previous[0])
                if old.consensus_hash!=decision.consensus_hash or previous['event']!=encoded(event): raise ValueError('DECISION_IDENTITY_CONFLICT')
                return old # A policy toggle cannot replay the same leader action.
            db.execute('INSERT INTO authorization_requests VALUES(?,?,?,?,?,?,?)',
                (decision_id,scope_key(policy.scope),event.event_id,encoded(decision),encoded(policy),encoded(consensus),encoded(event)))
        return decision

    def confirm(self,decision_id,scope,now,*,authenticated_user):
        """Called by an authenticated controller, never by an event consumer."""
        if str(authenticated_user)!=scope.tenant: raise ValueError('CONFIRMATION_TENANT_MISMATCH')
        if type(now) is not int: raise ValueError('CLOCK_INVALID')
        with self.store.transaction() as db:
            if db.execute('SELECT 1 FROM authorization_rejections WHERE id=? AND scope=?',(decision_id,scope_key(scope))).fetchone():
                raise ValueError('PROPOSAL_REJECTED')
            row=db.execute('SELECT body FROM authorization_requests WHERE id=? AND scope=?',(decision_id,scope_key(scope))).fetchone()
            if not row: raise ValueError('AUTHORIZATION_NOT_FOUND')
            pending=AuthorizationDecision.model_validate_json(row[0])
            if pending.mode!='LIVE_CONFIRM' or pending.outcome!='CONFIRMATION_REQUIRED' or not pending.created_ms<=now<pending.expires_ms:
                raise ValueError('CONFIRMATION_UNAVAILABLE_OR_EXPIRED')
            existing=db.execute('SELECT body FROM authorization_confirmations WHERE id=? AND scope=?',(decision_id,scope_key(scope))).fetchone()
            if existing: return AuthorizationDecision.model_validate_json(existing[0])
            approved=pending.model_copy(update={'outcome':'AUTHORIZED','execution_mode':'LIVE'})
            db.execute('INSERT INTO authorization_confirmations VALUES(?,?,?)',(decision_id,scope_key(scope),encoded(approved)))
            return approved

    def reject(self,decision_id,scope,now,*,authenticated_user,projection=None):
        if str(authenticated_user)!=scope.tenant:raise ValueError('CONFIRMATION_TENANT_MISMATCH')
        with self.store.transaction() as db:
            row=db.execute('SELECT body FROM authorization_requests WHERE id=? AND scope=?',(decision_id,scope_key(scope))).fetchone()
            if not row or AuthorizationDecision.model_validate_json(row[0]).mode!='LIVE_CONFIRM':raise ValueError('AUTHORIZATION_NOT_FOUND')
            if db.execute('SELECT 1 FROM authorization_confirmations WHERE id=?',(decision_id,)).fetchone() or db.execute('SELECT 1 FROM intents WHERE id=?',(decision_id,)).fetchone():raise ValueError('ALREADY_AUTHORIZED')
            db.execute('INSERT OR IGNORE INTO authorization_rejections VALUES(?,?,?)',(decision_id,scope_key(scope),now))
            if projection:projection(db)

    def verify(self,decision,now):
        decision=AuthorizationDecision.model_validate_json(decision.model_dump_json())
        if type(now) is not int or not decision.created_ms<=now<decision.expires_ms: return False
        table='authorization_confirmations' if decision.execution_mode=='LIVE' else 'authorization_requests'
        with self.store.transaction() as db:
            row=db.execute('SELECT body FROM '+table+' WHERE id=? AND scope=?',(decision.decision_id,scope_key(decision.scope))).fetchone()
        return bool(row and row[0]==encoded(decision) and decision.outcome in {'AUTHORIZED','HYPOTHETICAL'})
