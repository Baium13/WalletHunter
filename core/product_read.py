"""Tenant-scoped, bounded, read-only projections. No clients or trading authority."""
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from pydantic import Field
from core.foundation.contracts import Contract, Scope, PortfolioSnapshot,MarketSnapshot,OrderIntent,ExecutionReceipt
from core.foundation.store import scope_key
from core.foundation.autonomous_allocation import AutonomousAllocationPolicy, AutonomousLedger
from core.foundation.ledger import Reservation


class RuntimeBinding(Contract):
    scope: Scope
    # LIVE_AUTO is a real configured mode since the unattended consumer
    # landed. Excluding it here made a live runtime unreadable by the
    # product view, so the interface could not show that real money was
    # trading unattended - the one state it must never hide.
    mode: str = Field(pattern='^(OBSERVE|PAPER_AUTO|SHADOW|LIVE_CONFIRM|LIVE_AUTO)$')
    state_path: str
    config_path: str


def clean(value):
    """Finite JSON only. Never project credentials or free-form exception text."""
    if isinstance(value, dict):
        return {k:clean(v) for k,v in value.items() if not any(x in k.lower() for x in ('secret','private_key','api_key','credential','token','password','session_string','error_text'))}
    if isinstance(value,(list,tuple)):return [clean(v) for v in value]
    if isinstance(value,float) and not math.isfinite(value):return None
    return value


@contextmanager
def reader(path):
    path=Path(path)
    if not path.is_file() or any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('STORE_UNAVAILABLE')
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        yield db
    finally:db.close()


def rows(db,table,scope,limit=100):
    if table not in {'portfolios','autonomous_modes','autonomous_health','autonomous_decisions','intents','position_episodes',
        'autonomous_outcomes','calibration_records','manual_copy_runtime','manual_leader_configs','events'}:raise ValueError('TABLE')
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():return []
    return db.execute('SELECT * FROM '+table+' WHERE scope=? ORDER BY rowid DESC LIMIT ?',(scope,limit)).fetchall()


def body(row):return json.loads(row['body']) if row else None


def observed(stamp,now,*,failed=False,critical=False):
    fresh=isinstance(stamp,int) and 0<=now-stamp<120000
    return {'status':('UNHEALTHY' if critical else 'DEGRADED') if failed else
        'ACTIVE' if fresh else 'DEGRADED' if stamp is not None else 'UNKNOWN',
        'last_success_ms':stamp,'fresh':fresh,'critical':critical}


def agent_availability(agents):
    """Availability is not activity. Preserve every underlying signal state."""
    counts={name:sum(a.get('status')==name for a in agents) for name in
        ('READY','ACTIVE','WAITING','DEGRADED','OFFLINE','UNKNOWN')}
    return dict(total=len(agents),available=sum(counts[n] for n in ('READY','ACTIVE','WAITING')),
        active=counts['ACTIVE'],waiting=counts['WAITING'],ready=counts['READY'],
        degraded=counts['DEGRADED'],offline=counts['OFFLINE'],unknown=counts['UNKNOWN'])


AGENT_IDS=('structure','momentum','volatility','liquidity','order_flow','leader','risk_context')


def analysis_projection(latest,worker,now):
    """Separate loaded worker readiness from freshness of its last signal.

    An idle loaded agent is not offline. Historical outputs are never refreshed
    or passed back into trading. Shared heartbeat is explicitly labelled.
    """
    actual={a['agent_id']:a for a in (latest or {}).get('agents',[])}
    heartbeat=worker.get('heartbeat_ms')
    alive=type(heartbeat) is int and 0<=now-heartbeat<=90000
    configured=worker.get('readiness_version')=='configured-worker-v1' and worker.get('status') in {'HEALTHY','DEGRADED','READY','ACTIVE'}
    agents=[]
    for name in AGENT_IDS:
        a=actual.get(name);state=observed((a or {}).get('created_ms'),now)
        ready=alive and configured and name in worker.get('ready_components',[])
        if ready:state.update(readiness='READY',ready_observed_ms=heartbeat)
        state['signal_status']='UNAVAILABLE' if a is None else 'FRESH' if state['fresh'] else 'STALE'
        if ready and not state['fresh']:state.update(status='WAITING' if a else 'READY',activity='WAITING_FOR_EVENT')
        if a and state['fresh'] and a['direction'] in {'WAIT','BLOCK'}:state['status']='WAITING' if a['direction']=='WAIT' else 'DEGRADED'
        if not alive and type(heartbeat) is int:state['status']='OFFLINE'
        if worker.get('status')=='UNHEALTHY':state['status']='DEGRADED'
        missing=[]
        if name=='order_flow':missing=['AGGRESSOR_TRADE_FLOW_UNAVAILABLE']
        elif a and a.get('freshness')!='FRESH':missing=list(a.get('evidence',[]))
        agents.append(dict(agent_id=name,**state,result=a,heartbeat_ms=heartbeat,
            heartbeat_source='SHARED_ANALYSIS_WORKER',last_error=worker.get('error'),
            current_instrument=a.get('instrument') if a else None,missing_inputs=missing,latency_ms=None))
    c=(latest or {}).get('consensus');cs=observed((c or {}).get('created_ms'),now)
    if alive and configured and 'consensus' in worker.get('ready_components',[]) and not cs['fresh']:
        cs.update(status='READY',activity='WAITING_FOR_EVENT',ready_observed_ms=heartbeat)
    if not alive and type(heartbeat) is int:cs['status']='OFFLINE'
    if worker.get('status')=='UNHEALTHY':cs['status']='DEGRADED'
    cs.update(heartbeat_ms=heartbeat,decision_fresh=cs['fresh'])
    return dict(agents=agents,agent_summary=agent_availability(agents),consensus=c,consensus_health=cs,execution_authority=False)


