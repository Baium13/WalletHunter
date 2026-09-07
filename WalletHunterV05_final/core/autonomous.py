"""Connected tenant PAPER consumer of persisted discovery/research evidence.

No signing credentials or live client construction. All PAPER submissions use
the existing canonical gateway, not the follower analytics simulator.
"""
import json
from core.intelligence.models import LeaderTradeEvent,LeaderScore,IntelligencePolicy,RiskContextEvidence
from core.intelligence.agents import evaluate,consensus
from core.foundation.contracts import OrderIntent,MarketSnapshot
from core.foundation.authorization import AuthorizationService,AuthorizationPolicy
from core.foundation.autonomous_allocation import AutonomousLedger
from core.foundation.execution import ExecutionGateway,FakeExchange
from core.foundation.ledger import Reservation
from core.foundation.risk import RiskGateway
from core.foundation.store import scope_key,encoded,digest


class AutonomousBackend:
    def __init__(self,store,exchange,allocation_policy,authorization_policy,risk_policy,clock):
        from core.foundation.copy_execution import HyperliquidExecutionAdapter
        expected = HyperliquidExecutionAdapter if authorization_policy.mode=='LIVE_CONFIRM' else FakeExchange
        if type(exchange) is not expected: raise ValueError('Mode-specific controlled adapter required')
        if allocation_policy.scope!=authorization_policy.scope or risk_policy.scope!=authorization_policy.scope:
            raise ValueError('Scope mismatch')
        self.store,self.exchange,self.allocation_policy,self.auth_policy=store,exchange,allocation_policy,authorization_policy
        self.risk=RiskGateway(risk_policy); self.clock=clock
        self.authorization=AuthorizationService(store)
        self.gateway=ExecutionGateway(store,self.risk,exchange,clock)
        from core.position_episodes import EpisodeService
        self.episodes=EpisodeService(store)
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS autonomous_modes(scope TEXT PRIMARY KEY,mode TEXT NOT NULL)')
            mode=db.execute('SELECT mode FROM autonomous_modes WHERE scope=?',(scope_key(authorization_policy.scope),)).fetchone()
            if mode and mode[0]!=authorization_policy.mode: raise ValueError('Separate state required for PAPER/SHADOW/LIVE modes')
            db.execute('INSERT OR IGNORE INTO autonomous_modes VALUES(?,?)',(scope_key(authorization_policy.scope),authorization_policy.mode))
            db.execute('CREATE TABLE IF NOT EXISTS autonomous_decisions(id TEXT PRIMARY KEY,scope TEXT,event_id TEXT,body TEXT,intent TEXT,UNIQUE(scope,event_id))')
            db.execute('CREATE TABLE IF NOT EXISTS autonomous_cursors(scope TEXT PRIMARY KEY,seq INTEGER NOT NULL)')

    def drain(self,discovery,limit=4):
        if discovery.network!=self.auth_policy.scope.network: raise ValueError('Network mismatch')
        if type(limit) is not int or not 1<=limit<=4: raise ValueError('Consumer bound')
        scope=scope_key(self.auth_policy.scope)
        with self.store.transaction() as db:
            row=db.execute('SELECT seq FROM autonomous_cursors WHERE scope=?',(scope,)).fetchone()
            after=row[0] if row else 0
        for row in discovery.records(after,limit):
            if row['kind']=='DECISION': self.process(row['body'])
            with self.store.transaction() as db:
                db.execute('INSERT INTO autonomous_cursors VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET seq=MAX(seq,excluded.seq)',(scope,row['seq']))
        return True

    def process(self,record):
        """Input is the actual WalletDiscoveryEngine DECISION body, not a signal shortcut."""
        event=LeaderTradeEvent.model_validate(record['event'])
        leader=LeaderScore.model_validate(record['leader'])
        policy=IntelligencePolicy.model_validate(record['policy'])
        scope=self.auth_policy.scope
        if event.instrument.network!=scope.network: raise ValueError('Network mismatch')
        now=self.clock()
        with self.store.transaction() as db:
            previous=db.execute('SELECT body,intent FROM autonomous_decisions WHERE scope=? AND event_id=?',(scope_key(scope),event.event_id)).fetchone()
            if previous: return json.loads(previous['body']) # No resubmission on re-delivery.
            before=self.store.portfolio_in(db,scope)
            pending=db.execute("SELECT reservation FROM intents WHERE scope=? AND status IN ('SUBMITTING','UNKNOWN')",(scope_key(scope),)).fetchall()
            if not pending and before.evidence=='FAKE' and now>before.received_ms:
                # Local PAPER cash/positions are authoritative simulated state,
                # not a refreshed or invented Hyperliquid account watermark.
                before=before.model_copy(update={'revision':before.revision+1,'exchange_ms':now,'received_ms':now})
                self.store.publish_portfolio_in(db,before,event.event_id)
        reservations=[Reservation(**json.loads(r[0])) for r in pending]
        ledger=AutonomousLedger(before,self.allocation_policy,reservations)
        book=record['book']
        bid=float(book['levels'][0][0]['px']); ask=float(book['levels'][1][0]['px'])
        market=MarketSnapshot(instrument=event.instrument,exchange_ms=book['time'],received_ms=record['consensus']['created_ms'],
            price=bid+(ask-bid)/2,bid=bid,ask=ask,completeness='COMPLETE',freshness='FRESH',source='REST',source_version='intelligence-book-v1')
        limit=ask if event.side=='BUY' else bid
        leverage=min(self.allocation_policy.max_leverage,self.risk.policy.max_leverage)
        context=None
        if not ledger.errors:
            upper=ledger.size(limit,leverage,self.risk.policy.size_step,leader.confidence,1.)
            margin=upper*limit/leverage
            context=RiskContextEvidence(portfolio=before,market=market,allocation=ledger.allocation(self.allocation_policy.source),
                unresolved=bool(pending),required_margin=margin,required_capacity=margin+upper*limit*self.risk.policy.fee_buffer_pct/100,
                slippage_pct=self.risk.policy.max_slippage_pct,max_slippage_pct=self.risk.policy.max_slippage_pct)
        agents=evaluate(event,leader,record['candles'],book,now,policy,context=context,actionable=True)
        result=consensus(event,agents,now,policy)
        auth=self.authorization.decide(self.auth_policy,event,result,now)
        body={'event':event.model_dump(mode='json'),'agents':[a.model_dump(mode='json') for a in agents],
            'consensus':result.model_dump(mode='json'),'authorization':auth.model_dump(mode='json'),
            'mode':auth.mode,'status':auth.outcome,'correlation_id':event.event_id,
            'market':market.model_dump(mode='json'),'leader':leader.model_dump(mode='json'),
            'allocation':ledger.allocation(self.allocation_policy.source).model_dump(mode='json') if not ledger.errors else None,
            'allocation_policy':self.allocation_policy.model_dump(mode='json')}
        intent=None
        if (auth.outcome=='AUTHORIZED' and auth.execution_mode=='PAPER') or auth.outcome in {'HYPOTHETICAL','CONFIRMATION_REQUIRED'}:
            size=ledger.size(limit,leverage,self.risk.policy.size_step,leader.confidence,result.confidence)
            if size>0:
                live=auth.mode=='LIVE_CONFIRM'
                intent=OrderIntent(version=3 if live else 1,intent_id=auth.decision_id,scope=scope,instrument=event.instrument,source=self.allocation_policy.source,
                    action=event.action,side=event.side,size=size,limit_price=limit,leverage=leverage,slippage_pct=self.risk.policy.max_slippage_pct,
                    authorization='USER_CONFIRMED' if live else 'PAPER_POLICY',execution_mode='LIVE' if live else 'PAPER',
                    configure_leverage=live,correlation_id=event.event_id,created_ms=auth.created_ms,expires_ms=auth.expires_ms)
                if live: body['proposal']=intent.model_dump(mode='json')
            else: body['status']='ZERO_SIZE'
        if intent is not None and auth.mode=='SHADOW':
            risk=self.risk.evaluate(intent,market,ledger,now,authorized=self.authorization.verify(auth,now),unresolved=bool(pending))
            body.update(status='SHADOW_APPROVED' if risk.outcome=='APPROVED' else 'SHADOW_REJECTED',
                risk=risk.model_dump(mode='json'),hypothetical_intent=intent.model_dump(mode='json'),
                assumptions={'entry_price':limit,'price_source':'OBSERVED_ASK' if event.side=='BUY' else 'OBSERVED_BID',
                    'execution':'HYPOTHETICAL_ONLY','fee_buffer_pct':self.risk.policy.fee_buffer_pct,'fill_guaranteed':False})
        with self.store.transaction() as db:
            previous=db.execute('SELECT body FROM autonomous_decisions WHERE id=?',(auth.decision_id,)).fetchone()
            if previous: return json.loads(previous[0])
            db.execute('INSERT INTO autonomous_decisions VALUES(?,?,?,?,?)',(auth.decision_id,scope_key(scope),event.event_id,
                json.dumps(body,allow_nan=False),encoded(intent) if intent else None))
            self.episodes.prepare_in(db,body,intent)
        if intent is not None and auth.mode=='PAPER_AUTO':
            self.gateway.authorize_paper(intent,auth)
            receipt=self.gateway.execute(intent,market,autonomous_ledger=ledger)
            body['receipt']=receipt.model_dump(mode='json'); body['status']=receipt.status
            with self.store.transaction() as db:
                row=db.execute('SELECT decision FROM intents WHERE id=?',(intent.intent_id,)).fetchone()
                body['risk']=json.loads(row[0])
                db.execute('UPDATE autonomous_decisions SET body=? WHERE id=?',(json.dumps(body,allow_nan=False),auth.decision_id))
        episode=self.episodes.sync(auth.decision_id,scope)
        if episode:
            body['episode']=episode.model_dump(mode='json')
            with self.store.transaction() as db:
                db.execute('UPDATE autonomous_decisions SET body=? WHERE id=?',(json.dumps(body,allow_nan=False),auth.decision_id))
        return body

    def confirm(self, decision_id, *, authenticated_user):
        """Explicit controller action. Refresh account before deterministic risk.

        The proposal price bound is immutable. If its market evidence expired,
        confirmation rejects rather than silently repricing the user's order.
        """
        if self.auth_policy.mode != 'LIVE_CONFIRM': raise ValueError('Live confirmation mode required')
        scope=self.auth_policy.scope
        if str(authenticated_user)!=scope.tenant: raise ValueError('Confirmation tenant mismatch')
        with self.store.transaction() as db:
            row=db.execute('SELECT body,intent FROM autonomous_decisions WHERE id=? AND scope=?',
                (decision_id,scope_key(scope))).fetchone()
            if not row or not row['intent']: raise ValueError('Proposal unavailable')
            existing=db.execute('SELECT body FROM intents WHERE id=? AND scope=?',(decision_id,scope_key(scope))).fetchone()
            body=json.loads(row['body'])
            proposal=OrderIntent.model_validate_json(row['intent'])
            revision=self.store.portfolio_in(db,scope).revision+1
        if existing:
            # Query-only recovery even when the acknowledgement was lost.
            receipt=self.gateway.recover(OrderIntent.model_validate_json(existing[0]))
            body.update(receipt=receipt.model_dump(mode='json'),status=receipt.status)
            body['episode']=self.episodes.sync(decision_id,scope).model_dump(mode='json')
            return body
        auth=self.authorization.confirm(decision_id,scope,self.clock(),authenticated_user=authenticated_user)
        before=self.exchange.refresh(revision,proposal.instrument.dex)
        self.store.publish_portfolio(before,proposal.correlation_id)
        intent=proposal.model_copy(update={'created_ms':self.clock()})
        ledger=AutonomousLedger(before,self.allocation_policy)
        self.gateway.authorize_live(intent,auth)
        receipt=self.gateway.execute(intent,MarketSnapshot.model_validate(body['market']),autonomous_ledger=ledger)
        body.update(authorization=auth.model_dump(mode='json'),receipt=receipt.model_dump(mode='json'),status=receipt.status)
        with self.store.transaction() as db:
            risk=db.execute('SELECT decision FROM intents WHERE id=?',(decision_id,)).fetchone()
            body['risk']=json.loads(risk[0])
            db.execute('UPDATE autonomous_decisions SET body=? WHERE id=?',(json.dumps(body,allow_nan=False),decision_id))
        body['episode']=self.episodes.sync(decision_id,scope).model_dump(mode='json')
        return body


