"""Connected tenant PAPER consumer of persisted discovery/research evidence.

No signing credentials or live client construction. All PAPER submissions use
the existing canonical gateway, not the follower analytics simulator.
"""
import json
import math
import time
import hashlib
from typing import Annotated
from pydantic import Field
from core.foundation.contracts import Contract, Amount
from core.intelligence.models import LeaderTradeEvent,LeaderScore,IntelligencePolicy,RiskContextEvidence
from core.intelligence.agents import evaluate,consensus
from core.foundation.contracts import OrderIntent,MarketSnapshot
from core.foundation.authorization import AuthorizationService,AuthorizationPolicy
from core.foundation.autonomous_allocation import AutonomousLedger
from core.foundation.execution import ExecutionGateway,FakeExchange
from core.foundation.ledger import Reservation
from core.foundation.risk import RiskGateway
from core.foundation.store import scope_key,encoded,digest


def _fault(exc):
    """Name the failure, not just its class.

    Every quarantine and recovery record stored ``type(exc).__name__``. One
    deployment accumulated 37 jobs all reading "ValueError" - which turned out
    to be a single ambiguity check - with nothing in the record to say so. The
    message is bounded and carries no payload: these are the engine's own
    control-flow errors, never user or exchange data.
    """
    detail=' '.join(str(exc).split())[:180]
    return type(exc).__name__+(': '+detail if detail else '')


class LiveGuardPolicy(Contract):
    """Account-level stops for unattended live submission.

    These sit above the per-order Risk checks and answer a different question:
    not "is this order sound" but "should this account still be opening
    anything at all right now". Reducing actions are never gated by them - a
    stop that traps an open position is worse than the condition it reacts to.
    """
    max_concurrent_positions: Annotated[int,Field(strict=True,ge=1,le=32)]=3
    daily_loss_limit: Amount=0.   # 0 disables the daily stop
    halted: bool=False            # operator kill switch, honoured immediately
    policy_id: str='live-guard-v1'



def carried_attribution(fresh, previous):
    """Carry proven ownership across a fresh exchange read.

    A live account read returns every position as UNKNOWN: the exchange cannot
    say which of its positions we opened. ``publish_portfolio_in`` replaces the
    snapshot wholesale, so the refresh that runs before every live decision
    erased the VERIFIED attribution the gateway had just written from the order,
    fill and delta proof of our own submission.

    Measured on an offline LIVE_AUTO run of the real backend: one OPEN filled
    and was recorded VERIFIED; the next event refreshed the account, the
    position came back UNKNOWN, and the CLOSE was refused pre-consensus as
    REDUCTION_OWNERSHIP_OR_SIDE while the ledger reported ATTRIBUTION_UNKNOWN.
    An unattended live account could therefore open a position and then never
    close it, and never open another.

    Ownership is re-asserted only where the exchange still reports exactly what
    we proved: same instrument, same side, same size. Any divergence leaves the
    position UNKNOWN, because a position that moved is no longer the position
    the receipt proved. Nothing here can create ownership that was not already
    canonical - it only stops a read from discarding it.
    """
    if previous is None: return fresh
    proven={p.instrument:p for p in previous.positions if p.evidence=='VERIFIED'}
    if not proven: return fresh
    positions=[]
    for position in fresh.positions:
        prior=proven.get(position.instrument)
        if (prior is None or position.evidence!='UNKNOWN' or prior.side!=position.side
                or prior.notional<=0 or not math.isclose(prior.size,position.size,rel_tol=1e-9,abs_tol=0.)):
            positions.append(position); continue
        scale=position.notional/prior.notional
        positions.append(position.model_copy(update={'evidence':'VERIFIED','order_ids':prior.order_ids,
            'contributions':tuple(c.model_copy(update={'notional':c.notional*scale}) for c in prior.contributions)}))
    return fresh.model_copy(update={'positions':tuple(positions)})