def runtime_risk_health(worker,risk,execution,now):
    """Worker readiness and last decision are different observations.

    Poison jobs degrade processing, not a loaded risk evaluator. This projection
    grants no authority; every new intent still undergoes canonical validation.
    """
    signal=observed((risk or {}).get('created_ms'),now,critical=True)
    heartbeat=worker.get('heartbeat_ms')
    fresh=type(heartbeat) is int and 0<=now-heartbeat<=90000
    clear=all(type(worker.get(k)) is int and worker[k]==0 for k in
        ('unresolved_execution_count','reconciliation_backlog','processing_lag_ms')) and execution.get('reconciliation_backlog')==0
    poison_only=(worker.get('status')=='DEGRADED' and worker.get('quarantine_count',0)>0 and clear)
    ready=(fresh and clear and worker.get('readiness_version')=='configured-worker-v1'
        and 'risk' in worker.get('ready_components',[]) and (worker.get('status')=='HEALTHY' or poison_only))
    reasons=[]
    if ready:signal.update(status='READY',ready_observed_ms=heartbeat)
    elif not fresh:reasons.append('WORKER_HEARTBEAT_STALE_OR_UNAVAILABLE')
    elif not clear:reasons.append('UNRESOLVED_EXECUTION_OR_PROCESSING_BACKLOG')
    else:reasons.append('WORKER_READINESS_UNAVAILABLE')
    if not signal['fresh']:reasons.append('LAST_RISK_DECISION_STALE_OR_ABSENT')
    if not ready:
        signal['status']='UNHEALTHY' if worker.get('status')=='UNHEALTHY' else 'UNKNOWN' if not worker and not risk else 'DEGRADED'
    signal.update(reasons=reasons,heartbeat_ms=heartbeat,decision_fresh=signal['fresh'],
        last_decision_reasons=(risk or {}).get('reasons',[]),
        processing_degradation=['QUARANTINED_INPUT_JOBS'] if poison_only else [],
        quarantined_jobs=worker.get('quarantined_jobs'))
    return signal


def performance(outcomes):
    valid=[o for o in outcomes if o.get('version')=='outcome-v2']
    values=[o['net_pnl'] for o in valid if isinstance(o.get('net_pnl'),(int,float)) and math.isfinite(o['net_pnl'])]
    wins=[v for v in values if v>0];losses=[v for v in values if v<0]
    gp=sum(wins);gl=-sum(losses)
    def total(key):
        return sum(o[key] for o in valid) if valid and all(isinstance(o.get(key),(int,float)) and math.isfinite(o[key]) for o in valid) else None
    cumulative=peak=dd=0.
    for o in sorted(valid,key=lambda o:o.get('exit_ms',0)):
        if o.get('net_pnl') is None:continue
        cumulative+=o['net_pnl'];peak=max(peak,cumulative);dd=max(dd,peak-cumulative)
    return dict(trade_count=len(valid),sample_size=len(values),net_pnl=total('net_pnl'),gross_profit=gp if values else None,
        gross_loss=gl if values else None,win_rate=len(wins)/len(values) if values else None,
        profit_factor=gp/gl if gl else None,profit_factor_state='NO_LOSSES' if values and not gl else 'MEASURED' if gl else 'UNAVAILABLE',
        average_win=gp/len(wins) if wins else None,average_loss=-gl/len(losses) if losses else None,
        expectancy=sum(values)/len(values) if values else None,fees=total('fees'),slippage=total('slippage_vs_reference'),
        average_duration_ms=total('duration_ms')/len(valid) if valid and total('duration_ms') is not None else None,
        cumulative_realized_drawdown_proxy=dd if values and len(values)==len(valid) else None,
        drawdown_evidence='CUMULATIVE_REALIZED_PNL_NOT_ACCOUNT_EQUITY',window='LATEST_100_OUTCOMES')


