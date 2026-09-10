"""Selected-leader target reconciliation in the existing account-locked watcher.

Denominator: clearinghouseState.marginSummary.accountValue for the POSITION'S
DEX collateral pool. Never withdrawable/free collateral/notional. If unavailable,
entries HOLD; a fresh empty leader position still permits a proven follower close.
Other configured copy/AI thirds are removed from the manual allocatable base.
STOP/switch retain old provenance and lifecycle reads, without implicit closes.
"""
import hashlib
import json
import math
import os
import time
from contextlib import closing
from core.ai_review import account_guard, market_key
from core.capital_snapshot import finite_amount
from core.hyperliquid import HyperliquidReader
from core.manual_leader_copy import ManualLeaderCopyService, recover_pending_manual_leader


class ManualCopyWorker:
    MAX_AGE_MS=30000
    MAX_LEADERS=16
    def __init__(self,engine,reader,clock=None):
        self.engine,self.reader=engine,reader
        self.service=ManualLeaderCopyService(engine)
        self.clock=clock or (lambda:int(time.time()*1000))
        self.root=os.path.dirname(os.path.dirname(engine.journal.path))
        with closing(engine.journal.connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS manual_copy_runtime(scope TEXT PRIMARY KEY,body TEXT NOT NULL)')
            db.commit()

    def diagnostics(self,scope):
        with closing(self.engine.journal.connect()) as db:
            row=db.execute('SELECT body FROM manual_copy_runtime WHERE scope=?',(scope.model_dump_json(),)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self,scope,body):
        with closing(self.engine.journal.connect()) as db:
            db.execute('INSERT INTO manual_copy_runtime VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET body=excluded.body',
                (scope.model_dump_json(),json.dumps(body,allow_nan=False)))
            db.commit()

    def leader_snapshot(self,address):
        now=self.clock(); rows={}; capitals={}; stamps=[]
        for dex in ('','xyz'):
            state=self.reader.state(address,dex)
            now=self.clock()
            stamp=state.get('time')
            if type(stamp) is not int or not 0<=now-stamp<=self.MAX_AGE_MS: raise ValueError('LEADER_STALE')
            stamps.append(stamp)
            for p in HyperliquidReader._positions(state,'STOCKS' if dex else 'CRYPTO',dex):
                rows[market_key(p)]=p
            try:
                value=finite_amount(state['marginSummary']['accountValue'],'leader account value')
                capitals[dex]=value if value>0 else None
            except (KeyError,ValueError,TypeError): capitals[dex]=None
        return dict(positions=rows,capital=capitals,exchange_ms=min(stamps),received_ms=now,
            denominator='PER_DEX_MARGIN_SUMMARY_ACCOUNT_VALUE')

    def start_baseline(self, account, client, leader):
        from core.foundation.data import copy_account_snapshot
        scope=self.service._scope(account,client)
        if self.reader.network!=scope.network: raise ValueError('LEADER_NETWORK_MISMATCH')
        leader_state=self.leader_snapshot(leader)
        portfolio=copy_account_snapshot(client,scope,1,self.clock,'')
        return dict(wallet=leader,leader=leader_state,account=portfolio.model_dump(mode='json'))

    def prove(self,account,client,key,record,current):
        if (record.get('network')!=client.network or not record.get('managed') or current is None
                or record.get('side')!=current['side']): return False
        try:
            saved=record['position']
            if not (math.isclose(float(record['size']),float(current['size']),rel_tol=1e-8)
                and math.isclose(float(saved['entry_price']),float(current['entry_price']),rel_tol=1e-8)): return False
            since=int(record['verified_at_ms'])
            fills=client.info.user_fills_by_time(account['address'],since,self.clock())
            if not isinstance(fills,list) or len(fills)>=2000: return False
            known={str(x) for x in record.get('execution_evidence',{}).get('trade_ids',[])}
            return not any(market_key(f)==key and str(f.get('tid')) not in known for f in fills)
        except (KeyError,TypeError,ValueError): return False

    def cycle(self,uid,profile,client):
        """Called by production watcher; same interprocess lock as COPY/manual."""
        account=dict(profile.get('account') or {},_tenant=str(uid))
        if not account.get('address'): return None
        with account_guard(self.root,account['address']):
            # Re-read under lock: STOP/config switch and account changes serialize.
            _,fresh=self.engine.storage.profile(uid)
            if (fresh.get('account') or {}).get('address')!=account['address']: raise ValueError('ACCOUNT_CHANGED')
            return self._cycle(account,fresh,client)

    def _cycle(self,account,profile,client):
        scope=self.service._scope(account,client)
        if self.reader.network!=scope.network: raise ValueError('READER_NETWORK_MISMATCH')
        config=self.service.config(account,client)
        if config is None:
            previous=self.diagnostics(scope) or {}
            # OFF after a clean reset has no leader/lifecycle to monitor. This
            # is display-only evidence, never evidence authorizing a new order.
            # Explicit preview/START still obtains fresh private account data.
            if previous.get('reset_epoch') and self.clock()-previous.get('account_attempt_ms',0)>=900000:
                from core.foundation.data import copy_account_snapshot
                previous['account_attempt_ms']=self.clock()
                try:
                    p=copy_account_snapshot(client,scope,self.clock(),self.clock,'')
                    previous.update(account_evidence=p.model_dump(mode='json'),account_read_error=None,
                                    allocatable_capital=p.sizing_capital,heartbeat_ms=self.clock())
                except Exception:previous['account_read_error']='ACCOUNT_DATA_UNAVAILABLE'
                self.save(scope,previous)
            return None  # No leader subscriptions, recovery or execution.
        from core.execution_quarantine import active_in
        with closing(self.engine.journal.connect()) as db:
            quarantines=active_in(db,scope)
        if quarantines:config=config.model_copy(update={'enabled':False})
        report=dict(enabled=config.enabled,leader=config.leader,generation_id=config.generation_id,status='HOLD',heartbeat_ms=self.clock(),
            denominator='PER_DEX_MARGIN_SUMMARY_ACCOUNT_VALUE',monitored=[],results=[],errors=[])
        if not config.enabled and not quarantines:
            # A paused draft or generation is not permission to follow or
            # replay a leader.  In particular, do not enter recovery/leader
            # monitoring just because an old generation id remains attached
            # after STOP: that expensive path can contend with the shared
            # Hyperliquid budget and freeze the account read model.  Refresh
            # only bounded read-only account evidence; existing positions stay
            # untouched and no execution path is reachable from this branch.
            previous=self.diagnostics(scope) or {}
            report['status']='PAUSED' if config.generation_id else 'READY'
            report['account_attempt_ms']=previous.get('account_attempt_ms',0)
            cached=previous.get('account_evidence')
            try:
                fresh_cached=(cached and type(cached.get('received_ms')) is int
                              and 0<=self.clock()-cached['received_ms']<60000)
                if not fresh_cached:
                    if 0<=self.clock()-report['account_attempt_ms']<60000:
                        raise ValueError('ACCOUNT_RETRY_BACKOFF')
                    report['account_attempt_ms']=self.clock()
                    from core.foundation.data import copy_account_snapshot
                    cached=copy_account_snapshot(client,scope,self.clock(),self.clock,'').model_dump(mode='json')
                report['account_evidence']=cached
                report['account_read_error']=None
                capital=finite_amount(cached['sizing_capital'],'follower sizing capital')
                if capital<0 or cached['completeness']!='COMPLETE':
                    raise ValueError('ACCOUNT_DATA_UNAVAILABLE')
                slots=(len(profile.get('leaders',[])[:3]) if profile.get('copy_enabled') else 0)+int(bool(profile.get('ai_slot_selected')))
                allocatable=capital*max(0.,1-min(3,slots)/3)
                report['allocatable_capital']=allocatable
                report['allocation_limit']=config.capital_limit(allocatable)
            except Exception:
                report['account_evidence']=cached
                report['account_read_error']='ACCOUNT_DATA_UNAVAILABLE'
            self.save(scope,report)
            return report
        try:
            report['recovery']=list(recover_pending_manual_leader(self.engine,account,client))
            owned=self.engine.journal.owned(scope.account)
            manual={k:r for k,r in owned.items() if r.get('managed') and r.get('strategy')=='MANUAL_LEADER_COPY'}
            # A paused, flat generation has no admission subscription. Retain
            # owned HOLD leaders and query-only execution recovery, not polling
            # an unowned selected wallet at trading priority every few seconds.
            leaders={config.leader} if config.enabled else set()
            for r in manual.values():
                leaders.update(x['wallet'] for x in r.get('source_targets',[]))
            # Keep admission bounded and rotate old HOLD leaders, so adding
            # retained sources cannot permanently starve later lifecycle reads.
            old_leaders=sorted(leaders-{config.leader})
            previous=self.diagnostics(scope) or {}
            cursor=int(previous.get('lifecycle_cursor',0))%max(1,len(old_leaders))
            rotated=old_leaders[cursor:]+old_leaders[:cursor]
            selected=[config.leader] if config.leader in leaders else []
            leaders=selected+rotated[:self.MAX_LEADERS-len(selected)]
            report['lifecycle_cursor']=(cursor+self.MAX_LEADERS-1)%max(1,len(old_leaders))
            evidence={}
            for leader in leaders:
                try:
                    evidence[leader]=self.leader_snapshot(leader)
                    report['monitored'].append(dict(leader=leader,mode='ADMISSION' if config.enabled and leader==config.leader else 'POSITION_HOLD',
                        exchange_ms=evidence[leader]['exchange_ms'],capital=evidence[leader]['capital']))
                except Exception: report['errors'].append('LEADER_EVIDENCE_UNAVAILABLE:'+leader)
            if not config.enabled:
                # Display-only account evidence while paused. Never write the
                # execution portfolio or infer provenance from a public read.
                # One bounded refresh per minute, owned by this existing loop.
                previous=self.diagnostics(scope) or {}
                cached=previous.get('account_evidence')
                report['status']='PAUSED'
                report['account_attempt_ms']=previous.get('account_attempt_ms',0)
                try:
                    if not cached or not 0<=self.clock()-cached['received_ms']<60000:
                        if 0<=self.clock()-report['account_attempt_ms']<60000:
                            raise ValueError('ACCOUNT_RETRY_BACKOFF')
                        report['account_attempt_ms']=self.clock()
                        from core.foundation.data import copy_account_snapshot
                        cached=copy_account_snapshot(client,scope,self.clock(),self.clock,'').model_dump(mode='json')
                    report['account_evidence']=cached
                    report['account_read_error']=None
                    capital=finite_amount(cached['sizing_capital'],'follower sizing capital')
                    if capital<0 or cached['completeness']!='COMPLETE':raise ValueError('ACCOUNT_DATA_UNAVAILABLE')
                    slots=(len(profile.get('leaders',[])[:3]) if profile.get('copy_enabled') else 0)+int(bool(profile.get('ai_slot_selected')))
                    report['allocatable_capital']=capital*max(0.,1-min(3,slots)/3)
                    report['allocation_limit']=config.capital_limit(report['allocatable_capital'])
                except Exception:
                    report['account_evidence']=cached
                    report['account_read_error']='ACCOUNT_DATA_UNAVAILABLE'
                return report
            if config.leader not in evidence:return report
            follower=client.positions(True,True)
            actual={market_key(p):p for p in follower}
            if len(actual)!=len(follower):raise ValueError('DUPLICATE_POSITIONS')
            capital=finite_amount(client.capital_snapshot().sizing_base_usdc,'follower sizing capital')
            if capital<0:raise ValueError('NEGATIVE_CAPITAL')
            slots=(len(profile.get('leaders',[])[:3]) if profile.get('copy_enabled') else 0)+int(bool(profile.get('ai_slot_selected')))
            allocatable=capital*max(0.,1-min(3,slots)/3)
            limit=config.capital_limit(allocatable)
            committed=held_other=0.
            for key,r in manual.items():
                p=actual.get(key)
                if not self.prove(account,client,key,r,p):raise ValueError('MANUAL_OWNERSHIP_RECONCILIATION_REQUIRED')
                margin=finite_amount(p['margin_used'],'held margin')
                if margin<0:raise ValueError('NEGATIVE_MARGIN')
                committed+=margin
                if any(x['wallet']!=config.leader for x in r.get('source_targets',[])):held_other+=margin
            report.update(allocatable_capital=allocatable,allocation_limit=limit,committed=committed,
                available=max(0.,limit-committed),reserved_unknown=bool(self.engine.journal.pending(scope.account)))
            # Unknown envelopes retain capacity; recover is query-only above.
            if report['reserved_unknown']:raise ValueError('PENDING_EXECUTION_HOLD')
            leader=evidence[config.leader]
            keys=set(leader['positions'])|{k for k,r in manual.items() if any(x['wallet']==config.leader for x in r.get('source_targets',[]))}
            for key in sorted(keys):
                p=actual.get(key); target=leader['positions'].get(key); record=manual.get(key)
                if p and (record is None or any(x['wallet']!=config.leader for x in record.get('source_targets',[]))):
                    report['results'].append({'market':key,'status':'UNRELATED_POSITION_HOLD'});continue
                dex=key.split('|')[1]; coin=key.split('|')[0]
                denominator=leader['capital'][dex]
                current=finite_amount(p['margin_used'],'current margin') if p else 0.
                if target is None:
                    if p is None:continue
                    action='CLOSE';margin=0.;lev=int(p['leverage']);side=p['side']
                else:
                    if denominator is None:
                        report['results'].append({'market':key,'status':'DENOMINATOR_UNKNOWN_HOLD'});continue
                    margin=finite_amount(target['margin_used'],'leader margin');lev=int(target['leverage']);side=target['side']
                    proposed=limit*margin/denominator
                    # Held capital from old leaders is still charged to the one allocation.
                    proposed=min(proposed,max(0.,limit-(committed-current)))
                    if p and p['side']!=side:action='REVERSE'
                    elif p is None:action='OPEN'
                    else:action='ADD' if proposed>current else 'REDUCE'
                    if p and p['side']==side and abs(proposed-current)<=1e-8:continue
                    if p and p['side']==side and int(p['leverage'])!=lev:
                        report['results'].append({'market':key,'status':'LEVERAGE_CHANGE_HOLD'});continue
                identity=hashlib.sha256(json.dumps([scope.model_dump(mode='json'),config.updated_ms,config.leader,key,
                    action,target,limit,current,p],sort_keys=True,allow_nan=False).encode()).hexdigest()
                if not 0<=self.clock()-leader['exchange_ms']<=self.MAX_AGE_MS:
                    raise ValueError('LEADER_STALE')
                result=self.service.process(account,client,event_id=identity,action=action,leader_margin=margin,
                    leader_capital=denominator,allocatable_capital=allocatable,current_margin=current,before_position=p,
                    spec={'coin':coin,'dex':dex,'leverage':lev,'max_margin':max(0.,limit-(committed-current)),
                          '_manual_effective_limit':max(0.,limit-held_other)},side=side,event_ms=leader['exchange_ms'])
                report['results'].append({'market':key,'action':action,'status':result.status if hasattr(result,'status') else result['status']})
                # One mutation per fresh follower snapshot. Next cycle rebuilds capacity.
                if hasattr(result,'status'):break
            report['status']='FOLLOWING'
        except Exception as exc:
            report['status']='HOLD';report['errors'].append(type(exc).__name__ if str(exc) not in {
                'PENDING_EXECUTION_HOLD','MANUAL_OWNERSHIP_RECONCILIATION_REQUIRED','LIFECYCLE_WATCH_LIMIT'} else str(exc))
        finally:self.save(scope,report)
        return report
