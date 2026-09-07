"""Deterministic public discovery/research tests; no real websocket or orders."""
import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_trade_analyzer import fill
from core.intelligence.models import IntelligencePolicy, LeaderTradeEvent
from core.intelligence.analysis import reports, score, DAY
from core.intelligence.agents import evaluate, consensus
from core.intelligence.service import WalletDiscoveryEngine, RequestBudget, replay_decision

ADDRESS='0x'+'a'*40
NOW=1800000000000


def history():
    rows=[fill(i+1,pnl='10' if i%4 else '-2',time=NOW-(45-i//4)*DAY+i,coin='BTC' if i%2 else 'ETH') for i in range(176)]
    rows += [fill(1000+i,pnl='5',time=NOW-10000+i) for i in range(6)]
    return rows


class PublicFixture:
    network='TESTNET'
    def __init__(self): self.fills=history(); self.calls=[]
    def _info(self,p):
        self.calls.append(p['type'])
        if p['type']=='userFillsByTime': return [f for f in self.fills if p['startTime']<=f['time']<=p['endTime']]
        if p['type']=='l2Book': return {'time':NOW+1000,'levels':[[{'px':'100','sz':'1000'}],[{'px':'100.01','sz':'1000'}]]}
        if p['type']=='candleSnapshot': return [{'T':NOW-(63-i)*900000,'c':100+i,'h':101+i,'l':99+i} for i in range(64)]
        raise AssertionError('Unexpected public request')


class IntelligenceTests(unittest.TestCase):
    def actionable(self):
        from core.intelligence.models import RiskContextEvidence,LeaderScore
        from core.foundation.contracts import Scope,PortfolioSnapshot,MarketSnapshot,Allocation
        analysis=self.activate(); event=self.signal()
        scope=Scope(tenant='7',account=ADDRESS,network='TESTNET')
        portfolio=PortfolioSnapshot(scope=scope,revision=1,exchange_ms=NOW+1000,received_ms=NOW+1000,
            equity=3000.,sizing_capital=3000.,available_collateral=3000.,completeness='COMPLETE',evidence='FAKE')
        market=MarketSnapshot(instrument=event.instrument,exchange_ms=NOW+1000,received_ms=NOW+1000,price=100.,bid=100.,ask=100.01,
            completeness='COMPLETE',freshness='FRESH',source='FAKE',source_version='test')
        allocation=Allocation(scope=scope,source='intelligence',limit=1000.,committed=0.,reserved=0.,available=1000.,revision=1,received_ms=NOW+1000)
        context=RiskContextEvidence(portfolio=portfolio,market=market,allocation=allocation,unresolved=False,
            required_margin=10.,required_capacity=11.,slippage_pct=.1,max_slippage_pct=.5)
        def run(e=context):
            return evaluate(event,LeaderScore.model_validate(analysis['score']),self.reader._info({'type':'candleSnapshot'}),
                self.reader._info({'type':'l2Book'}),NOW+1000,self.worker.policy,context=e,actionable=True)
        return event,context,run
    def test_actionable_agents_use_full_context(self):
        event,context,run=self.actionable(); agents=run()
        self.assertEqual(len(agents),7)
        self.assertEqual(next(a for a in agents if a.agent_id=='risk_context').direction,'PASS')
        self.assertEqual(next(a for a in agents if a.agent_id=='order_flow').direction,'WAIT')
        self.assertEqual(consensus(event,agents,NOW+1000,self.worker.policy).decision,'COPY_LONG')
        for a in agents: self.assertEqual(a.model_validate_json(a.model_dump_json()),a)
    def test_authorization_modes_require_durable_authority(self):
        from core.foundation.authorization import AuthorizationPolicy,AuthorizationService
        from core.foundation.contracts import Scope
        event,e,run=self.actionable(); decision=consensus(event,run(),NOW+1000,self.worker.policy)
        service=AuthorizationService(self.worker.store)
        for i,mode in enumerate(('OBSERVE','PAPER_AUTO','SHADOW','LIVE_CONFIRM')):
            scope=Scope(tenant=str(i),account='0x'+str(i+1)*40,network='TESTNET')
            auth=service.decide(AuthorizationPolicy(scope=scope,mode=mode),event,decision,NOW+1000)
            self.assertEqual(auth.execution_mode,'PAPER' if mode=='PAPER_AUTO' else 'NONE')
            self.assertEqual(service.verify(auth,NOW+1001),mode in ('PAPER_AUTO','SHADOW'))
            if mode=='LIVE_CONFIRM':
                with self.assertRaises(ValueError): service.confirm(auth.decision_id,scope,NOW+1001,authenticated_user='another')
                confirmed=service.confirm(auth.decision_id,scope,NOW+1001,authenticated_user=scope.tenant)
                self.assertEqual(confirmed.execution_mode,'LIVE')
                self.assertTrue(service.verify(confirmed,NOW+1001))
                self.assertFalse(service.verify(confirmed,NOW+100000))
    def test_authorization_policy_toggle_cannot_replay_event(self):
        from core.foundation.authorization import AuthorizationPolicy,AuthorizationService
        event,e,run=self.actionable(); result=consensus(event,run(),NOW+1000,self.worker.policy)
        service=AuthorizationService(self.worker.store)
        policy=AuthorizationPolicy(scope=e.portfolio.scope)
        first=service.decide(policy,event,result,NOW+1000)
        second=service.decide(policy.model_copy(update={'mode':'PAPER_AUTO'}),event,result,NOW+1001)
        self.assertEqual(first,second)
        self.assertFalse(service.verify(first.model_copy(update={'outcome':'AUTHORIZED','execution_mode':'PAPER'}),NOW+1001))
        with self.assertRaises(ValueError): AuthorizationPolicy(scope=policy.scope,mode='LIVE_AUTO')
    def autonomous_ledger(self,positions=(),reservations=(),**changes):
        from core.foundation.autonomous_allocation import AutonomousAllocationPolicy,AutonomousLedger
        from core.foundation.contracts import Scope,PortfolioSnapshot
        portfolio=PortfolioSnapshot(scope=Scope(tenant='7',account=ADDRESS,network='TESTNET'),revision=1,
            exchange_ms=NOW+1000,received_ms=NOW+1000,equity=3000.,sizing_capital=3000.,available_collateral=3000.,
            completeness='COMPLETE',evidence='FAKE')
        policy=AutonomousAllocationPolicy(scope=portfolio.scope,allocation_limit=1000.,other_allocation_limits=(1000.,1000.),
            entry_fraction=.1,max_position_margin=100.,max_leverage=5)
        p=portfolio.model_copy(update={'positions':positions,**changes})
        return AutonomousLedger(p,policy,reservations)
    def test_autonomous_ledger_no_borrowing_and_deterministic_size(self):
        ledger=self.autonomous_ledger()
        self.assertEqual(ledger.allocation('intelligence').available,1000)
        self.assertEqual(ledger.size(100.,5,.01,.8,.5),2.)
        self.assertEqual(ledger.size(100.,5,.01,0.,.5),0.)
        for value in (float('nan'),float('inf'),-1.,1.1):
            with self.assertRaises(ValueError): ledger.size(100.,5,.01,value,.5)
    def test_autonomous_ledger_shared_held_capital_and_reservations(self):
        from core.foundation.contracts import Position,Contribution,InstrumentId
        from core.foundation.ledger import Reservation
        p=Position(instrument=InstrumentId(network='TESTNET',symbol='BTC'),side='LONG',size=20.,entry_price=100.,
            notional=2000.,margin=1000.,leverage=2,held=True,evidence='VERIFIED',order_ids=('1',),
            contributions=(Contribution(source='intelligence',notional=1400.),Contribution(source='copy-a',notional=600.)))
        ledger=self.autonomous_ledger((p,),(Reservation('unknown-1','intelligence',100.,101.),))
        a=ledger.allocation('intelligence')
        self.assertEqual((a.committed,a.reserved,a.available),(700.,100.,200.))
        self.assertEqual(ledger.available_capacity,2899.)
    def test_autonomous_ledger_fails_closed_on_external_and_invalid_reservation(self):
        from core.foundation.ledger import Reservation
        for row in (Reservation('bad','intelligence',float('nan'),1.),Reservation('bad','intelligence',-1.,1.)):
            ledger=self.autonomous_ledger(reservations=(row,))
            self.assertIsNone(ledger.available_capacity)
            with self.assertRaises(ValueError): ledger.allocation('intelligence')
        ledger=self.autonomous_ledger(sizing_capital=2000.)
        self.assertIn('ALLOCATION_OVERCOMMITTED',ledger.errors)
    def test_missing_context_blocks_actionable_consensus(self):
        event,_,run=self.actionable(); agents=run(None)
        self.assertEqual(next(a for a in agents if a.agent_id=='risk_context').direction,'BLOCK')
        self.assertEqual(consensus(event,agents,NOW+1000,self.worker.policy).decision,'WAIT')
    def test_context_rejects_unresolved_capacity_and_slippage(self):
        event,e,run=self.actionable()
        for update in ({'unresolved':None},{'unresolved':True},{'required_margin':1001.},{'required_capacity':3001.},{'slippage_pct':1.}):
            with self.subTest(update=update):
                agents=run(e.model_copy(update=update))
                self.assertEqual(consensus(event,agents,NOW+1000,self.worker.policy).decision,'WAIT')
    def test_context_rejects_nonfinite_and_mismatched_allocation(self):
        event,e,run=self.actionable()
        for update in ({'required_margin':float('nan')},{'required_capacity':float('inf')},
                       {'allocation':e.allocation.model_copy(update={'available':1100.})},
                       {'market':e.market.model_copy(update={'exchange_ms':1})},
                       {'portfolio':e.portfolio.model_copy(update={'received_ms':1})}):
            agents=run(e.model_copy(update=update))
            self.assertEqual(consensus(event,agents,NOW+1000,self.worker.policy).decision,'WAIT')
    def test_deep_metrics_are_explicit_and_deduplicated(self):
        rows=history()
        first=reports(rows,NOW)['30']
        second=reports(rows+[rows[-1]],NOW)['30']
        self.assertEqual(first['deep_analysis'],second['deep_analysis'])
        deep=first['deep_analysis']
        self.assertEqual(deep['drawdown_proxy']['value_usdc'],first['max_drawdown'])
        self.assertIsNone(deep['drawdown_proxy']['account_equity_drawdown_pct'])
        self.assertIsNone(deep['leverage_behavior']['value'])
        self.assertAlmostEqual(sum(deep['symbol_notional_share'].values()),1)
        self.assertAlmostEqual(sum(deep['long_short_bias'][k] for k in ('long','short','unknown')),1)
        self.assertGreater(deep['payoff_ratio'],0)
    def test_reversal_bias_splits_close_and_open_notional(self):
        row=fill(9000,direction='Long > Short',side='A',start='1',size='3',pnl='1',time=NOW)
        bias=reports([row],NOW)['30']['deep_analysis']['long_short_bias']
        self.assertAlmostEqual(bias['long'],1/3)
        self.assertAlmostEqual(bias['short'],2/3)
    def test_missing_windows_not_zero_or_fabricated_leverage(self):
        self.assertEqual(reports([],NOW),{'30':None,'90':None,'180':None})
        row=fill(time=NOW)
        row['leverage']=100
        deep=reports([row],NOW)['30']['deep_analysis']
        self.assertIsNone(deep['leverage_behavior']['value'])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'intelligence.sqlite3'
        self.worker=WalletDiscoveryEngine(self.path,'TESTNET')
        self.reader=PublicFixture()
    def discover(self):
        self.worker.observe([{'time':NOW,'users':[ADDRESS,'0x'+'b'*40]}],NOW)
    def activate(self):
        self.discover()
        analysis=self.worker.analyze_one(ADDRESS,self.reader._info,NOW)
        self.assertTrue(analysis['score']['qualified'])
        self.worker.promote(NOW)
        return analysis
    def signal(self):
        self.reader.fills.append(fill(3000,direction='Open Long',side='B',start='0',pnl='0',time=NOW+1000))
        return self.worker.detect(ADDRESS,self.reader._info,NOW+1000)[0]
    def test_discovery_registry_bounded_and_network_scoped(self):
        w=WalletDiscoveryEngine(self.path,'TESTNET',IntelligencePolicy(registry_limit=1))
        w.observe([{'time':NOW,'users':[ADDRESS,'0x'+'b'*40]}],NOW)
        self.assertEqual(sum(w.snapshot(NOW)['counts'].values()),1)
        self.assertEqual(WalletDiscoveryEngine(self.path,'MAINNET').snapshot(NOW)['counts'],{})
    def test_restart_preserves_registry(self):
        self.discover()
        self.assertEqual(WalletDiscoveryEngine(self.path,'TESTNET').snapshot(NOW)['counts'],{'DISCOVERED':2})
    def test_tiny_sample_does_not_qualify(self):
        w=reports([fill(pnl='100',time=NOW)],NOW)
        ranked=score(ADDRESS,'TESTNET',w,NOW,self.worker.policy)
        self.assertFalse(ranked.qualified); self.assertIn('SMALL_SAMPLE',ranked.reasons)
    def test_high_win_rate_negative_expectancy_rejected(self):
        rows=[fill(i,pnl='1' if i%10 else '-100',time=NOW-i*DAY//4) for i in range(100)]
        r=score(ADDRESS,'TESTNET',reports(rows,NOW),NOW,self.worker.policy)
        self.assertIn('NEGATIVE_EXPECTANCY',r.reasons); self.assertFalse(r.qualified)
    def test_single_lucky_fill_rejected(self):
        rows=history()+[fill(4000,pnl='1000000',time=NOW)]
        r=score(ADDRESS,'TESTNET',reports(rows,NOW),NOW,self.worker.policy)
        self.assertIn('DOMINANT_WIN',r.reasons); self.assertFalse(r.qualified)
    def test_cheap_filter_avoids_deep_requests(self):
        self.discover(); self.reader.fills=[]
        self.assertIsNone(self.worker.analyze_one(ADDRESS,self.reader._info,NOW))
        self.assertEqual(len(self.reader.calls),1)
    def test_promote_watchlist_without_trading_admission(self):
        self.activate()
        view=self.worker.snapshot(NOW)
        self.assertEqual(view['counts']['ACTIVE'],1)
        self.assertFalse(view['execution_enabled'])
    def test_trade_event_is_once_across_restart(self):
        self.activate(); first=self.signal()
        self.worker=WalletDiscoveryEngine(self.path,'TESTNET')
        self.assertEqual(self.worker.detect(ADDRESS,self.reader._info,NOW+2000),[])
        self.assertEqual(first.action,'OPEN')
    def test_detection_crash_before_commit_does_not_advance(self):
        self.activate()
        self.reader.fills.append(fill(3000,direction='Open Long',side='B',start='0',time=NOW+1000))
        with patch.object(self.worker,'_record',side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError): self.worker.detect(ADDRESS,self.reader._info,NOW+1000)
        self.assertEqual(len(self.worker.detect(ADDRESS,self.reader._info,NOW+1000)),1)
    def test_real_pipeline_replays_identical_decision(self):
        analysis=self.activate(); event=self.signal()
        from core.intelligence.models import LeaderScore
        decision=self.worker.research(event,LeaderScore.model_validate(analysis['score']),self.reader._info,NOW+1000)
        self.assertEqual(decision.decision,'COPY_LONG')
        record=next(r for r in self.worker.records(limit=100) if r['kind']=='DECISION')['body']
        self.assertEqual(replay_decision(record),record['consensus'])
        self.assertFalse(record['executed'])
    def test_research_is_idempotent_and_does_not_refetch(self):
        analysis=self.activate(); event=self.signal()
        from core.intelligence.models import LeaderScore
        leader=LeaderScore.model_validate(analysis['score'])
        self.worker.research(event,leader,self.reader._info,NOW+1000)
        calls=len(self.reader.calls)
        self.worker.research(event,leader,self.reader._info,NOW+2000)
        self.assertEqual(calls,len(self.reader.calls))
    def test_stale_and_unknown_book_block_consensus(self):
        analysis=self.activate(); event=self.signal()
        from core.intelligence.models import LeaderScore
        leader=LeaderScore.model_validate(analysis['score'])
        for book in ({},{'time':1,'levels':[]},{'time':NOW,'levels':[[{'px':'NaN','sz':'1'}],[]]}):
            outputs=evaluate(event,leader,[],book,NOW+1000,self.worker.policy)
            result=consensus(event,outputs,NOW+1000,self.worker.policy)
            self.assertEqual(result.decision,'WAIT')
    def test_network_mismatch_no_requests(self):
        self.reader.network='MAINNET'
        with self.assertRaises(ValueError): self.worker.cycle(self.reader,[],NOW)
        self.assertFalse(self.reader.calls)
    def test_delayed_fill_inside_overlap_detected_once(self):
        self.activate()
        self.worker.detect(ADDRESS,self.reader._info,NOW+2000)
        self.reader.fills.append(fill(5000,direction='Open Long',side='B',start='0',time=NOW+1000))
        self.assertEqual(len(self.worker.detect(ADDRESS,self.reader._info,NOW+3000)),1)
        self.assertEqual(self.worker.detect(ADDRESS,self.reader._info,NOW+4000),[])
    def test_reanalysis_does_not_reset_active_cursor(self):
        self.activate(); self.signal()
        self.worker.analyze_one(ADDRESS,self.reader._info,NOW+2000)
        self.worker.promote(NOW+2000)
        with self.worker.store.transaction() as db:
            row=db.execute('SELECT status,cursor FROM candidates WHERE wallet=?',(ADDRESS,)).fetchone()
        self.assertEqual(row['status'],'ACTIVE'); self.assertEqual(row['cursor'],NOW+1000)
    def test_repromotion_does_not_action_inactive_history(self):
        self.activate()
        with self.worker.store.transaction() as db:
            db.execute("UPDATE candidates SET status='QUALIFIED' WHERE wallet=?",(ADDRESS,))
        self.reader.fills.append(fill(5000,direction='Open Long',side='B',start='0',time=NOW+1000))
        self.worker.promote(NOW+2000)
        self.assertEqual(self.worker.detect(ADDRESS,self.reader._info,NOW+3000),[])
    def test_restart_recovers_persisted_research_queue(self):
        self.activate(); self.signal()
        worker=WalletDiscoveryEngine(self.path,'TESTNET')
        worker.cycle(self.reader,[],NOW+2000)
        decisions=[r for r in worker.records(limit=100) if r['kind']=='DECISION']
        self.assertEqual(len(decisions),1)
        worker.cycle(self.reader,[],NOW+3000)
        self.assertEqual(len([r for r in worker.records(limit=100) if r['kind']=='DECISION']),1)
    def test_socket_failure_does_not_lose_research_and_is_visible(self):
        self.activate(); self.signal()
        from core.intelligence.worker import tick
        stream=__import__('unittest.mock',fromlist=['Mock']).Mock()
        stream.poll.side_effect=RuntimeError('synthetic socket outage')
        self.assertFalse(tick(self.worker,self.reader,stream,NOW+2000))
        self.assertEqual(self.worker.snapshot(NOW+2000)['last_error'],'PUBLIC_STREAM_UNAVAILABLE')
        self.assertTrue(any(r['kind']=='DECISION' for r in self.worker.records(limit=100)))
    def test_duplicate_cycle_lease_no_requests(self):
        with self.worker.store.transaction() as db:
            db.execute('INSERT INTO intelligence_lease VALUES(?,?,?)',('TESTNET','other',NOW+10000))
        self.assertFalse(self.worker.cycle(self.reader,[],NOW))
        self.assertEqual(self.reader.calls,[])
    def test_clock_regression_is_not_healthy(self):
        self.worker.cycle(self.reader,[],NOW)
        self.assertEqual(self.worker.snapshot(NOW-1)['health'],'DEGRADED')
    def test_invalid_query_bounds(self):
        for after,limit in ((-1,1),(0,101),(False,1),(0,0)):
            with self.assertRaises(ValueError): self.worker.records(after,limit)
    def test_existing_financial_database_is_not_modified(self):
        import sqlite3
        path=Path(self.path).with_name('financial.sqlite')
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE journal(value TEXT)')
            db.execute("INSERT INTO journal VALUES('preserve')")
        db.close()
        before=path.read_bytes()
        with self.assertRaisesRegex(ValueError,'DEDICATED_RESEARCH'): WalletDiscoveryEngine(path,'TESTNET')
        self.assertEqual(path.read_bytes(),before)
    def test_research_freshness_uses_receipt_time_after_reads(self):
        from core.intelligence.models import LeaderScore
        analysis=self.activate(); event=self.signal()
        result=self.worker.research(event,LeaderScore.model_validate(analysis['score']),self.reader._info,NOW+1000,clock=lambda:NOW+120000)
        self.assertEqual(result.decision,'WAIT')
        self.assertIn('STALE_SIGNAL',result.blockers)
    def test_consensus_rejects_duplicate_or_wrong_network_agent(self):
        from core.intelligence.models import LeaderScore
        analysis=self.activate(); event=self.signal()
        outputs=evaluate(event,LeaderScore.model_validate(analysis['score']),self.reader._info({'type':'candleSnapshot'}),self.reader._info({'type':'l2Book'}),NOW+1000,self.worker.policy)
        self.assertIn('DUPLICATE_AGENT',consensus(event,outputs+(outputs[0],),NOW+1000,self.worker.policy).blockers)
        wrong=outputs[0].model_copy(update={'instrument':event.instrument.model_copy(update={'network':'MAINNET'})})
        self.assertIn('AGENT_SCOPE_OR_AGE',consensus(event,(wrong,)+outputs[1:],NOW+1000,self.worker.policy).blockers)
    def test_reduction_is_not_misinterpreted_as_new_short(self):
        from core.intelligence.models import LeaderScore
        analysis=self.activate(); event=self.signal().model_copy(update={'action':'REDUCE','side':'SELL'})
        outputs=evaluate(event,LeaderScore.model_validate(analysis['score']),[],{},NOW+1000,self.worker.policy)
        self.assertIn('POSITION_LIFECYCLE_REQUIRED',consensus(event,outputs,NOW+1000,self.worker.policy).blockers)
    def test_overflow_and_unsorted_depth_are_unknown(self):
        from core.intelligence.models import LeaderScore
        analysis=self.activate(); event=self.signal()
        for levels in ([[{'px':'1e308','sz':'1e308'}],[{'px':'1e308','sz':'1e308'}]],
                       [[{'px':'99','sz':'1000'},{'px':'100','sz':'1000'}],[{'px':'101','sz':'1000'}]]):
            outputs=evaluate(event,LeaderScore.model_validate(analysis['score']),[],{'time':NOW+1000,'levels':levels},NOW+1000,self.worker.policy)
            self.assertEqual(next(a for a in outputs if a.agent_id=='liquidity').direction,'WAIT')
    def test_request_budget_is_bounded(self):
        budget=RequestBudget(self.reader,1)
        budget({'type':'l2Book'})
        with self.assertRaises(ValueError): budget({'type':'l2Book'})
    def test_invalid_registry_addresses_and_stale_observation(self):
        self.worker.observe([{'time':NOW,'users':['bad','no']},{'time':1,'users':[ADDRESS,ADDRESS]}],NOW)
        self.assertEqual(self.worker.snapshot(NOW)['counts'],{})
    def test_no_signing_or_execution_imports_in_intelligence(self):
        root=Path(__import__('core.intelligence.service',fromlist=['']).__file__).parent
        forbidden={'integrations','core.trading_engine','core.foundation.execution','core.foundation.copy_execution'}
        for path in root.glob('*.py'):
            tree=ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom): self.assertNotIn(node.module,forbidden)
                if isinstance(node,ast.Import):
                    for alias in node.names: self.assertNotIn(alias.name,forbidden)
            self.assertNotIn('private_key',path.read_text(encoding='utf-8'))
    def test_research_http_authentication_and_bounds(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from webapp.intelligence_api import router
        def auth(token):
            if token!='synthetic-user': raise HTTPException(401,'unauthorized')
            return {'id':7}
        app=FastAPI(); app.include_router(router(self.path,'TESTNET',auth))
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/intelligence').status_code,401)
            self.assertEqual(client.get('/api/intelligence/stream').status_code,401)
            self.assertEqual(client.get('/api/intelligence?limit=51',headers={'x-telegram-init-data':'synthetic-user'}).status_code,422)
            response=client.get('/api/intelligence',headers={'x-telegram-init-data':'synthetic-user'})
            self.assertEqual(response.status_code,200)
            self.assertFalse(response.json()['execution_enabled'])
    def test_research_view_no_creation_and_network_isolation(self):
        from webapp.intelligence_api import ResearchView
        absent=Path(self.path).with_name('absent.sqlite')
        self.assertEqual(ResearchView(absent,'TESTNET').read()['reason'],'WORKER_NOT_STARTED')
        self.assertFalse(absent.exists())
        self.discover(); before=Path(self.path).read_bytes()
        first=ResearchView(self.path,'TESTNET').read(limit=1)
        self.assertEqual(len(first['events']),1)
        second=ResearchView(self.path,'TESTNET').read(after=first['cursor'])
        self.assertGreater(second['cursor'],first['cursor'])
        self.assertEqual(ResearchView(self.path,'MAINNET').read()['events'],[])
        self.assertEqual(Path(self.path).read_bytes(),before)
    def test_sse_reconnect_payload_and_disconnect(self):
        import asyncio
        from fastapi import FastAPI
        from webapp.intelligence_api import router
        self.discover()
        api=router(self.path,'TESTNET',lambda token:{'id':7})
        endpoint=next(r.endpoint for r in api.routes if r.path=='/api/intelligence/stream')
        class Request:
            async def is_disconnected(self): return False
        async def one():
            response=await endpoint(Request(),0,'synthetic')
            output=await anext(response.body_iterator)
            await response.body_iterator.aclose()
            return output
        output=asyncio.run(one())
        self.assertIn('event: research',output)
        self.assertIn('WALLET_DISCOVERED',output)
        self.assertNotIn('private_key',output)
    def test_research_records_are_immutable_and_causally_linked(self):
        import sqlite3,json
        from core.intelligence.models import LeaderScore
        analysis=self.activate(); event=self.signal()
        self.worker.research(event,LeaderScore.model_validate(analysis['score']),self.reader._info,NOW+1000)
        with self.worker.store.transaction() as db:
            canonical=[json.loads(r[0]) for r in db.execute('SELECT body FROM events')]
        self.assertEqual(len([r for r in canonical if r['correlation_id']==event.event_id]),2)
        for sql in ('UPDATE intelligence_records SET kind=kind','DELETE FROM intelligence_records'):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.worker.store.transaction() as db: db.execute(sql)