class ProductReadModel:
    def __init__(self,root,scope,bindings=(),clock=lambda:int(time.time()*1000),research_path=None,sources=()):
        self.root=Path(root);self.scope=Scope.model_validate_json(scope.model_dump_json());self.clock=clock
        self.bindings=tuple(b for b in bindings if b.scope==scope)
        self.research_path=Path(research_path or self.root/'data/intelligence.sqlite')
        self.sources=tuple(sources)

    def _path(self,path):
        p=Path(path);return p if p.is_absolute() else self.root/p

    def _store(self,path,mode=None,config=None):
        scope=self.scope;key=scope_key(scope);now=self.clock()
        with reader(path) as db:
            owner=db.execute('SELECT tenant FROM owners WHERE network=? AND account=?',(scope.network,scope.account)).fetchone()
            if not owner or owner[0]!=scope.tenant:raise ValueError('STORE_SCOPE_UNVERIFIED')
            p=rows(db,'portfolios',key,1);portfolio=PortfolioSnapshot.model_validate(body(p[0])) if p else None
            if portfolio and portfolio.scope!=scope:raise ValueError('PORTFOLIO_SCOPE_MISMATCH')
            raw=rows(db,'intents',key);decisions=rows(db,'autonomous_decisions',key)
            episodes=[body(r) for r in rows(db,'position_episodes',key)]
            from core.position_episodes import PositionEpisode
            if any(PositionEpisode.model_validate(e).scope!=scope or (mode is not None and e['mode']!=mode) for e in episodes):raise ValueError('EPISODE_SCOPE_MISMATCH')
            outcomes=[body(r) for r in rows(db,'autonomous_outcomes',key)]
            calibrations=[body(r) for r in rows(db,'calibration_records',key) if body(r).get('version')=='evaluation-v2']
            health=rows(db,'autonomous_health',key,1);health=body(health[0]) if health else {}
            runtime=rows(db,'autonomous_modes',key,1);runtime=runtime[0]['mode'] if runtime else None
            if mode and runtime!=mode:raise ValueError('RUNTIME_MODE_MISMATCH')
            # Active reservation reads are not truncated to the UI history window.
            pending=db.execute("SELECT * FROM intents WHERE scope=? AND status IN ('SUBMITTING','UNKNOWN','PARTIAL') LIMIT 501",(key,)).fetchall()
            if len(pending)>500:raise ValueError('RESERVATION_READ_LIMIT')
            reservations=[Reservation(**json.loads(r['reservation'])) for r in pending if r['reservation']]
            allocation=None
            if portfolio and config:
                policy=AutonomousAllocationPolicy.model_validate(config['allocation'])
                if policy.scope!=scope:raise ValueError('CONFIG_SCOPE_MISMATCH')
                ledger=AutonomousLedger(portfolio,policy,reservations)
                allocation={'source':policy.source,'allocation_limit':policy.allocation_limit,'committed':None,'reserved':None,'available':None,
                    'account_capacity':ledger.available_capacity,'status':'RECONCILIATION_REQUIRED' if ledger.errors else 'VERIFIED',
                    'reasons':ledger.errors,'unknown_reserved':sum(json.loads(r['reservation'])['margin'] for r in pending
                        if r['status']=='UNKNOWN' and r['reservation'] and json.loads(r['reservation'])['source']==policy.source)}
                if not ledger.errors:
                    a=ledger.allocation(policy.source);allocation.update(committed=a.committed,reserved=a.reserved,available=a.available)
            actions=[]
            for r in raw:
                intent=OrderIntent.model_validate_json(r['body'])
                if intent.scope!=scope or intent.intent_id!=r['id']:raise ValueError('INTENT_SCOPE_MISMATCH')
                i=intent.model_dump(mode='json');receipt=None
                if r['receipt']:
                    parsed=ExecutionReceipt.model_validate_json(r['receipt'])
                    if parsed.scope!=scope or parsed.intent_id!=r['id']:raise ValueError('RECEIPT_SCOPE_MISMATCH')
                    receipt=parsed.model_dump(mode='json')
                origin='AUTONOMOUS' if mode else 'COPY' if i['authorization']=='COPY_POLICY' else 'MANUAL' if i['source']=='manual' else None
                if not mode and db.execute("SELECT 1 FROM sqlite_master WHERE name='operations'").fetchone():
                    parent=db.execute('SELECT intent FROM operations WHERE id=? AND account=?',(i.get('parent_intent_id'),scope.account)).fetchone()
                    if parent:
                        envelope=json.loads(parent[0])
                        if envelope.get('network')==scope.network and envelope.get('strategy')=='MANUAL_LEADER_COPY':origin='MANUAL_LEADER_COPY'
                actions.append(dict(intent_id=r['id'],intent=i,status=r['status'],risk=json.loads(r['decision']) if r['decision'] else None,
                    receipt=receipt,correlation_id=i['correlation_id'],timestamp=i['created_ms'],origin=origin))
            episode_views=[]
            for episode in episodes:
                linked=db.execute('SELECT id FROM episode_actions WHERE episode=?',(episode['episode_id'],)).fetchall()
                ids={r[0] for r in linked};matching=[a for a in actions if a['intent_id'] in ids]
                predictions=db.execute('SELECT p.body FROM autonomous_predictions p JOIN episode_actions a ON a.id=p.id WHERE a.episode=? AND p.scope=? ORDER BY p.rowid LIMIT 100',(episode['episode_id'],key)).fetchall()
                predictions=[body(p) for p in predictions]
                outcome=next((o for o in outcomes if o.get('episode_id')==episode['episode_id'] and o.get('version')=='outcome-v2'),None)
                order_ids={oid for a in matching if a['status'] in {'FILLED','PARTIAL'} for oid in (a['receipt'] or {}).get('order_ids',[])}
                position=next((p for p in (portfolio.positions if portfolio else ()) if p.instrument.model_dump(mode='json')==episode['instrument'] and p.evidence=='VERIFIED' and order_ids.intersection(p.order_ids)),None)
                first=predictions[0] if predictions else {};latest=matching[0] if matching else None
                # SHADOW inventory has no exchange identifiers; scoped hypothetical intent identity is its proof.
                if mode=='SHADOW' and portfolio:
                    position=next((p for p in portfolio.positions if p.instrument.model_dump(mode='json')==episode['instrument'] and any(c.source==config['allocation']['source'] for c in p.contributions)),None)
                quote=None
                for decision in decisions:
                    raw_quote=body(decision).get('market')
                    if not raw_quote:continue
                    candidate=MarketSnapshot.model_validate(raw_quote)
                    if candidate.instrument.model_dump(mode='json')==episode['instrument']:
                        if candidate.exchange_ms is not None and 0<=now-candidate.exchange_ms<=30000 and candidate.freshness=='FRESH':quote=candidate
                        break
                unrealized=(quote.price-position.entry_price)*position.size*(1 if position.side=='LONG' else -1) if quote and quote.price and position else None
                detail=dict(episode,origin='AUTONOMOUS',leader=episode['leader'],actions=matching,
                    side=position.side if position else (outcome or {}).get('direction'),
                    entry=position.entry_price if position else (outcome or {}).get('entry_price'),
                    current_price=quote.price if quote else None,mark_timestamp=quote.exchange_ms if quote else None,
                    exit_price=(outcome or {}).get('exit_price'),size=position.size if position else (outcome or {}).get('filled_size'),
                    margin=position.margin if position else None,notional=position.notional if position else None,leverage=position.leverage if position else None,
                    pnl=outcome.get('net_pnl') if outcome else unrealized,pnl_kind='REALIZED' if outcome else 'UNREALIZED_MARK_EXCLUDES_FEES' if unrealized is not None else 'UNAVAILABLE',
                    outcome=outcome,current_action_state=latest['status'] if latest else first.get('status'),
                    unresolved=episode['state'] in {'UNKNOWN','PARTIAL','RECONCILIATION_REQUIRED'} or any(a['status'] in {'UNKNOWN','PARTIAL','SUBMITTING'} for a in matching),correlation_id=episode['first_event_id'],timeline=[])
                detail['recorded_state']=episode['state']
                if episode['state']=='REJECTED' and position is not None:
                    # Preserve Block 2's legacy read-through: an action-level
                    # rejection cannot hide exact proven, attributable exposure.
                    detail.update(state='RECONCILIATION_REQUIRED',unresolved=True)
                for prediction in predictions:
                    for field,kind in [('event','LEADER_EVENT'),('agents','ANALYSIS'),('consensus','CONSENSUS'),('authorization','AUTHORIZATION'),('risk','RISK')]:
                        value=prediction.get(field)
                        stamp=max((a['created_ms'] for a in value),default=None) if isinstance(value,list) else (value or {}).get('exchange_ms' if field=='event' else 'created_ms')
                        if value is not None:detail['timeline'].append(dict(type=kind,timestamp=stamp,correlation_id=prediction.get('event',{}).get('event_id'),evidence=value))
                for action in matching:
                    detail['timeline'].append(dict(type='EXECUTION',timestamp=(action['receipt'] or {}).get('received_ms',action['timestamp']),status=action['status'],correlation_id=action['correlation_id'],intent_id=action['intent_id'],evidence=action['receipt']))
                for t in db.execute('SELECT state,evidence FROM episode_transitions WHERE episode=? ORDER BY seq LIMIT 200',(episode['episode_id'],)):
                    detail['timeline'].append(dict(type='POSITION_TRANSITION',status=t['state'],evidence=json.loads(t['evidence']),timestamp=None,correlation_id=episode['first_event_id']))
                if outcome:detail['timeline'].append(dict(type='OUTCOME',timestamp=outcome['recorded_ms'],evidence=outcome,correlation_id=episode['first_event_id']))
                episode_views.append(detail)
            latest=body(decisions[0]) if decisions else None
            analysis=analysis_projection(latest,health,now)
            return clean(dict(mode=mode,runtime_mode=runtime,configured_mode=mode,last_transition_ms=None,worker=health,allocation=allocation,
                portfolio=portfolio.model_dump(mode='json') if portfolio else None,episodes=episode_views,actions=actions,
                latest_decision=latest,**analysis,
                risk=actions[0]['risk'] if actions else None,analytics=performance(outcomes),calibration=calibrations,
                execution={'pending':sum(r['status']=='SUBMITTING' for r in pending),'unknown':sum(r['status']=='UNKNOWN' for r in pending),
                    'partial':sum(r['status']=='PARTIAL' for r in pending),'reconciliation_backlog':len(pending)+sum(e['unresolved'] and not any(a['status'] in {'SUBMITTING','PARTIAL','UNKNOWN'} for a in e['actions']) for e in episode_views),
                    'last_error':next(({'code':'EXECUTION_UNKNOWN','intent_id':a['intent_id'],'timestamp':(a['receipt'] or {}).get('received_ms')} for a in actions if a['status']=='UNKNOWN'),None),
                    'last_success_ms':max(((a['receipt'] or {}).get('received_ms',0) for a in actions if (a['receipt'] or {}).get('reconciliation')=='CONFIRMED'),default=None)}))

    def shared_analysis(self,modes):
        """Network-public research, independent of displayed financial mode."""
        latest=None;worker={};flow={}
        try:
            with reader(self.research_path) as db:
                row=db.execute("SELECT body FROM intelligence_records WHERE network=? AND kind='DECISION' ORDER BY rowid DESC LIMIT 1",(self.scope.network,)).fetchone()
                latest=body(row)
                row=db.execute("SELECT body FROM component_health WHERE network=? AND component='leader_detection'",(self.scope.network,)).fetchone()
                observation=body(row) or {}
                # Successful scan in this same process + prior seven outputs.
                # No claim of seven independent services or subscriptions.
                if latest and {a['agent_id'] for a in latest.get('agents',[])}==set(AGENT_IDS):
                    worker=dict(observation,status='UNHEALTHY' if observation.get('error') else 'HEALTHY',
                        readiness_version='configured-worker-v1',ready_components=[*AGENT_IDS,'consensus'])
                tail=db.execute("SELECT MAX(rowid),MAX(created) FROM intelligence_records WHERE network=? AND kind='DECISION'",(self.scope.network,)).fetchone()
                flow={'latest_decision_seq':tail[0],'last_research_ms':tail[1]}
                if latest:
                    book=latest.get('book') or {};candles=latest.get('candles') or []
                    flow.update(event=latest.get('event'),candle_count=len(candles),last_closed_candle_ms=candles[-1].get('T') if candles else None,
                        book_exchange_ms=book.get('time'),book_depth_levels=[len(x) for x in book.get('levels',[])],
                        evidence_time='CAPTURED_FOR_LAST_ANALYSIS_NOT_CURRENT_MARKET',order_flow='AGGRESSOR_TRADE_FLOW_UNAVAILABLE')
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):worker={}
        # Explicit configured worker observation supports readiness before its
        # first signal too. Only heartbeat/capabilities cross this boundary.
        for m in modes:
            h=m.get('worker',{})
            if h.get('readiness_version')=='configured-worker-v1' and h.get('heartbeat_ms',0)>worker.get('heartbeat_ms',0):worker=h
        return clean(dict(**analysis_projection(latest,worker,self.clock()),scope=self.scope.model_dump(mode='json'),
            analysis_scope='PUBLIC_RESEARCH',input_evidence=flow,shared_across_modes=['OBSERVE','PAPER_AUTO','SHADOW','LIVE_CONFIRM']))

    def discovery(self):
        result={'status':'UNKNOWN','counts':None,'leaders':[],'coverage':{'meaning':'CURRENTLY_OBSERVED_OR_SUBSCRIBED_MARKETS','instruments':None}}
        try:
            with reader(self.research_path) as db:
                counts={r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM candidates WHERE network=? GROUP BY status',(self.scope.network,))}
                result['counts']={**counts,'OBSERVED':sum(counts.values())}
                result['registry']={'total':sum(counts.values()),'total_limit':None,
                    'cold':sum(counts.get(k,0) for k in ('ARCHIVED','INACTIVE','ZERO_SUPPORTED_CAPITAL','HIGH_FREQUENCY'))}
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_segments'").fetchone():
                    result['research_sectors']={r[0]:r[1] for r in db.execute('SELECT sector,COUNT(*) FROM candidate_segments WHERE network=? GROUP BY sector',(self.scope.network,))}
                    result['sector_details']=[dict(r) for r in db.execute('SELECT wallet,sector,reason,checked_ms,recheck_ms FROM candidate_segments WHERE network=? ORDER BY checked_ms DESC LIMIT 32',(self.scope.network,))]
                for r in db.execute("SELECT wallet,status,last_seen,analysis FROM candidates WHERE network=? AND status NOT IN ('ARCHIVED','INACTIVE','ZERO_SUPPORTED_CAPITAL','HIGH_FREQUENCY') ORDER BY score DESC,wallet LIMIT 32",(self.scope.network,)):
                    analysis=json.loads(r['analysis']) if r['analysis'] else None
                    if isinstance(analysis,dict):analysis={k:analysis[k] for k in ('wallet','network','computed_ms','score','windows','history_method','equity_drawdown_pct','funding_included','not_a_profit_probability') if k in analysis}
                    result['leaders'].append(dict(wallet=r['wallet'],status=r['status'],last_seen=r['last_seen'],analysis=analysis))
                h=db.execute('SELECT last_success,last_attempt,error FROM intelligence_health WHERE network=?',(self.scope.network,)).fetchone()
                result.update(observed(h['last_success'] if h else None,self.clock(),failed=bool(h and h['error'])))
                result['heartbeat_ms']=h['last_attempt'] if h else None
                result['reasons']=(h['error'] or '').split(',') if h and h['error'] else []
                stamps={r[0]:r[1] for r in db.execute('SELECT kind,MAX(created) FROM intelligence_records WHERE network=? GROUP BY kind',(self.scope.network,))}
                result['components']={name:observed(stamps.get(kind),self.clock()) for name,kind in
                    [('deep_analysis','LEADER_ANALYZED'),('watchlist','LEADER_PROMOTED'),('leader_detection','LEADER_TRADE')]}
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='component_health'").fetchone():
                    for row in db.execute('SELECT component,body FROM component_health WHERE network=?',(self.scope.network,)):
                        health=body(row);heartbeat=health.get('heartbeat_ms')
                        state=observed(health.get('last_success_ms'),self.clock(),failed=bool(health.get('error')))
                        state.update(health,reasons=[health['error']] if health.get('error') else [])
                        fresh=type(heartbeat) is int and 0<=self.clock()-heartbeat<120000
                        state['worker_active']=fresh
                        if fresh and health.get('error')=='HISTORY_INCOMPLETE':state['status']='DEGRADED'
                        if row['component']=='public_data' and fresh and not health.get('error'):
                            stamp=health.get('exchange_ms');state['market_fresh']=type(stamp) is int and 0<=self.clock()-stamp<30000
                            state['status']='ACTIVE' if state['market_fresh'] else 'READY'
                        if row['component']=='discovery':
                            result.update(state)
                            if result['reasons'] or (h and h['error']):
                                result.update(status='DEGRADED',reasons=(h['error'] or '').split(',') if h else result['reasons'])
                        else:result['components'][row['component']]=state
                result['coverage']['meaning']='PUBLIC_ACTIVITY_AND_LEADER_RESEARCH_NOT_PAPER_ADMISSION'
                result['coverage']['observed_instruments']=[r[0] for r in db.execute("SELECT DISTINCT json_extract(body,'$.instrument.symbol') FROM intelligence_records WHERE network=? AND kind='LEADER_TRADE' AND json_valid(body) LIMIT 100",(self.scope.network,)) if r[0]]
        except (OSError,sqlite3.Error,ValueError,TypeError):result['status']='UNKNOWN'
        result['display_state']='ACTIVE · DEGRADED' if result.get('worker_active') and result['status']=='DEGRADED' else result['status']
        return clean(result)

    def snapshot(self):
        modes=[]
        for binding in self.bindings:
            try:
                config=json.loads(self._path(binding.config_path).read_text(encoding='utf-8'))
                if Scope.model_validate(config['authorization']['scope'])!=self.scope or config['authorization']['mode']!=binding.mode:raise ValueError('CONFIG_SCOPE_MISMATCH')
                modes.append(self._store(self._path(binding.state_path),binding.mode,config))
                modes[-1]['allowed_instrument']=config['risk']['instrument']
                session_path=self._path(binding.config_path).parent/'session.json'
                if session_path.is_file() and not session_path.is_symlink():
                    session=json.loads(session_path.read_text(encoding='utf-8'))
                    # Public, operator-recorded experiment metadata only. No paths/config/secrets.
                    if session.get('primary_mode')==binding.mode and session.get('network')==self.scope.network:
                        modes[-1]['session']={k:session.get(k) for k in ('session_id','start_ms','evaluation_end_ms','virtual_starting_capital','allowed_instrument','auto_policy_promotion')}
            except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):modes.append({'mode':binding.mode,'configured_mode':binding.mode,'runtime_mode':None,'status':'UNKNOWN','reason':'CANONICAL_STORE_UNAVAILABLE'})
        manual={'configured':None,'status':'UNKNOWN','committed':None,'reserved':None,'available':None,'positions':[],
            'pnl':None,'outcome_summary':None,'pending_operations':None}
        account=None
        try:
            account=self._store(self.root/'data/executions.sqlite3')
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):pass
        try:
            with reader(self.root/'data/executions.sqlite3') as db:
                configs=rows(db,'manual_leader_configs',self.scope.model_dump_json(),1)
                runtime=rows(db,'manual_copy_runtime',self.scope.model_dump_json(),1)
                manual.update(configured=bool(configs),configuration=body(configs[0]) if configs else None,runtime=body(runtime[0]) if runtime else None)
                if runtime:
                    state=body(runtime[0]);manual.update({k:state.get(k) for k in ('status','committed','available','allocation_limit','allocatable_capital','denominator','monitored','heartbeat_ms')})
                    manual['runtime']={k:v for k,v in state.items() if k!='account_evidence'}
                    manual['reserved']=None if state.get('reserved_unknown') else 0.
                    cached=state.get('account_evidence')
                    if cached:
                        display=PortfolioSnapshot.model_validate(cached)
                        if display.scope!=self.scope or display.evidence!='EXCHANGE':raise ValueError('ACCOUNT_SCOPE_MISMATCH')
                        current=(account or {}).get('portfolio')
                        if not current or display.received_ms>current['received_ms']:
                            account=account or {'actions':[]}
                            account['portfolio']=display.model_dump(mode='json')
                            account['display_evidence_only']=True
                        manual['account_read_error']=state.get('account_read_error')
                if configs:
                    configuration=body(configs[0]);manual['enabled']=configuration['enabled'];manual['paused']=not configuration['enabled']
                    manual.update(selected_leader=configuration['leader'],alias=configuration['alias'],allocation_pct=configuration['allocation_pct'])
                    if manual['paused']:manual['status']='PAUSED'
                    evidence=next((m for m in (manual.get('monitored') or []) if m.get('leader')==configuration['leader']),{})
                    manual['leader_capital_denominator']=evidence.get('capital')
                    manual['denominator_evidence_type']=manual.get('denominator')
                    manual['last_leader_evidence_ms']=evidence.get('exchange_ms')
                manual['positions']=[]
                if account and account.get('portfolio'):
                    from core.source_allocation import SourceAllocationBook
                    portfolio=PortfolioSnapshot.model_validate(account['portfolio'])
                    if portfolio.completeness!='COMPLETE' or portfolio.evidence=='LEGACY_UNKNOWN':
                        manual.update(committed=None,reserved=None,available=None,status='HOLD')
                        raise ValueError('CAPITAL_EVIDENCE_UNAVAILABLE')
                    owned={r['market']:json.loads(r['record']) for r in db.execute('SELECT market,record FROM ownership WHERE account=?',(self.scope.account,))}
                    owned={k:r for k,r in owned.items() if r.get('network',r.get('execution_evidence',{}).get('network'))==self.scope.network}
                    actual={p.instrument.market_key:dict(side=p.side,size=p.size,entry_price=p.entry_price,position_value=p.notional,leverage=p.leverage,margin_used=p.margin) for p in portfolio.positions}
                    pending={r['market']:json.loads(r['intent']) for r in db.execute("SELECT market,intent FROM operations WHERE account=? AND status IN ('PREPARED','UNKNOWN')",(self.scope.account,))}
                    account['source_allocations']=None
                    if all(r.get('network')==self.scope.network for r in pending.values()):
                        # Manual Leader Copy has its own allocation even if the
                        # public leader address also appears in configured COPY.
                        separate={k for k,r in owned.items() if r.get('strategy')=='MANUAL_LEADER_COPY'}
                        copy_owned={k:r for k,r in owned.items() if k not in separate}
                        copy_actual={k:r for k,r in actual.items() if k not in separate}
                        copy_pending={k:r for k,r in pending.items() if r.get('strategy')!='MANUAL_LEADER_COPY'}
                        book=SourceAllocationBook(portfolio.sizing_capital,list(self.sources),copy_actual,copy_owned,
                            {k for k,r in copy_owned.items() if r.get('managed')},copy_pending)
                        if not book.errors:account['source_allocations']={source:{'allocation_limit':a.allocation_limit,'committed':a.committed_margin,'reserved':a.reserved_margin,'available':a.available_source_budget} for source,a in book.accounts.items()}
                    for p in account['portfolio']['positions']:
                        instrument=p['instrument'];market=(instrument['dex']+':' if instrument['dex'] else '')+instrument['symbol']+'|'+instrument['dex']
                        record=owned.get(market,{})
                        proof=record.get('execution_evidence',{});known=bool(set(proof.get('order_ids',[])).intersection(p['order_ids']))
                        linked=next((a for a in account['actions'] if a['intent_id']==proof.get('intent_id')),None)
                        if not known and linked and (linked['receipt'] or {}).get('provenance')=='EXCHANGE':
                            previous=record.get('position') or {}
                            known=(bool(set(proof.get('order_ids',[])).intersection(linked['receipt'].get('order_ids',[])))
                                and record.get('side')==p['side'] and record.get('size')==p['size'] and previous.get('entry_price')==p['entry_price'])
                        origin=record.get('strategy') if known else None
                        if origin not in {'MANUAL_LEADER_COPY','AUTONOMOUS','COPY','MANUAL'}:
                            matches=[a for a in account['actions'] if set((a['receipt'] or {}).get('order_ids',[])).intersection(p['order_ids'])]
                            origin=('COPY' if matches[0]['intent']['authorization']=='COPY_POLICY' else 'MANUAL' if matches[0]['intent']['source']=='manual' else 'AUTONOMOUS') if matches else None
                        p['origin']=origin;p['leader']=record.get('source_targets') if known else None
                        p['provenance_evidence']='LAST_RECONCILED_JOURNAL_MATCH' if known else 'UNAVAILABLE'
                        p['mode']='LIVE';p['correlation_id']=proof.get('intent_id') if known else None
                        if origin=='MANUAL_LEADER_COPY':manual['positions'].append(p)
                    if configs and manual.get('allocation_limit') is not None:
                        configuration=body(configs[0]);selected=configuration['leader']
                        manual_owned={k:r for k,r in owned.items() if r.get('strategy')=='MANUAL_LEADER_COPY'}
                        manual_actual={k:v for k,v in actual.items() if k in manual_owned}
                        manual_pending={k:r for k,r in pending.items() if r.get('strategy')=='MANUAL_LEADER_COPY'}
                        manual['pending_operations']=[{'market':k,'action':r.get('action'),'state':'RECONCILIATION_REQUIRED'} for k,r in manual_pending.items()]
                        manual_book=SourceAllocationBook(portfolio.sizing_capital,[selected],manual_actual,manual_owned,
                            {k for k,r in manual_owned.items() if r.get('managed')},manual_pending,allocation_limits={selected:manual['allocation_limit']})
                        if not manual_book.errors and all(r.get('network')==self.scope.network for r in manual_pending.values()):
                            manual.update(committed=sum(a.committed_margin for a in manual_book.accounts.values()),
                                reserved=sum(a.reserved_margin for a in manual_book.accounts.values()))
                            manual['available']=max(0.,manual['allocation_limit']-manual['committed']-manual['reserved'])
                        else:manual.update(committed=None,reserved=None,available=None,status='HOLD')
                    # Runtime projection includes held old-leader commitment; do not replace it with selected-leader-only accounting.
                    if runtime and body(runtime[0]).get('reserved_unknown'):
                        manual['status']='HOLD'
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):pass
        manual['capital_status']='UNAVAILABLE'
        if account and account.get('portfolio'):
            p=account['portfolio'];stamp=p.get('exchange_ms')
            manual['account_balance']=p.get('equity')
            manual['capital_status']='FRESH' if p.get('completeness')=='COMPLETE' and type(stamp) is int and 0<=self.clock()-stamp<120000 and not manual.get('account_read_error') else 'STALE'
        if manual.get('paused') and manual['positions']:manual['status']='HOLD'
        if manual.get('configured') is False:
            manual.update(status='OFF',enabled=False,selected_leader=None,generation_id=None,allocation_pct=0.,
                          allocation_limit=0.,available=0.)
            if account and account.get('portfolio') and not account['portfolio']['positions']:
                manual.update(committed=0.,reserved=0.)
        elif manual.get('configuration'):
            manual['generation_id']=manual['configuration'].get('generation_id')
        manual['ui_state']='PENDING' if manual.get('pending_operations') else ('NO_LEADER' if manual.get('configured') is False else
            'ACTIVE' if manual.get('enabled') and manual.get('status')=='FOLLOWING' else manual['status'])
        manual['valid_actions']=(['PAUSE'] if manual.get('enabled') else ['RESUME','CHANGE_LEADER'] if manual.get('selected_leader') else ['SELECT_LEADER'])
        if manual['ui_state'] in ('PENDING','UNKNOWN','ERROR'):manual['valid_actions']=['PAUSE'] if manual.get('enabled') else []
        quarantines=[]
        try:
            from core.execution_quarantine import active_in
            from core.manual_copy_reset import history
            with reader(self.root/'data/executions.sqlite3') as db:
                quarantines=active_in(db,self.scope)
                archived=history(db,self.scope)
            manual['reset_epoch']=archived[-1]['reset_epoch'] if archived else None
            manual['archived_operations']=[{k:a[k] for k in ('intent_id','state','financial_outcome',
                'retry_allowed','capital_administration','released_margin','created_ms')} for a in archived]
        except (OSError,sqlite3.Error,ValueError):pass
        if quarantines:
            from core.manual_recovery_review import assess
            with reader(self.root/'data/executions.sqlite3') as db:
                review=assess(db,self.scope,(account or {}).get('portfolio'),manual.get('allocation_limit'),self.clock())
            manual['operator_review']=review
            manual.update(quarantined_reserve=review['quarantined_reserve'],available_for_new=review['available_for_new'])
            if review['committed'] is not None:manual.update(committed=review['committed'],reserved=review['reserved'])
            manual.update(status='PAUSED',ui_state='PAUSED',paused=True,enabled=False,valid_actions=[],
                quarantine={'count':len(quarantines),'reservation_held':sum(q['reservation']['margin'] for q in quarantines),
                    'retry_allowed':False,'operator_review_required':True,
                    'message':'One previous Manual Copy operation has an unresolved exchange outcome. No retry will be submitted automatically.',
                    'technical_details':quarantines})
        components={name:observed(None,self.clock(),critical=name in {'private_account','allocation','risk','execution','reconciliation'}) for name in
            ('public_data','private_account','discovery','deep_analysis','watchlist','leader_detection','agents','consensus','authorization','allocation','risk','execution','reconciliation','events','database','telegram','web')}
        discovery=self.discovery();components['discovery']={k:v for k,v in discovery.items() if k in {'status','last_success_ms','fresh','heartbeat_ms','reasons','worker_active'}}
        components.update(discovery.get('components',{}))
        if account and account.get('portfolio'):
            components['private_account']=observed(account['portfolio'].get('exchange_ms'),self.clock())
            components['private_account'].update(receipt_ms=account['portfolio']['received_ms'],reasons=[manual['account_read_error']] if manual.get('account_read_error') else [])
            if manual.get('account_read_error'):components['private_account']['status']='DEGRADED'
        for m in modes:
            heartbeat=m.get('worker',{}).get('heartbeat_ms');backlog=m.get('execution',{}).get('reconciliation_backlog',0)
            decision=m.get('latest_decision') or {};actions=m.get('actions',[])
            stamps={'agents':max((a.get('result',{}).get('created_ms',0) for a in m.get('agents',[]) if a.get('result')),default=None),
                'consensus':(m.get('consensus') or {}).get('created_ms'),'authorization':decision.get('authorization',{}).get('created_ms'),
                'risk':(m.get('risk') or {}).get('created_ms'),'execution':actions[0]['timestamp'] if actions else None,
                'reconciliation':m.get('execution',{}).get('last_success_ms'),'events':heartbeat,
                'allocation':(m.get('portfolio') or {}).get('received_ms') if (m.get('allocation') or {}).get('status')=='VERIFIED' else None}
            for name,stamp in stamps.items():
                signal=observed(stamp,self.clock(),failed=bool(backlog and name in {'execution','reconciliation'}),critical=name in {'allocation','risk','execution','reconciliation'})
                worker=m.get('worker',{})
                ready = (worker.get('readiness_version')=='configured-worker-v1' and worker.get('status')=='HEALTHY'
                    and heartbeat is not None and 0<=self.clock()-heartbeat<=90000 and not backlog)
                if ready and not signal['fresh'] and (name in worker.get('ready_components',[]) or
                        name=='agents' and all(a.get('readiness')=='READY' for a in m.get('agents',[])) or
                        name in {'authorization','execution'} and m.get('runtime_mode') in {'PAPER_AUTO','SHADOW'} or
                        name=='allocation' and (m.get('allocation') or {}).get('status')=='VERIFIED' and (m.get('portfolio') or {}).get('evidence')=='FAKE'):
                    signal.update(status='READY',ready_observed_ms=heartbeat,reasons=['WAITING_FOR_ACTIONABLE_EVENT'])
                if name=='risk':
                    signal=runtime_risk_health(worker,m.get('risk'),m.get('execution',{}),self.clock())
                    m['risk_health']=dict(signal,mode=m['mode'])
                if name=='consensus':signal=m.get('consensus_health',signal)
                if name=='agents' and m.get('agent_summary',{}).get('available')==7:
                    signal.update(status='ACTIVE' if m['agent_summary']['active'] else 'READY',heartbeat_ms=heartbeat,
                        available=7,reasons=['WAITING_FOR_EVENT'] if not m['agent_summary']['active'] else [])
                if components[name]['status']!='UNHEALTHY':components[name]=signal
        try:
            with reader(self.root/'data/product.sqlite3') as db:
                delivery=db.execute('SELECT body FROM product_delivery_health WHERE scope=?',(scope_key(self.scope),)).fetchone()
                if delivery:
                    d=body(delivery);components['telegram']=observed(d.get('heartbeat_ms'),self.clock(),failed=d.get('status')=='DEGRADED')
        except (OSError,ValueError,sqlite3.Error):pass
        components['web']=observed(self.clock(),self.clock());components['database']=observed(self.clock() if account or any(m.get('portfolio') for m in modes) else None,self.clock())
        status='UNHEALTHY' if any(c['status']=='UNHEALTHY' for c in components.values()) else 'DEGRADED' if any(c['status'] in {'UNKNOWN','DEGRADED'} for c in components.values()) else 'HEALTHY'
        if quarantines:
            components['risk']['manual_copy_mainnet']={'status':'BLOCKED','unresolved_execution_count':len(quarantines)}
            for name in ('execution','reconciliation'):
                if components[name]['status']!='UNHEALTHY':components[name]['status']='DEGRADED'
                components[name].update(quarantined_mainnet=len(quarantines),retry_allowed=False,
                    reasons=list(components[name].get('reasons',[]))+['MANUAL_COPY_QUARANTINED_UNKNOWN'])
            if status=='HEALTHY':status='DEGRADED'
        api_budget={'state':'UNKNOWN','reason':'API_METRICS_NOT_CONFIGURED'}
        try:
            from core.hl_budget import snapshot as budget_snapshot
            api_budget=budget_snapshot(self.root/'data/hl-api-budget.sqlite3')
        except (OSError,sqlite3.Error):pass
        from core.operational_health import storage_health
        storage=storage_health(self.root,self.clock())
        if storage['status'] in {'UNHEALTHY','DEGRADED'} and status=='HEALTHY':status='DEGRADED'
        return clean({'version':'product-v1','scope':self.scope.model_dump(mode='json'),'checked_ms':self.clock(),
            # Reported, not asserted: whether any configured runtime is
            # actually placing unconfirmed live orders right now.
            'live_auto':any(m.get('mode')=='LIVE_AUTO' for m in modes),
            'runtimes':modes,'analysis':self.shared_analysis(modes),'account':account,'manual_copy':manual,'discovery':discovery,'health':{'status':status,'components':components,'api_budget':api_budget,'storage':storage},
            'history_limit':100,'legacy_paper_substitution':False})

    def episode(self,episode_id):
        for mode in self.snapshot()['runtimes']:
            for episode in mode.get('episodes',[]):
                if episode['episode_id']==episode_id:return episode
        raise KeyError('EPISODE_NOT_FOUND')