def load_paper_backend(config_path,state_directory,network,clock):
    """Explicit operator-configured isolated PAPER runtime; never loads profiles."""
    from pathlib import Path
    from contextlib import closing
    import sqlite3
    from pydantic import BaseModel,ConfigDict
    from core.foundation.autonomous_allocation import AutonomousAllocationPolicy
    from core.foundation.risk import RiskPolicy
    from core.foundation.contracts import Positive,PortfolioSnapshot
    from core.foundation.store import Store
    class Config(BaseModel):
        model_config=ConfigDict(extra='forbid')
        allocation: AutonomousAllocationPolicy
        authorization: AuthorizationPolicy
        risk: RiskPolicy
        initial_paper_equity: Positive
    config=Config.model_validate_json(Path(config_path).read_text(encoding='utf-8'))
    if config.authorization.mode not in ('OBSERVE','PAPER_AUTO','SHADOW') or config.authorization.scope.network!=network:
        raise ValueError('Explicit PAPER configuration required')
    directory=Path(state_directory)
    for name,marker in (('autonomy.sqlite','autonomous_decisions'),('fake.sqlite','fake_orders')):
        path=directory/name
        if path.is_symlink() or directory.is_symlink(): raise ValueError('Unsafe PAPER path')
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables and marker not in tables: raise ValueError('Dedicated PAPER database required')
    store=Store(directory/'autonomy.sqlite'); exchange=FakeExchange(directory/'fake.sqlite')
    backend=AutonomousBackend(store,exchange,config.allocation,config.authorization,config.risk,clock)
    with store.transaction() as db:
        row=db.execute('SELECT body FROM portfolios WHERE scope=?',(scope_key(config.authorization.scope),)).fetchone()
        if row is None:
            now=clock()
            portfolio=PortfolioSnapshot(scope=config.authorization.scope,revision=1,exchange_ms=now,received_ms=now,
                equity=config.initial_paper_equity,sizing_capital=config.initial_paper_equity,available_collateral=config.initial_paper_equity,
                completeness='COMPLETE',evidence='FAKE')
            store.publish_portfolio_in(db,portfolio,'explicit-paper-seed')
    return backend
