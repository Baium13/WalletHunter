"""Connected tenant PAPER consumer of persisted discovery/research evidence.

No signing credentials or live client construction. All PAPER submissions use
the existing canonical gateway, not the follower analytics simulator.
"""
import json
import hashlib
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
    @staticmethod
    def _child_event_id(parent, suffix):
        return hashlib.sha256((parent+'|'+suffix).encode()).hexdigest()[:48]
    def __init__(self,store,exchange,allocation_policy,authorization_policy,risk_policy,clock,cost_model=None):
        from core.foundation.copy_execution import HyperliquidExecutionAdapter
        expected = HyperliquidExecutionAdapter if authorization_policy.mode=='LIVE_CONFIRM' else FakeExchange
        if type(exchange) is not expected: raise ValueError('Mode-specific controlled adapter required')
        if allocation_policy.scope!=authorization_policy.scope or risk_policy.scope!=authorization_policy.scope:
            raise ValueError('Scope mismatch')
        self.store,self.exchange,self.allocation_policy,self.auth_policy=store,exchange,allocation_policy,authorization_policy
        self.risk=RiskGateway(risk_policy); self.clock=clock
        self.authorization=AuthorizationService(store)
        self.gateway=ExecutionGateway(store,self.risk,exchange,clock)
        from core.foundation.paper_costs import PaperCosts
        self.cost_model=cost_model or PaperCosts()
        if authorization_policy.mode!='LIVE_CONFIRM':
            if self.cost_model.fee_bps>risk_policy.fee_buffer_pct*100:raise ValueError('Simulation fees exceed configured risk buffer')
            exchange.configure_costs(self.cost_model)
        from core.position_episodes import EpisodeService
        self.episodes=EpisodeService(store)
        from core.autonomous_jobs import JobStore
        self.jobs=JobStore(store,authorization_policy.scope,clock)
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
        self.recover(limit)
        # Indexed actionable query, not four records of telemetry noise.
        with discovery.store.transaction() as db:
            rows=db.execute("SELECT rowid AS seq,id,body FROM intelligence_records WHERE network=? AND kind='DECISION' AND rowid>? ORDER BY rowid LIMIT ?",
                (discovery.network,after,limit)).fetchall()
        successes=0
        for row in rows:
            try:
                record=json.loads(row['body'])
                with self.store.transaction() as db:
                    db.execute('INSERT OR IGNORE INTO autonomous_deliveries VALUES(?,?,?,?)',
                        (scope,row['id'],row['seq'],record.get('event',{}).get('event_id')))
                processed=self.process(record)
                successes+=int(processed.get('status')!='QUARANTINED')
            except Exception as exc:
                # Financial work is retained by process()/canonical intents;
                # dead-lettering the delivery never releases its reservation.
                self.jobs.quarantine(row['seq'],row['body'],type(exc).__name__)
            self.jobs.advance(row['seq'])
        with self.store.transaction() as db:
            old_health=db.execute('SELECT body FROM autonomous_health WHERE scope=?',(scope,)).fetchone()
            old_health=json.loads(old_health[0]) if old_health else {}
            health={'heartbeat_ms':self.clock(),'last_success_ms':self.clock() if successes else old_health.get('last_success_ms'),
                'quarantine_count':db.execute('SELECT COUNT(*) FROM autonomous_quarantine WHERE scope=?',(scope,)).fetchone()[0],
                'unresolved_execution_count':db.execute("SELECT COUNT(*) FROM intents WHERE scope=? AND status IN ('SUBMITTING','UNKNOWN','PARTIAL')",(scope,)).fetchone()[0],
                'open_episode_count':db.execute("SELECT COUNT(*) FROM position_episodes WHERE scope=? AND json_extract(body,'$.state') NOT IN ('CLOSED','REJECTED')",(scope,)).fetchone()[0],
                'outcome_count':db.execute('SELECT COUNT(*) FROM autonomous_outcomes WHERE scope=?',(scope,)).fetchone()[0]}
            with discovery.store.transaction() as research:
                tail=research.execute("SELECT COUNT(*),MIN(json_extract(body,'$.event.exchange_ms')) FROM intelligence_records WHERE network=? AND kind='DECISION' AND rowid>?",
                    (discovery.network,rows[-1]['seq'] if rows else after)).fetchone()
            health.update(actionable_backlog=tail[0],actionable_lag_ms=max(0,self.clock()-tail[1]) if tail[1] else 0)
            health['reconciliation_backlog']=health['unresolved_execution_count']
            health['quarantined_jobs']=db.execute("SELECT COUNT(*) FROM autonomous_jobs WHERE scope=? AND stage='QUARANTINED'",(scope,)).fetchone()[0]
            health['quarantine_count']+=health['quarantined_jobs']
            health['status']='DEGRADED' if health['quarantine_count'] or health['unresolved_execution_count'] else 'HEALTHY'
            pending_job=db.execute("SELECT MIN(started_ms) FROM autonomous_jobs WHERE scope=? AND stage IN ('CLAIMED','ANALYZED','AUTHORIZED','SUBMISSION_PENDING','RECOVERY_REQUIRED','RECONCILING')",(scope,)).fetchone()[0]
            health['processing_lag_ms']=max(0,self.clock()-pending_job) if pending_job else 0
            db.execute('INSERT OR REPLACE INTO autonomous_health VALUES(?,?)',(scope,json.dumps(health)))
        return True

    def process(self,record):
        from pathlib import Path
        from core.ai_review import account_guard
        event=LeaderTradeEvent.model_validate(record['event'])
        if event.action=='REVERSE':return self.reverse(record)
        with account_guard(Path(self.store.path).parent,self.auth_policy.scope.account):
            job=self.jobs.claim(event.event_id,record)
            if job['stage']=='QUARANTINED':return {'status':'QUARANTINED','event_id':event.event_id}
            try:
                result=self._process(record)
                self.jobs.stage(event.event_id,'RECONCILING' if result.get('status') in {'UNKNOWN','PARTIAL','SUBMITTING'} else
                    'AWAITING_CONFIRMATION' if result.get('status')=='CONFIRMATION_REQUIRED' else 'COMPLETED')
                return result
            except Exception as exc:
                with self.store.transaction() as db:
                    financial=db.execute('SELECT intent FROM autonomous_decisions WHERE scope=? AND event_id=?',
                        (scope_key(self.auth_policy.scope),event.event_id)).fetchone()
                self.jobs.stage(event.event_id,'RECOVERY_REQUIRED' if financial and financial['intent'] else 'QUARANTINED',type(exc).__name__)
                raise

    def _process(self,record):
        """Input is the actual WalletDiscoveryEngine DECISION body, not a signal shortcut."""
        event=LeaderTradeEvent.model_validate(record['event'])
        if record.get('admission_allowed',True) is False and event.action not in {'REDUCE','CLOSE'}:
            return {'status':'LEADER_ADMISSION_HOLD','event_id':event.event_id}
        if event.action=='REVERSE':
            return self.reverse(record)
        leader=LeaderScore.model_validate(record['leader'])
        policy=IntelligencePolicy.model_validate(record['policy'])
        scope=self.auth_policy.scope
        if event.instrument.network!=scope.network: raise ValueError('Network mismatch')
        now=self.clock()
        with self.store.transaction() as db:
            previous=db.execute('SELECT body,intent FROM autonomous_decisions WHERE scope=? AND event_id=?',(scope_key(scope),event.event_id)).fetchone()
            if previous: return json.loads(previous['body']) # Recovery is a separate query-only pass.
            before=self.store.portfolio_in(db,scope)
            episode=self.episodes.active_in(db,scope,self.auth_policy.mode,event.wallet,event.instrument)
            pending=db.execute("SELECT reservation FROM intents WHERE scope=? AND status IN ('SUBMITTING','UNKNOWN','PARTIAL')",(scope_key(scope),)).fetchall()
            if not pending and before.evidence=='FAKE' and now>before.received_ms:
                # Local PAPER cash/positions are authoritative simulated state,
                # not a refreshed or invented Hyperliquid account watermark.
                before=before.model_copy(update={'revision':before.revision+1,'exchange_ms':now,'received_ms':now})
                self.store.publish_portfolio_in(db,before,event.event_id)
        reservations=[Reservation(**json.loads(r[0])) for r in pending]
        ledger=AutonomousLedger(before,self.allocation_policy,reservations)
        position=next((p for p in before.positions if p.instrument==event.instrument),None) if episode else None
        reducing=event.action in {'REDUCE','CLOSE'}
        book=record['book']
        bid=float(book['levels'][0][0]['px']); ask=float(book['levels'][1][0]['px'])
        market=MarketSnapshot(instrument=event.instrument,exchange_ms=book['time'],received_ms=record['consensus']['created_ms'],
            price=bid+(ask-bid)/2,bid=bid,ask=ask,completeness='COMPLETE',freshness='FRESH',source='REST',source_version='intelligence-book-v1')
        limit=ask if event.side=='BUY' else bid
        leverage=min(self.allocation_policy.max_leverage,self.risk.policy.max_leverage)
        if position: leverage=int(position.leverage)
        context=None
        if not ledger.errors:
            upper=0. if reducing else ledger.size(limit,leverage,self.risk.policy.size_step,leader.confidence,1.)
            margin=upper*limit/leverage
            context=RiskContextEvidence(portfolio=before,market=market,allocation=ledger.allocation(self.allocation_policy.source),
                unresolved=bool(pending),required_margin=margin,required_capacity=margin+upper*limit*self.risk.policy.fee_buffer_pct/100,
                slippage_pct=self.risk.policy.max_slippage_pct,max_slippage_pct=self.risk.policy.max_slippage_pct)
        from core.intelligence.models import AgentResult,ConsensusDecision
        with self.store.transaction() as db:
            saved=db.execute('SELECT body FROM autonomous_analysis WHERE scope=? AND event_id=?',(scope_key(scope),event.event_id)).fetchone()
        if saved:
            saved=json.loads(saved[0]);agents=[AgentResult.model_validate(a) for a in saved['agents']]
            result=ConsensusDecision.model_validate(saved['consensus'])
        else:
            agents=evaluate(event,leader,record['candles'],book,now,policy,context=context,actionable=True)
            result=consensus(event,agents,now,policy,position=position)
            with self.store.transaction() as db:
                db.execute('INSERT INTO autonomous_analysis VALUES(?,?,?)',(scope_key(scope),event.event_id,
                    json.dumps({'agents':[a.model_dump(mode='json') for a in agents],'consensus':result.model_dump(mode='json')})))
        self.jobs.stage(event.event_id,'ANALYZED')
        auth=self.authorization.decide(self.auth_policy,event,result,now)
        self.jobs.stage(event.event_id,'AUTHORIZED')
        body={'event':event.model_dump(mode='json'),'agents':[a.model_dump(mode='json') for a in agents],
            'consensus':result.model_dump(mode='json'),'authorization':auth.model_dump(mode='json'),
            'mode':auth.mode,'status':auth.outcome,'correlation_id':event.event_id,
            'market':market.model_dump(mode='json'),'leader':leader.model_dump(mode='json'),
            'allocation':ledger.allocation(self.allocation_policy.source).model_dump(mode='json') if not ledger.errors else None,
            'allocation_policy':self.allocation_policy.model_dump(mode='json')}
        body['cost_model']=self.cost_model.model_dump(mode='json') if auth.mode!='LIVE_CONFIRM' else None
        intent=None
        if (auth.outcome=='AUTHORIZED' and auth.execution_mode=='PAPER') or auth.outcome in {'HYPOTHETICAL','CONFIRMATION_REQUIRED'}:
            if reducing:
                from decimal import Decimal,ROUND_FLOOR
                fraction=min(1.,event.size/abs(event.before_size)) if event.before_size else 0.
                raw=position.size*(1. if event.action=='CLOSE' else fraction) if position else 0.
                size=float((Decimal(str(raw))/Decimal(str(self.risk.policy.size_step))).to_integral_value(rounding=ROUND_FLOOR)*Decimal(str(self.risk.policy.size_step)))
                if event.action=='CLOSE' and position: size=position.size
            else:
                size=ledger.size(limit,leverage,self.risk.policy.size_step,leader.confidence,result.confidence)
            if size>0:
                live=auth.mode=='LIVE_CONFIRM'
                intent=OrderIntent(version=3 if live else 1,intent_id=auth.decision_id,scope=scope,instrument=event.instrument,source=self.allocation_policy.source,
                    action=event.action,side=event.side,size=size,limit_price=limit,leverage=leverage,slippage_pct=self.risk.policy.max_slippage_pct,
                    authorization='USER_CONFIRMED' if live else 'PAPER_POLICY',execution_mode='LIVE' if live else 'PAPER',
                    configure_leverage=live,correlation_id=event.event_id,created_ms=auth.created_ms,expires_ms=auth.expires_ms)
                if live: body['proposal']=intent.model_dump(mode='json')
            else: body['status']='ZERO_SIZE'
        if intent is not None:
            body['risk']=self.risk.evaluate(intent,market,ledger,now,
                authorized=auth.outcome in {'AUTHORIZED','HYPOTHETICAL'},unresolved=bool(pending)).model_dump(mode='json')
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
            if intent is not None and body['status']=='SHADOW_REJECTED':
                rejected_episode=self.episodes.active_in(db,scope,auth.mode,event.wallet,event.instrument)
                self.episodes.transition_in(db,rejected_episode,intent.intent_id,'REJECTED',{'risk':body['risk'],'no_submission':True})
            if intent is not None and body['status']=='SHADOW_APPROVED':
                from core.foundation.contracts import Fill
                from core.foundation.paper_costs import costed_effect
                # Hypothetical position, not an exchange receipt or actual fill.
                hypo_id='hypo-'+hashlib.sha256(intent.intent_id.encode()).hexdigest()[:48]
                assumed=Fill(intent_id=intent.intent_id,instrument=intent.instrument,order_id=hypo_id,
                    trade_id=hypo_id,side=intent.side,size=intent.size,price=limit,exchange_ms=now)
                after=costed_effect(intent,before,(assumed,),now,self.cost_model)
                db.execute('INSERT INTO hypothetical_executions VALUES(?,?,?)',(intent.intent_id,scope_key(scope),json.dumps(
                    {'intent':intent.model_dump(mode='json'),'fills':[assumed.model_dump(mode='json')],
                     'cost_model':self.cost_model.model_dump(mode='json'),'recorded_ms':now,'evidence':'HYPOTHETICAL'})))
                self.store.publish_portfolio_in(db,after,event.event_id)
                hypothetical_episode=self.episodes.active_in(db,scope,auth.mode,event.wallet,event.instrument)
                state={'OPEN':'OPEN','ADD':'INCREASED','REDUCE':'REDUCED','CLOSE':'CLOSED'}[intent.action]
                self.episodes.transition_in(db,hypothetical_episode,intent.intent_id,state,
                    {'hypothetical':True,'assumptions':body['assumptions'],'intent_id':intent.intent_id})
        if intent is not None and auth.mode=='PAPER_AUTO':
            self.jobs.stage(event.event_id,'SUBMISSION_PENDING')
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

    def recover(self,limit=4):
        """Same identities; existing canonical intent always means query, never submit."""
        from core.foundation.authorization import AuthorizationDecision
        from pathlib import Path
        from core.ai_review import account_guard
        scope=self.auth_policy.scope
        with account_guard(Path(self.store.path).parent,scope.account):
            with self.store.transaction() as db:
                rows=db.execute("SELECT * FROM autonomous_jobs j WHERE scope=? AND (stage NOT IN ('COMPLETED','QUARANTINED','AWAITING_CONFIRMATION') OR (stage='AWAITING_CONFIRMATION' AND EXISTS (SELECT 1 FROM authorization_requests r WHERE r.scope=j.scope AND r.event_id=j.event_id AND (json_extract(r.body,'$.expires_ms')<=? OR EXISTS (SELECT 1 FROM authorization_confirmations c WHERE c.id=r.id))))) ORDER BY updated_ms LIMIT ?",(scope_key(scope),self.clock(),limit)).fetchall()
            for job in rows:
                try:
                    with self.store.transaction() as db:
                        row=db.execute('SELECT * FROM autonomous_decisions WHERE scope=? AND event_id=?',(scope_key(scope),job['event_id'])).fetchone()
                    if row is None:
                        # No reservation/submission existed. Replay read-only analysis;
                        # stale evidence still passes current authorization/risk checks.
                        result=self._process(json.loads(job['record']))
                        self.jobs.stage(job['event_id'],'RECONCILING' if result.get('status') in {'UNKNOWN','PARTIAL','SUBMITTING'} else 'COMPLETED')
                        continue
                    elif row['intent'] and self.auth_policy.mode!='SHADOW':
                        body=json.loads(row['body']); intent=OrderIntent.model_validate_json(row['intent'])
                        with self.store.transaction() as db:
                            execution=db.execute('SELECT body FROM intents WHERE id=? AND scope=?',(intent.intent_id,scope_key(scope))).fetchone()
                        if execution:
                            receipt=self.gateway.recover(OrderIntent.model_validate_json(execution[0]))
                        elif self.clock()>=intent.expires_ms:
                            # No canonical reservation/submission exists, so
                            # expiry is a definitive action rejection, not loss
                            # of any existing position owned by the episode.
                            from core.position_episodes import PositionEpisode
                            body['status']='EXPIRED_BEFORE_SUBMISSION'
                            with self.store.transaction() as db:
                                episode=db.execute('SELECT p.body FROM position_episodes p JOIN episode_actions a ON a.episode=p.id WHERE a.id=?',(row['id'],)).fetchone()
                                if episode:self.episodes.transition_in(db,PositionEpisode.model_validate_json(episode[0]),row['id'],'REJECTED',{'reason':'EXPIRED_BEFORE_SUBMISSION','no_submission':True})
                                db.execute('UPDATE autonomous_decisions SET body=? WHERE id=?',(json.dumps(body),row['id']))
                            self.jobs.stage(job['event_id'],'COMPLETED');continue
                        elif self.auth_policy.mode=='PAPER_AUTO':
                            auth=AuthorizationDecision.model_validate(body['authorization'])
                            # Expired grants cannot be reminted. Preserve a definitive
                            # no-submission expiry, not UNKNOWN exchange evidence.
                            if not self.authorization.verify(auth,self.clock()):
                                body['status']='EXPIRED_BEFORE_SUBMISSION'
                                from core.position_episodes import PositionEpisode
                                with self.store.transaction() as db:
                                    episode=db.execute('SELECT p.body FROM position_episodes p JOIN episode_actions a ON a.episode=p.id WHERE a.id=?',(row['id'],)).fetchone()
                                    if episode:self.episodes.transition_in(db,PositionEpisode.model_validate_json(episode[0]),row['id'],'REJECTED',{'reason':'EXPIRED_BEFORE_SUBMISSION','no_submission':True})
                                    db.execute('UPDATE autonomous_decisions SET body=? WHERE id=?',(json.dumps(body),row['id']))
                                self.jobs.stage(job['event_id'],'COMPLETED');continue
                            before=self.store.portfolio(scope)
                            ledger=AutonomousLedger(before,self.allocation_policy)
                            self.gateway.authorize_paper(intent,auth)
                            receipt=self.gateway.execute(intent,MarketSnapshot.model_validate(body['market']),autonomous_ledger=ledger)
                        else:
                            with self.store.transaction() as db:
                                confirmed=db.execute('SELECT body FROM authorization_confirmations WHERE id=? AND scope=?',(intent.intent_id,scope_key(scope))).fetchone()
                            if confirmed and self.authorization.verify(AuthorizationDecision.model_validate_json(confirmed[0]),self.clock()):
                                # Replay the persisted, exact user confirmation,
                                # never mint authority from a consensus record.
                                self.confirm(intent.intent_id,authenticated_user=scope.tenant)
                            else:self.jobs.stage(job['event_id'],'AWAITING_CONFIRMATION')
                            continue
                        body.update(receipt=receipt.model_dump(mode='json'),status=receipt.status)
                        episode=self.episodes.sync(intent.intent_id,scope)
                        if episode:body['episode']=episode.model_dump(mode='json')
                        with self.store.transaction() as db:
                            risk=db.execute('SELECT decision FROM intents WHERE id=?',(intent.intent_id,)).fetchone()
                            body['risk']=json.loads(risk[0])
                            db.execute('UPDATE autonomous_decisions SET body=? WHERE id=?',(json.dumps(body),row['id']))
                        self.jobs.stage(job['event_id'],'RECONCILING' if receipt.status in {'UNKNOWN','SUBMITTING','PARTIAL'} else 'COMPLETED')
                        continue
                    elif row and row['intent']:
                        self.episodes.sync(row['id'],scope)
                    self.jobs.stage(job['event_id'],'COMPLETED')
                except Exception as exc:
                    with self.store.transaction() as db:
                        financial=db.execute('SELECT intent FROM autonomous_decisions WHERE scope=? AND event_id=?',(scope_key(scope),job['event_id'])).fetchone()
                    self.jobs.stage(job['event_id'],'RECOVERY_REQUIRED' if financial and financial['intent'] else 'QUARANTINED',type(exc).__name__)

    def reverse(self,record):
        """Derived legs retain original public fill evidence; each is re-evaluated.

        No OPEN leg exists until the CLOSE is proven terminal. Stable derived
        identities make redelivery query-only, including between the legs.
        """
        from copy import deepcopy
        event=LeaderTradeEvent.model_validate(record['event'])
        if event.before_size*event.after_size>=0: raise ValueError('Invalid reversal evidence')
        close=deepcopy(record); close['parent_event']=event.model_dump(mode='json')
        close['event']=event.model_copy(update={'event_id':self._child_event_id(event.event_id,'close'),'action':'CLOSE',
            'size':abs(event.before_size),'after_size':0.}).model_dump(mode='json')
        result=self.process(close)
        if result.get('episode',{}).get('state')!='CLOSED':
            return {'status':'REVERSE_CLOSE_UNRESOLVED','close':result,'correlation_id':event.event_id}
        opening=deepcopy(record); opening['parent_event']=event.model_dump(mode='json')
        opening['event']=event.model_copy(update={'event_id':self._child_event_id(event.event_id,'open'),'action':'OPEN',
            'size':abs(event.after_size),'before_size':0.}).model_dump(mode='json')
        return {'status':'REVERSE_EVALUATED','close':result,'open':self.process(opening),'correlation_id':event.event_id}

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
            with self.store.transaction() as db:db.execute('UPDATE autonomous_decisions SET body=? WHERE id=?',(json.dumps(body),decision_id))
            self.jobs.stage(proposal.correlation_id,'RECONCILING' if receipt.status in {'UNKNOWN','PARTIAL','SUBMITTING'} else 'COMPLETED')
            return body
        auth=self.authorization.confirm(decision_id,scope,self.clock(),authenticated_user=authenticated_user)
        self.jobs.stage(proposal.correlation_id,'SUBMISSION_PENDING')
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
        with self.store.transaction() as db:db.execute('UPDATE autonomous_decisions SET body=? WHERE id=?',(json.dumps(body),decision_id))
        self.jobs.stage(proposal.correlation_id,'RECONCILING' if receipt.status in {'UNKNOWN','PARTIAL','SUBMITTING'} else 'COMPLETED')
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
    from core.foundation.paper_costs import PaperCosts
    class Config(BaseModel):
        model_config=ConfigDict(extra='forbid')
        allocation: AutonomousAllocationPolicy
        authorization: AuthorizationPolicy
        risk: RiskPolicy
        initial_paper_equity: Positive
        costs: PaperCosts=PaperCosts()
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
    backend=AutonomousBackend(store,exchange,config.allocation,config.authorization,config.risk,clock,config.costs)
    with store.transaction() as db:
        row=db.execute('SELECT body FROM portfolios WHERE scope=?',(scope_key(config.authorization.scope),)).fetchone()
        if row is None:
            now=clock()
            portfolio=PortfolioSnapshot(scope=config.authorization.scope,revision=1,exchange_ms=now,received_ms=now,
                equity=config.initial_paper_equity,sizing_capital=config.initial_paper_equity,available_collateral=config.initial_paper_equity,
                completeness='COMPLETE',evidence='FAKE')
            store.publish_portfolio_in(db,portfolio,'explicit-paper-seed')
    return backend