class AutonomousBackend:
    @staticmethod
    def _child_event_id(parent, suffix):
        return hashlib.sha256((parent+'|'+suffix).encode()).hexdigest()[:48]
    def __init__(self,store,exchange,allocation_policy,authorization_policy,risk_policy,clock,cost_model=None,
                 *,multi_instrument=False,symbols=(),precision=None,live_guard=None,evidence=None,
                 ceilings=None,leader_leverage=None,protection=None):
        from core.foundation.copy_execution import HyperliquidExecutionAdapter
        expected = HyperliquidExecutionAdapter if authorization_policy.mode in ('LIVE_CONFIRM','LIVE_AUTO') else FakeExchange
        if type(exchange) is not expected: raise ValueError('Mode-specific controlled adapter required')
        if allocation_policy.scope!=authorization_policy.scope or risk_policy.scope!=authorization_policy.scope:
            raise ValueError('Scope mismatch')
        self.store,self.exchange,self.allocation_policy,self.auth_policy=store,exchange,allocation_policy,authorization_policy
        # A leader is followed across whatever it trades, so the risk policy
        # re-binds to the intent's instrument when the operator asked for it.
        # Default stays single-instrument: an unexpected symbol is a scope error.
        self.risk=RiskGateway(risk_policy,multi_instrument=multi_instrument,symbols=symbols); self.clock=clock
        # Resolves the venue's lot step for an instrument. Without it every
        # market is sized on the policy's single step, which is only ever
        # correct for the policy's own market.
        self.precision=precision
        # Resolves the venue's leverage ceiling for an instrument, and the
        # leverage the leader is actually running there. Without the ceiling
        # every entry asks for the policy maximum, which most alt perps and
        # every stock cap far below and the venue simply rejects.
        self.ceilings=ceilings
        self.leader_leverage=leader_leverage
        # Unattended live trading gets account-level stops that a per-order
        # policy cannot express. Required for LIVE_AUTO: running it without
        # any stop is not a configuration this constructor will accept.
        if authorization_policy.mode=='LIVE_AUTO' and live_guard is None:
            raise ValueError('LIVE_AUTO requires an explicit live guard policy')
        self.live_guard=live_guard
        # A simulated scope renews its own watermark because its portfolio is
        # authoritative locally. A live one cannot: Risk demands exchange
        # evidence fresher than the policy window and describing the collateral
        # pool the instrument settles in, so LIVE_AUTO must be given a reader.
        if authorization_policy.mode=='LIVE_AUTO' and evidence is None:
            raise ValueError('LIVE_AUTO requires an account evidence reader')
        self.evidence=evidence
        # The only exit that does not need the leader to act. Off unless the
        # configuration names a threshold; a policy whose thresholds are all
        # zero is a stop that silently never fires, so it is refused rather
        # than kept.
        if protection is not None:
            if protection.scope!=authorization_policy.scope: raise ValueError('Scope mismatch')
            if not protection.enabled: raise ValueError('A protection policy with no threshold protects nothing')
        self.protection=protection
        self.authorization=AuthorizationService(store)
        self.gateway=ExecutionGateway(store,self.risk,exchange,clock)
        from core.foundation.paper_costs import PaperCosts
        self.cost_model=cost_model or PaperCosts()
        if authorization_policy.mode not in ('LIVE_CONFIRM','LIVE_AUTO'):
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
                self.jobs.quarantine(row['seq'],row['body'],_fault(exc))
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
            health['closed_episode_count']=db.execute("SELECT COUNT(*) FROM position_episodes WHERE scope=? AND json_extract(body,'$.state')='CLOSED'",(scope,)).fetchone()[0]
            health['calibration_count']=db.execute('SELECT COUNT(*) FROM calibration_records WHERE scope=?',(scope,)).fetchone()[0]
            health['last_episode_created_ms']=db.execute("SELECT MAX(json_extract(body,'$.created_ms')) FROM position_episodes WHERE scope=?",(scope,)).fetchone()[0]
            health['last_action_ms']=db.execute('SELECT MAX(updated_ms) FROM autonomous_jobs WHERE scope=?',(scope,)).fetchone()[0]
            health['quarantined_jobs']=db.execute("SELECT COUNT(*) FROM autonomous_jobs WHERE scope=? AND stage='QUARANTINED'",(scope,)).fetchone()[0]
            # Delivery and job tables describe the same poison event. Count
            # their union, not both physical representations.
            health.update(self.jobs.quarantine_summary_in(db))
            health['status']='DEGRADED' if health['quarantine_count'] or health['unresolved_execution_count'] else 'HEALTHY'
            # Readiness is a fresh worker/configuration observation, not a
            # fabricated market signal or a successful execution timestamp.
            health['ready_components']=['structure','momentum','volatility','liquidity',
                'order_flow','leader','risk_context','consensus','risk','reconciliation']
            health['readiness_version']='configured-worker-v1'
            pending_job=db.execute("SELECT MIN(started_ms) FROM autonomous_jobs WHERE scope=? AND stage IN ('CLAIMED','ANALYZED','AUTHORIZED','SUBMISSION_PENDING','RECOVERY_REQUIRED','RECONCILING')",(scope,)).fetchone()[0]
            health['processing_lag_ms']=max(0,self.clock()-pending_job) if pending_job else 0
            db.execute('INSERT OR REPLACE INTO autonomous_health VALUES(?,?)',(scope,json.dumps(health)))
        return True

    def protect(self,market):
        """Close what the account's own policy says must be closed.

        ``market(instrument)`` returns a fresh MarketSnapshot for an instrument,
        or None when the venue cannot be read. Returns one record per position
        it acted on or tried to act on, so an operator log shows the exits it
        took AND the exits it could not take: a stop that quietly does nothing
        is worse than no stop at all.

        This is the only path here that can close a position without a leader
        event. It can never open or add - the authority it uses reduces only -
        and Risk, the ledger, reconciliation and the adapter's own scope check
        all still run exactly as they do for a copied exit. In particular an
        unresolved execution still blocks it, because closing on a position we
        cannot currently size is how one stuck order becomes two.
        """
        from pathlib import Path
        from core.ai_review import account_guard
        from core.position_protection import protection_reasons
        from core.position_episodes import PositionEpisode
        if self.protection is None or self.auth_policy.mode not in ('PAPER_AUTO','LIVE_AUTO'): return []
        scope=self.auth_policy.scope
        results=[]
        with account_guard(Path(self.store.path).parent,scope.account):
            now=self.clock()
            with self.store.transaction() as db:
                portfolio=self.store.portfolio_in(db,scope)
                pending=bool(db.execute("SELECT 1 FROM intents WHERE scope=? AND status IN ('SUBMITTING','UNKNOWN','PARTIAL')",
                    (scope_key(scope),)).fetchone())
                episodes=[PositionEpisode.model_validate_json(r[0]) for r in
                    db.execute('SELECT body FROM position_episodes WHERE scope=?',(scope_key(scope),)).fetchall()]
                if not pending and portfolio.evidence=='FAKE' and now>portfolio.received_ms:
                    # Same renewal the decision path performs: local PAPER state
                    # is authoritative, and a stop must not be refused for the
                    # staleness of a watermark only we advance.
                    portfolio=portfolio.model_copy(update={'revision':portfolio.revision+1,'exchange_ms':now,'received_ms':now})
                    self.store.publish_portfolio_in(db,portfolio,'protection-'+str(now))
            for episode in episodes:
                if episode.mode!=self.auth_policy.mode or episode.state not in {'OPEN','INCREASED','REDUCED','PARTIAL'}:
                    continue
                if self.evidence is not None and not pending:
                    fresh=self.evidence(portfolio.revision+1,episode.instrument.dex)
                    if fresh is not None:
                        fresh=carried_attribution(fresh,portfolio)
                        with self.store.transaction() as db:
                            self.store.publish_portfolio_in(db,fresh,'protection-'+str(now))
                        portfolio=fresh
                position=next((p for p in portfolio.positions
                               if p.instrument==episode.instrument and p.evidence=='VERIFIED'),None)
                if position is None: continue
                snapshot=market(episode.instrument)
                reasons=protection_reasons(position,snapshot.price if snapshot is not None else None,
                                           episode.created_ms,now,self.protection)
                if not reasons: continue
                entry={'episode_id':episode.episode_id,'symbol':episode.instrument.symbol,'reasons':list(reasons)}
                if snapshot is None:
                    results.append(dict(entry,status='MARKET_UNAVAILABLE'));continue
                try:
                    results.append(dict(entry,**self._protective_exit(episode,position,snapshot,reasons,now)))
                except Exception as exc:
                    # A refused stop is a fact the operator needs, not a crash
                    # that stops the remaining positions being checked.
                    results.append(dict(entry,status='REFUSED',detail=_fault(exc)))
        return results

    def _protective_exit(self,episode,position,market,reasons,now):
        side='SELL' if position.side=='LONG' else 'BUY'
        limit=(market.bid if side=='SELL' else market.ask) or market.price
        live=self.auth_policy.mode=='LIVE_AUTO'
        # One attempt per minute per episode: a retry inside the same minute is
        # the same intent identity, and a retry after a refusal is a new one.
        action_id=self._child_event_id(episode.episode_id,'protect|%d'%(now//60000))
        intent=OrderIntent(version=3 if live else 1,intent_id=action_id,scope=episode.scope,
            instrument=episode.instrument,source=self.allocation_policy.source,action='CLOSE',side=side,
            size=position.size,limit_price=limit,leverage=position.leverage,
            slippage_pct=self.risk.policy.max_slippage_pct,
            authorization='AUTONOMOUS_POLICY' if live else 'PAPER_POLICY',
            execution_mode='LIVE' if live else 'PAPER',correlation_id=action_id,
            created_ms=now,expires_ms=now+self.auth_policy.max_signal_age_ms)
        body={'version':'protective-exit-v1','authority':'PROTECTION_POLICY','mode':self.auth_policy.mode,
            'leader':episode.leader,'agents':[],'consensus':None,
            # Derived, and labelled as derived. No leader event caused this, and
            # none is invented: these fields describe the action we are taking.
            'event':{'wallet':episode.leader,'action':'CLOSE','side':side,'derived_from':'PROTECTION_POLICY',
                     'instrument':episode.instrument.model_dump(mode='json'),'exchange_ms':now},
            'protection':{'policy':self.protection.model_dump(mode='json'),'reasons':list(reasons),
                          'held_ms':now-episode.created_ms,'mark':market.price,
                          'liquidation_price':position.liquidation_price},
            'market':market.model_dump(mode='json'),
            'authorization':{'outcome':'AUTHORIZED','authority':'PROTECTION_POLICY','mode':self.auth_policy.mode,
                             'created_ms':now,'expires_ms':intent.expires_ms},
            'cost_model':None if live else self.cost_model.model_dump(mode='json')}
        with self.store.transaction() as db:
            self.episodes.prepare_protective_in(db,body,intent,episode)
        self.gateway.authorize_protective(intent,{'reasons':list(reasons),'episode_id':episode.episode_id,
            'policy':self.protection.model_dump(mode='json')})
        ledger=AutonomousLedger(self.store.portfolio(episode.scope),self.allocation_policy)
        receipt=self.gateway.execute(intent,market,autonomous_ledger=ledger)
        final=self.episodes.sync(intent.intent_id,episode.scope)
        return {'status':receipt.status,'intent_id':intent.intent_id,
                'episode_state':final.state if final else None,
                'risk':None if receipt.status!='REJECTED' else 'RISK_REJECTED'}

    def process(self,record):
        from pathlib import Path
        from core.ai_review import account_guard
        event=LeaderTradeEvent.model_validate(record['event'])
        if event.action=='REVERSE':return self.reverse(record)
        with account_guard(Path(self.store.path).parent,self.auth_policy.scope.account):
            job=self.jobs.claim(event.event_id,record)
            if job['stage']=='QUARANTINED':return {'status':'QUARANTINED','event_id':event.event_id}
            if job['stage']=='SKIPPED_HISTORICAL':return {'status':'SKIPPED_NO_FOLLOWER_POSITION','event_id':event.event_id}
            try:
                result=self._process(record)
                self.jobs.stage(event.event_id,'RECONCILING' if result.get('status') in {'UNKNOWN','PARTIAL','SUBMITTING'} else
                    'AWAITING_CONFIRMATION' if result.get('status')=='CONFIRMATION_REQUIRED' else 'COMPLETED')
                return result
            except Exception as exc:
                with self.store.transaction() as db:
                    financial=db.execute('SELECT intent FROM autonomous_decisions WHERE scope=? AND event_id=?',
                        (scope_key(self.auth_policy.scope),event.event_id)).fetchone()
                self.jobs.stage(event.event_id,'RECOVERY_REQUIRED' if financial and financial['intent'] else 'QUARANTINED',_fault(exc))
                raise

    def _live_guard_reasons(self,db,portfolio,now,action):
        """Account-level stops, evaluated only for unattended live entries.

        REDUCE/CLOSE are deliberately exempt: every stop here exists to prevent
        taking MORE risk, and none of them should ever prevent shedding it.
        """
        guard=self.live_guard
        if guard is None or action in {'REDUCE','CLOSE'}: return ()
        db.execute('''CREATE TABLE IF NOT EXISTS live_guard_state(scope TEXT PRIMARY KEY,day TEXT,
                      opening_equity REAL,halted INTEGER NOT NULL DEFAULT 0)''')
        key=scope_key(self.auth_policy.scope)
        day=time.strftime('%Y-%m-%d',time.gmtime(now/1000))
        row=db.execute('SELECT day,opening_equity,halted FROM live_guard_state WHERE scope=?',(key,)).fetchone()
        equity=portfolio.equity
        if row is None or row['day']!=day:
            # A new UTC day re-arms the daily stop and records its baseline.
            db.execute('''INSERT INTO live_guard_state VALUES(?,?,?,0) ON CONFLICT(scope) DO UPDATE
                          SET day=excluded.day,opening_equity=excluded.opening_equity,halted=0''',(key,day,equity))
            opening,halted=equity,0
        else:
            opening,halted=row['opening_equity'],row['halted']
        reasons=[]
        if guard.halted or halted: reasons.append('LIVE_GUARD_HALTED')
        if guard.daily_loss_limit>0:
            if equity is None or opening is None: reasons.append('LIVE_GUARD_EQUITY_UNKNOWN')
            elif opening-equity>=guard.daily_loss_limit:
                reasons.append('LIVE_GUARD_DAILY_LOSS')
                # Latch it: the rest of the day stays closed to new entries
                # even if equity recovers, until the next UTC day re-arms it.
                db.execute('UPDATE live_guard_state SET halted=1 WHERE scope=?',(key,))
        if sum(1 for p in portfolio.positions if p.size>0)>=guard.max_concurrent_positions:
            reasons.append('LIVE_GUARD_POSITION_LIMIT')
        return tuple(reasons)

    def _size_step(self,instrument):
        """Venue lot step for this instrument; the policy step is the fallback.

        A resolver failure must not stall the pipeline, so the policy step is
        used and the decision body records which source was applied.
        """
        if self.precision is None: return self.risk.policy.size_step,'POLICY'
        try: step=self.precision(instrument)
        except Exception: step=None
        if not isinstance(step,(int,float)) or isinstance(step,bool): return self.risk.policy.size_step,'POLICY_FALLBACK'
        step=float(step)
        if not math.isfinite(step) or step<=0: return self.risk.policy.size_step,'POLICY_FALLBACK'
        return step,'VENUE'

    @staticmethod
    def _whole(value):
        """A resolver answer only counts as leverage if it is a real integer >= 1."""
        if isinstance(value,bool) or not isinstance(value,(int,float)): return None
        if not math.isfinite(float(value)): return None
        value=int(value)
        return value if value>=1 else None

    def _leverage(self,instrument,position,wallet):
        """Leverage for this order, clamped to what the venue actually allows.

        An open position keeps its own leverage untouched: changing leverage
        under a live position is a separate operation, not something a reduce
        should smuggle in. For an entry we copy the leverage the LEADER is
        running, because copying a 3x trade at 20x is a different trade with a
        different liquidation distance, and fall back to the policy maximum
        only when that is unavailable.

        Either way the result is clamped by the market ceiling. Most alt perps
        and every stock cap well below the crypto maximum, and Hyperliquid
        rejects an order asking for more, so an unclamped policy maximum did
        not merely mis-size those markets - it excluded them.
        """
        if position is not None: return max(1,int(position.leverage)),'POSITION'
        cap=min(self.allocation_policy.max_leverage,self.risk.policy.max_leverage)
        market=None
        if self.ceilings is not None:
            try: market=self._whole(self.ceilings(instrument))
            except Exception: market=None
        observed=None
        if self.leader_leverage is not None:
            try: observed=self._whole(self.leader_leverage(wallet,instrument))
            except Exception: observed=None
        chosen,source=(observed,'LEADER') if observed is not None else (cap,'POLICY')
        limit=cap if market is None else min(cap,market)
        final=max(1,min(int(chosen),int(limit)))
        if market is None: source+='_UNVERIFIED_MARKET'
        elif final!=int(chosen): source+='_CLAMPED'
        return final,source

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
        if self.evidence is not None and not pending:
            # Deliberately outside the transaction above: this reads the
            # exchange, and holding the shared write lock across a network call
            # would stall every other consumer of this store.
            fresh=self.evidence(before.revision+1,event.instrument.dex)
            if fresh is not None:
                fresh=carried_attribution(fresh,before)
                with self.store.transaction() as db:
                    self.store.publish_portfolio_in(db,fresh,event.event_id)
                before=fresh
        reservations=[Reservation(**json.loads(r[0])) for r in pending]
        ledger=AutonomousLedger(before,self.allocation_policy,reservations)
        position=next((p for p in before.positions if p.instrument==event.instrument),None) if episode else None
        if episode is None and event.action in {'ADD','REDUCE','CLOSE'}:
            # A leader action is not evidence that this follower entered the
            # original trade. Never turn ADD into OPEN or adopt other exposure.
            unexplained=any(p.instrument==event.instrument for p in before.positions)
            body={'event':event.model_dump(mode='json'),'mode':self.auth_policy.mode,
                'status':'WAIT' if unexplained else 'SKIP','reason':'FOLLOWER_OWNERSHIP_UNPROVEN' if unexplained else 'NO_FOLLOWER_POSITION',
                'correlation_id':event.event_id,'submission':False}
            with self.store.transaction() as db:
                db.execute('INSERT INTO autonomous_decisions VALUES(?,?,?,?,NULL)',
                    (hashlib.sha256((scope_key(scope)+'|'+event.event_id+'|no-follower').encode()).hexdigest(),scope_key(scope),event.event_id,json.dumps(body)))
            return body
        reducing=event.action in {'REDUCE','CLOSE'}
        book=record['book']
        bid=float(book['levels'][0][0]['px']); ask=float(book['levels'][1][0]['px'])
        # The decision already carries the book it was made on, so resting
        # depth costs nothing extra here. Thinner side of the top five levels,
        # matching what the liquidity agent measures.
        try:
            depth=min(math.fsum(float(x['px'])*float(x['sz']) for x in side[:5]) for side in book['levels'][:2])
            if not math.isfinite(depth) or depth<0: depth=None
        except (KeyError,TypeError,ValueError): depth=None
        market=MarketSnapshot(instrument=event.instrument,exchange_ms=book['time'],received_ms=record['consensus']['created_ms'],
            price=bid+(ask-bid)/2,bid=bid,ask=ask,depth_usd=depth,completeness='COMPLETE',freshness='FRESH',source='REST',source_version='intelligence-book-v1')
        limit=ask if event.side=='BUY' else bid
        leverage,leverage_source=self._leverage(event.instrument,position,event.wallet)
        step,step_source=self._size_step(event.instrument)
        floor=self.risk.policy.min_notional
        context=None
        if not ledger.errors:
            upper=0. if reducing else ledger.size(limit,leverage,step,leader.confidence,1.,floor)
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
            agents=evaluate(event,leader,record['candles'],book,now,policy,context=context,actionable=True,
                flow=record.get('flow'))
            result=consensus(event,agents,now,policy,position=position)
            with self.store.transaction() as db:
                db.execute('INSERT INTO autonomous_analysis VALUES(?,?,?)',(scope_key(scope),event.event_id,
                    json.dumps({'agents':[a.model_dump(mode='json') for a in agents],'consensus':result.model_dump(mode='json')})))
        self.jobs.stage(event.event_id,'ANALYZED')
        auth=self.authorization.decide(self.auth_policy,event,result,now)
        self.jobs.stage(event.event_id,'AUTHORIZED')
        guard=()
        if auth.mode=='LIVE_AUTO' and auth.outcome=='AUTHORIZED':
            with self.store.transaction() as db:
                guard=self._live_guard_reasons(db,before,now,event.action)
        body={'event':event.model_dump(mode='json'),'agents':[a.model_dump(mode='json') for a in agents],
            'consensus':result.model_dump(mode='json'),'authorization':auth.model_dump(mode='json'),
            'mode':auth.mode,'status':auth.outcome,'correlation_id':event.event_id,
            'market':market.model_dump(mode='json'),'leader':leader.model_dump(mode='json'),
            'allocation':ledger.allocation(self.allocation_policy.source).model_dump(mode='json') if not ledger.errors else None,
            'allocation_policy':self.allocation_policy.model_dump(mode='json'),
            'sizing':{'size_step':step,'size_step_source':step_source,'min_notional':floor,
                      'leverage':leverage,'leverage_source':leverage_source,
                      'instrument_scope':'MULTI' if self.risk.multi_instrument else 'SINGLE'},
            'live_guard':{'policy':self.live_guard.model_dump(mode='json') if self.live_guard else None,
                          'blocked_by':list(guard)}}
        if guard: body['status']='LIVE_GUARD_BLOCKED'
        body['cost_model']=self.cost_model.model_dump(mode='json') if auth.mode!='LIVE_CONFIRM' else None
        intent=None
        if not guard and ((auth.outcome=='AUTHORIZED' and auth.execution_mode in {'PAPER','LIVE'})
                          or auth.outcome in {'HYPOTHETICAL','CONFIRMATION_REQUIRED'}):
            if reducing:
                from decimal import Decimal,ROUND_FLOOR
                fraction=min(1.,event.size/abs(event.before_size)) if event.before_size else 0.
                raw=position.size*(1. if event.action=='CLOSE' else fraction) if position else 0.
                size=float((Decimal(str(raw))/Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR)*Decimal(str(step)))
                if event.action=='CLOSE' and position: size=position.size
            else:
                size=ledger.size(limit,leverage,step,leader.confidence,result.confidence,floor)
            if size>0:
                live=auth.mode in {'LIVE_CONFIRM','LIVE_AUTO'}
                intent=OrderIntent(version=3 if live else 1,intent_id=auth.decision_id,scope=scope,instrument=event.instrument,source=self.allocation_policy.source,
                    action=event.action,side=event.side,size=size,limit_price=limit,leverage=leverage,slippage_pct=self.risk.policy.max_slippage_pct,
                    authorization=('AUTONOMOUS_POLICY' if auth.mode=='LIVE_AUTO' else 'USER_CONFIRMED') if live else 'PAPER_POLICY',
                    execution_mode='LIVE' if live else 'PAPER',
                    configure_leverage=live,correlation_id=event.event_id,created_ms=auth.created_ms,expires_ms=auth.expires_ms)
                if auth.mode=='LIVE_CONFIRM': body['proposal']=intent.model_dump(mode='json')
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
        if intent is not None and auth.mode in {'PAPER_AUTO','LIVE_AUTO'}:
            self.jobs.stage(event.event_id,'SUBMISSION_PENDING')
            # Same gateway, same ledger, same reconciliation. The only thing
            # LIVE_AUTO changes is which grant the gateway will accept, and
            # that grant still has to match the durable authorization record.
            if auth.mode=='LIVE_AUTO': self.gateway.authorize_live(intent,auth)
            else: self.gateway.authorize_paper(intent,auth)
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
                        elif self.auth_policy.mode=='LIVE_AUTO':
                            # No canonical reservation exists, so nothing was ever
                            # signed: the gateway reserves before it submits. Do not
                            # re-sign it here. The copy window is tens of seconds, so
                            # an entry that survived a crash is stale even while its
                            # grant is technically valid, and this path never evaluates
                            # the live guard - halted, daily loss and concurrency are
                            # only checked on the decision path. Abandon the unsent
                            # action and let the next leader event decide afresh.
                            # Without this branch a LIVE_AUTO job fell into the
                            # confirmation wait below and stalled forever, because no
                            # human confirmation is ever minted in this mode.
                            body['status']='ABANDONED_BEFORE_SUBMISSION'
                            from core.position_episodes import PositionEpisode
                            with self.store.transaction() as db:
                                episode=db.execute('SELECT p.body FROM position_episodes p JOIN episode_actions a ON a.episode=p.id WHERE a.id=?',(row['id'],)).fetchone()
                                if episode:self.episodes.transition_in(db,PositionEpisode.model_validate_json(episode[0]),row['id'],'REJECTED',{'reason':'ABANDONED_BEFORE_SUBMISSION','no_submission':True})
                                db.execute('UPDATE autonomous_decisions SET body=? WHERE id=?',(json.dumps(body),row['id']))
                            self.jobs.stage(job['event_id'],'COMPLETED');continue
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
                    self.jobs.stage(job['event_id'],'RECOVERY_REQUIRED' if financial and financial['intent'] else 'QUARANTINED',_fault(exc))

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


def load_paper_backend(config_path,state_directory,network,clock,precision=None,exchange=None,evidence=None,
                       ceilings=None,leader_leverage=None):
    """Explicit operator-configured isolated runtime; never loads profiles.

    ``precision`` resolves a venue lot step, ``ceilings`` the venue leverage
    ceiling for an instrument, and ``leader_leverage`` the leverage a leader is
    actually running there. All three are read-only resolvers supplied by the
    caller that owns a reader; none of them can place an order.

    ``multi_instrument``
    and ``symbols`` in the config let one policy cover every market a leader
    trades; both default off, so an unlisted symbol is still a scope error.

    ``exchange`` stays None for PAPER/SHADOW and this builds the FakeExchange
    itself. A LIVE_AUTO configuration must be handed a signing adapter by the
    caller: this loader constructs no credentials, which is what keeps the
    research worker unable to sign even if it is pointed at a live config.
    """
    from pathlib import Path
    from contextlib import closing
    import sqlite3
    from pydantic import BaseModel,ConfigDict
    from core.foundation.autonomous_allocation import AutonomousAllocationPolicy
    from core.foundation.risk import RiskPolicy
    from core.foundation.contracts import Positive,PortfolioSnapshot
    from core.foundation.store import Store
    from core.foundation.paper_costs import PaperCosts
    from core.position_protection import PositionProtectionPolicy
    class Config(BaseModel):
        model_config=ConfigDict(extra='forbid')
        allocation: AutonomousAllocationPolicy
        authorization: AuthorizationPolicy
        risk: RiskPolicy
        initial_paper_equity: Positive
        costs: PaperCosts=PaperCosts()
        # One policy, every market the leader trades. Off by default so an
        # existing configuration keeps its single-instrument scope check.
        multi_instrument: bool=False
        symbols: tuple[str,...]=()
        live_guard: LiveGuardPolicy|None=None
        protection: PositionProtectionPolicy|None=None
    config=Config.model_validate_json(Path(config_path).read_text(encoding='utf-8'))
    if config.authorization.scope.network!=network: raise ValueError('Explicit configuration required')
    if config.authorization.mode not in ('OBSERVE','PAPER_AUTO','SHADOW','LIVE_AUTO'):
        raise ValueError('Explicit PAPER configuration required')
    if (config.authorization.mode=='LIVE_AUTO')!=(exchange is not None):
        # A live configuration without a signing adapter, or a simulated
        # configuration handed one, is a wiring mistake and never a default.
        raise ValueError('LIVE_AUTO requires a caller-supplied signing adapter; simulated modes forbid one')
    directory=Path(state_directory)
    for name,marker in (('autonomy.sqlite','autonomous_decisions'),('fake.sqlite','fake_orders')):
        path=directory/name
        if path.is_symlink() or directory.is_symlink(): raise ValueError('Unsafe PAPER path')
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables and marker not in tables: raise ValueError('Dedicated PAPER database required')
    store=Store(directory/'autonomy.sqlite')
    exchange=exchange if exchange is not None else FakeExchange(directory/'fake.sqlite')
    backend=AutonomousBackend(store,exchange,config.allocation,config.authorization,config.risk,clock,config.costs,
        multi_instrument=config.multi_instrument,symbols=config.symbols,precision=precision,
        live_guard=config.live_guard,evidence=evidence,ceilings=ceilings,leader_leverage=leader_leverage,
        protection=config.protection)
    with store.transaction() as db:
        row=db.execute('SELECT body FROM portfolios WHERE scope=?',(scope_key(config.authorization.scope),)).fetchone()
        # Only a simulated run may invent its own opening balance. A live scope
        # takes its portfolio from exchange evidence, never from a config file.
        if row is None and config.authorization.mode!='LIVE_AUTO':
            now=clock()
            portfolio=PortfolioSnapshot(scope=config.authorization.scope,revision=1,exchange_ms=now,received_ms=now,
                equity=config.initial_paper_equity,sizing_capital=config.initial_paper_equity,available_collateral=config.initial_paper_equity,
                completeness='COMPLETE',evidence='FAKE')
            store.publish_portfolio_in(db,portfolio,'explicit-paper-seed')
    return backend
