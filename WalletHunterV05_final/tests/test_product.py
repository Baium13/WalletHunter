import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import test_intelligence as fixture
from core.product_read import ProductReadModel,RuntimeBinding,performance
from core.product_events import ProductEvents
from core.product_control import ProductConfirmations
from core.foundation.store import scope_key


class ProductTests(unittest.TestCase):
    def test_api_budget_is_operational_truth_not_financial_data(self):
        from core.hl_budget import Budget
        b,r,_,view=self.setup_view()
        self.assertEqual(view.snapshot()['health']['api_budget']['state'],'UNKNOWN')
        budget=Budget(self.root/'data/hl-api-budget.sqlite3')
        budget.begin('orderStatus',{},'reconciliation',0)
        data=view.snapshot()['health']['api_budget']
        self.assertEqual(data['rest_weight_1m'],2)
        self.assertTrue(data['enforced'])
        self.assertNotIn(view.scope.account,json.dumps(data))

    def notifications(self,events,scope):
        with events.store.transaction() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM product_outbox WHERE scope=? AND status!=?',(scope_key(scope),'SUPPRESSED'))]

    def test_financial_only_actual_open_add_reduce_close_and_restart(self):
        from core.product_notifications import notice_text
        b,r,_,view=self.setup_view();events=ProductEvents(self.root/'notices.sqlite',b.clock)
        events.preferences(view.scope)
        for record in (r,self.case.followup(r,'ADD',1,2,side='BUY',suffix='add'),
                       self.case.followup(r,'REDUCE',2,1,suffix='reduce'),self.case.followup(r,'CLOSE',1,0,suffix='close')):
            b.process(record);events.ingest(view,50)
        rows=self.notifications(events,view.scope)
        self.assertEqual([e['type'] for e in rows],['POSITION_OPEN','POSITION_ADD','POSITION_REDUCE','POSITION_CLOSE'])
        for e in rows:
            text=notice_text(e,'internal-id')
            self.assertIn('🧪 PAPER',text);self.assertNotIn('internal-id',text);self.assertNotIn(view.scope.account,text)
        self.assertIn('net_pnl',rows[-1]['data']);self.assertIn('duration_ms',rows[-1]['data'])
        sent=[]
        async def send(e,i):sent.append(i)
        asyncio.run(events.deliver(view.scope,send));restarted=ProductEvents(events.store.path,b.clock)
        restarted.ingest(view,50);asyncio.run(restarted.deliver(view.scope,send))
        self.assertEqual(len(sent),4);self.assertEqual(len(set(sent)),4)

    def test_internal_events_never_enqueue_even_if_notify_requested(self):
        from core.product_notifications import notice_text
        b,r,_,v=self.setup_view();events=ProductEvents(self.root/'notices.sqlite',b.clock)
        kinds=['LEADER_EVENT','CONSENSUS_UPDATED','AGENT_UPDATED','DISCOVERY_UPDATED','LEADER_PROMOTED','RANKING_UPDATED','HEALTH_UPDATED','RISK_UPDATED','AUTHORIZATION_REQUIRED']
        for kind in kinds:
            events.publish(v.scope,kind,kind,{'status':'UNHEALTHY','wallet':'0x'+'a'*40},notify=True,critical=True)
            self.assertEqual(notice_text({'type':kind,'data':{}},'id'),'')
        self.assertEqual(self.notifications(events,v.scope),[])
        self.assertEqual(len(events.read(v.scope)['events']),len(kinds))

    def test_legacy_outbox_suppressed_without_deleting_activity_or_history(self):
        b,r,_,v=self.setup_view();events=ProductEvents(self.root/'notices.sqlite',b.clock)
        with events.store.transaction() as db:
            for n in range(260):db.execute('INSERT INTO product_outbox VALUES(?,?,?,?,?,?,?,?)',(scope_key(v.scope),str(n),json.dumps({'type':'CONSENSUS_UPDATED','data':{}}),'PENDING',0,0,0,None))
        sent=[]
        async def send(*a):sent.append(a)
        for _ in range(3):asyncio.run(events.deliver(v.scope,send))
        self.assertEqual(sent,[])
        with events.store.transaction() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM product_outbox WHERE status='SUPPRESSED'").fetchone()[0],260)

    def test_shadow_and_historical_fills_do_not_notify(self):
        b,r,_,v=self.setup_view('SHADOW');b.process(r)
        events=ProductEvents(self.root/'notices.sqlite',b.clock);events.ingest(v,50)
        self.assertEqual(self.notifications(events,v.scope),[])
        self.case=fixture.IntelligenceTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.root=Path(self.case.temp.name)
        b,r,_,v=self.setup_view();b.process(r)
        events=ProductEvents(self.root/'later.sqlite',lambda:b.clock()+1000);events.ingest(v,50)
        self.assertEqual(self.notifications(events,v.scope),[])
        self.assertTrue(events.read(v.scope)['events'])

    def test_unknown_one_critical_no_fake_position_or_retry(self):
        b,r,_,v=self.setup_view();events=ProductEvents(self.root/'notices.sqlite',b.clock);events.preferences(v.scope)
        b.exchange.behavior='ACK_LOSS';b.process(r)
        for _ in range(3):events.ingest(v,50);events.collect(v.snapshot())
        rows=self.notifications(events,v.scope)
        self.assertEqual([e['type'] for e in rows],['EXECUTION_UNKNOWN']);self.assertEqual(b.exchange.calls,1)

    def test_legacy_sent_unknown_survives_notification_policy_cutover(self):
        b,r,_,v=self.setup_view();events=ProductEvents(self.root/'notices.sqlite',b.clock)
        b.exchange.behavior='ACK_LOSS';b.process(r)
        intent=v.snapshot()['runtimes'][0]['actions'][0]['intent_id']
        payload={'intent_id':intent,'status':'UNKNOWN'}
        for n,data in enumerate((payload,{'payload':payload},{'evidence':payload},{'evidence':{'payload':payload}})):
            with self.subTest(envelope=n):
                events=ProductEvents(self.root/f'legacy-{n}.sqlite',b.clock)
                with events.store.transaction() as db:
                    db.execute('INSERT INTO product_outbox VALUES(?,?,?,?,?,?,?,?)',
                        (scope_key(v.scope),'legacy',json.dumps({'type':'EXECUTION_UPDATED','data':data}),
                         'SENT',1,0,1,None))
                for _ in range(2):
                    events=ProductEvents(events.store.path,b.clock);events.ingest(v,50)
                with events.store.transaction() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM product_outbox').fetchone()[0],1)

    def test_unknown_identity_envelopes_restart_modes_and_tenants(self):
        b,r,_,v=self.setup_view();events=ProductEvents(self.root/'identity.sqlite',b.clock)
        sent=[]
        async def send(e,i):sent.append(i)
        for n,data in enumerate(({'evidence':{'payload':{'intent_id':'same'}}},
                {'payload':{'intent_id':'same'}},{'intent_id':'same'})):
            events=ProductEvents(events.store.path,b.clock)
            events.publish(v.scope,str(n),'EXECUTION_UNKNOWN',dict(data,mode='LIVE'),notify=True)
            asyncio.run(events.deliver(v.scope,send))
        self.assertEqual(len(sent),1)
        for identity,mode in [('other','LIVE'),('same','PAPER')]:
            events.publish(v.scope,identity+mode,'EXECUTION_UNKNOWN',{'intent_id':identity,'mode':mode},notify=True)
        self.assertEqual(len(self.notifications(events,v.scope)),3)
        other=v.scope.model_copy(update={'tenant':'other','account':'0x'+'f'*40})
        events.publish(other,'other-tenant','EXECUTION_UNKNOWN',{'intent_id':'same','mode':'LIVE'},notify=True)
        self.assertEqual(len(self.notifications(events,other)),1)

    def test_ambiguous_notification_identity_cannot_suppress_other_intent(self):
        from core.product_notifications import execution_identity
        self.assertEqual(execution_identity({'intent_id':'a','evidence':{'payload':{'intent_id':'b'}}})[0],None)
        self.assertEqual(execution_identity({'symbol':'BTC','action':'OPEN'})[0],None)

    def test_agent_availability_counts_active_waiting_not_degraded(self):
        from core.product_read import agent_availability
        agents=[{'status':'ACTIVE'} for _ in range(6)]+[{'status':'WAITING'}]
        before=json.dumps(agents)
        self.assertEqual(agent_availability(agents)['available'],7)
        self.assertEqual(agent_availability(agents)['active'],6)
        self.assertEqual(agent_availability(agents)['waiting'],1)
        self.assertEqual(json.dumps(agents),before)
        agents[0]={'status':'DEGRADED','readiness':'READY'};agents[1]={'status':'OFFLINE','readiness':'READY'}
        self.assertEqual(agent_availability(agents)['available'],5)

    def test_risk_readiness_is_separate_from_poison_jobs_and_historical_decision(self):
        from core.product_read import runtime_risk_health
        worker=dict(status='DEGRADED',heartbeat_ms=200000,quarantine_count=6,quarantined_jobs=3,
            unresolved_execution_count=0,reconciliation_backlog=0,processing_lag_ms=0,
            readiness_version='configured-worker-v1',ready_components=['risk'])
        risk={'created_ms':1,'reasons':['SCOPE_MISMATCH']};execution={'reconciliation_backlog':0}
        h=runtime_risk_health(worker,risk,execution,200000)
        self.assertEqual(h['status'],'READY');self.assertFalse(h['decision_fresh'])
        self.assertEqual(h['last_decision_reasons'],['SCOPE_MISMATCH'])
        self.assertEqual(h['processing_degradation'],['QUARANTINED_INPUT_JOBS'])
        self.assertEqual(worker['status'],'DEGRADED')
        for broken in ({'heartbeat_ms':1},{'unresolved_execution_count':1},{'processing_lag_ms':1},
                       {'status':'UNHEALTHY'},{'quarantine_count':0},{'ready_components':[]}):
            self.assertNotEqual(runtime_risk_health(dict(worker,**broken),risk,execution,200000)['status'],'READY')

    def test_idle_agents_keep_readiness_without_refreshing_old_signals(self):
        from core.product_read import analysis_projection,AGENT_IDS
        latest={'agents':[{'agent_id':n,'created_ms':1,'direction':'WAIT','instrument':{'symbol':'BTC'},
            'evidence':['INSUFFICIENT_EVIDENCE'],'freshness':'UNKNOWN'} for n in AGENT_IDS],
            'consensus':{'created_ms':1,'decision':'WAIT'}}
        worker={'heartbeat_ms':200000,'readiness_version':'configured-worker-v1','status':'DEGRADED',
            'quarantined_jobs':3,'ready_components':[*AGENT_IDS,'consensus']}
        p=analysis_projection(latest,worker,200000)
        self.assertEqual(p['agent_summary']['available'],7);self.assertEqual(p['agent_summary']['waiting'],7)
        self.assertTrue(all(a['signal_status']=='STALE' and a['result']['created_ms']==1 for a in p['agents']))
        self.assertEqual(p['consensus_health']['status'],'READY');self.assertFalse(p['consensus_health']['decision_fresh'])
        self.assertFalse(p['execution_authority']);self.assertTrue(p['agents'][4]['missing_inputs'])
        self.assertEqual(analysis_projection(latest,worker,300001)['agent_summary']['offline'],7)
        self.assertEqual(analysis_projection(latest,dict(worker,status='UNHEALTHY'),200000)['agent_summary']['available'],0)

    def test_public_analysis_available_without_execution_runtime(self):
        b,r,_,view=self.setup_view()
        self.case.worker.health_observation('leader_detection',b.clock(),details={'watched':1,'scanned':1})
        readonly=ProductReadModel(self.root,view.scope,[],b.clock,research_path=self.case.worker.store.path)
        s=readonly.snapshot();a=s['analysis']
        self.assertEqual(len(a['agents']),7);self.assertEqual(a['agent_summary']['available'],7)
        self.assertEqual(a['analysis_scope'],'PUBLIC_RESEARCH');self.assertFalse(a['execution_authority'])
        self.assertEqual(s['runtimes'],[]);self.assertEqual(b.exchange.calls,0)
        self.assertEqual(a['shared_across_modes'],['OBSERVE','PAPER_AUTO','SHADOW','LIVE_CONFIRM'])
        self.assertGreater(a['input_evidence']['candle_count'],0)
        other=ProductReadModel(self.root,view.scope.model_copy(update={'network':'MAINNET' if view.scope.network=='TESTNET' else 'TESTNET'}),[],b.clock,research_path=self.case.worker.store.path)
        self.assertEqual(other.snapshot()['analysis']['agent_summary']['available'],0)

    def test_notification_preferences_strict_scoped_authenticated(self):
        b,r,_,v=self.setup_view();client,events=self.api(v);url='/api/product/notification-preferences';h={'x-telegram-init-data':'valid'}
        self.assertEqual(client.put(url,json={'OPEN':False}).status_code,401)
        for change in ({'OPEN':'false'},{'OPEN':None},{'RISK_UPDATED':True},{'tenant':'other'}):
            self.assertEqual(client.put(url,headers=h,json=change).status_code,422)
        self.assertFalse(client.put(url,headers=h,json={'OPEN':False}).json()['preferences']['OPEN'])
        self.assertTrue(events.preferences(v.scope.model_copy(update={'account':'0x'+'f'*40,'tenant':'other'}))['preferences']['OPEN'])
        b.process(r);events.ingest(v,50);sent=[]
        async def send(*a):sent.append(a)
        asyncio.run(events.deliver(v.scope,send));self.assertEqual(sent,[])

    def test_reverse_is_one_notification_only_after_both_proven_legs(self):
        b,r,_,v=self.setup_view();events=ProductEvents(self.root/'notices.sqlite',b.clock);events.preferences(v.scope)
        b.process(r);events.ingest(v,50)
        reverse=self.case.followup(r,'REVERSE',1,-1)
        for candle in reverse['candles']:
            high,low=float(candle['h']),float(candle['l'])
            candle.update(c=str(200-float(candle['c'])),h=str(200-low),l=str(200-high))
        result=b.process(reverse);self.assertEqual(result['open']['status'],'FILLED',result['open'])
        events.ingest(v,50)
        rows=self.notifications(events,v.scope)
        self.assertEqual([e['type'] for e in rows],['POSITION_OPEN','POSITION_REVERSE'])
        self.assertEqual((rows[-1]['data']['previous_side'],rows[-1]['data']['side']),('LONG','SHORT'))

    def test_reverse_unproven_close_is_one_critical_not_a_reverse(self):
        b,r,_,v=self.setup_view();events=ProductEvents(self.root/'notices.sqlite',b.clock);events.preferences(v.scope)
        b.process(r);events.ingest(v,50);b.exchange.behavior='ACK_LOSS'
        b.process(self.case.followup(r,'REVERSE',1,-1));events.ingest(v,50);events.ingest(v,50)
        self.assertEqual([e['type'] for e in self.notifications(events,v.scope)],['POSITION_OPEN','RECONCILIATION_REQUIRED'])
        self.assertEqual(b.exchange.calls,2)

    def test_financial_health_critical_but_discovery_degradation_silent(self):
        b,r,_,v=self.setup_view();events=ProductEvents(self.root/'notices.sqlite',b.clock);snapshot=v.snapshot()
        snapshot['health']['components']['discovery']['status']='UNHEALTHY';events.collect(snapshot)
        self.assertEqual(self.notifications(events,v.scope),[])
        snapshot['health']['components']['private_account']['status']='UNHEALTHY';events.collect(snapshot);events.collect(snapshot)
        self.assertEqual([e['type'] for e in self.notifications(events,v.scope)],['CRITICAL_TRADING_FAILURE'])

    def test_desktop_copy_sender_cannot_bypass_financial_outbox(self):
        import ast
        tree=ast.parse((Path(__file__).parents[1]/'desktop/main.py').read_text(encoding='utf-8'))
        notify=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='notify')
        self.assertFalse(any(isinstance(n,ast.Call) for n in ast.walk(notify)))

    def test_confirmation_is_optional_live_label_without_research_dump(self):
        from core.product_notifications import notice_text,classification
        self.assertIsNone(classification('POSITION_OPEN',[]))
        text=notice_text({'type':'LIVE_CONFIRM_REQUIRED','data':{'mode':'LIVE_CONFIRM','symbol':'BTC','intent_id':'internal-only','raw_reason':'unsafe-debug'}},'id',False)
        self.assertIn('⚠️ LIVE',text);self.assertIn('BTC',text)
        self.assertNotIn('internal-only',text);self.assertNotIn('unsafe-debug',text)

    def setUp(self):
        self.case=fixture.IntelligenceTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.root=Path(self.case.temp.name)

    def setup_view(self,mode='PAPER_AUTO'):
        b,r=self.case.paper_backend(mode);scope=b.auth_policy.scope
        config=self.root/(mode+'.json');config.write_text(json.dumps({'allocation':b.allocation_policy.model_dump(mode='json'),
            'authorization':b.auth_policy.model_dump(mode='json'),'risk':b.risk.policy.model_dump(mode='json')}))
        binding=RuntimeBinding(scope=scope,mode=mode,state_path=str(b.store.path),config_path=str(config))
        view=ProductReadModel(self.root,scope,[binding],b.clock,research_path=self.case.worker.store.path)
        return b,r,binding,view

    def test_authoritative_paper_position_and_allocation(self):
        b,r,_,view=self.setup_view();b.process(r)
        snapshot=view.snapshot();m=snapshot['runtimes'][0]
        self.assertEqual(m['runtime_mode'],'PAPER_AUTO');self.assertEqual(m['episodes'][0]['state'],'OPEN')
        self.assertGreater(m['allocation']['committed'],0);self.assertLess(m['allocation']['available'],m['allocation']['allocation_limit'])
        self.assertFalse(snapshot['legacy_paper_substitution']);self.assertEqual(m['episodes'][0]['origin'],'AUTONOMOUS')
        self.assertGreater(len(m['episodes'][0]['timeline']),4);self.assertEqual(len(m['agents']),7)
        self.assertIsNotNone(m['episodes'][0]['current_price'])

    def test_mode_separated_outcome_and_calibration(self):
        for mode in ('PAPER_AUTO','SHADOW'):
            self.case=fixture.IntelligenceTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
            self.root=Path(self.case.temp.name)
            b,r,_,view=self.setup_view(mode);b.process(r);b.process(self.case.followup(r,'CLOSE',1,0))
            m=view.snapshot()['runtimes'][0]
            self.assertEqual(m['analytics']['trade_count'],1);self.assertIsNotNone(m['analytics']['net_pnl'])
            self.assertEqual(m['calibration'][0]['mode'],'PAPER' if mode=='PAPER_AUTO' else 'SHADOW')
            self.assertEqual(m['episodes'][0]['outcome']['mode'],'PAPER' if mode=='PAPER_AUTO' else 'SHADOW')

    def test_missing_stale_and_unknown_health(self):
        b,r,_,view=self.setup_view();s=view.snapshot();self.assertEqual(s['health']['components']['risk']['status'],'UNKNOWN')
        b.exchange.behavior='ACK_LOSS';b.process(r)
        with b.store.transaction() as db:db.execute('INSERT OR REPLACE INTO autonomous_health VALUES(?,?)',(scope_key(b.auth_policy.scope),json.dumps({'heartbeat_ms':b.clock()})))
        self.assertEqual(view.snapshot()['health']['components']['reconciliation']['status'],'UNHEALTHY')
        self.assertEqual(view.snapshot()['runtimes'][0]['execution']['unknown'],1)

    def test_tenant_network_isolation(self):
        b,r,binding,view=self.setup_view();b.process(r)
        for field,value in [('tenant','other'),('network','TESTNET' if b.auth_policy.scope.network=='MAINNET' else 'MAINNET')]:
            scope=b.auth_policy.scope.model_copy(update={field:value})
            other=ProductReadModel(self.root,scope,[binding],b.clock)
            self.assertEqual(other.snapshot()['runtimes'],[])
            with self.assertRaises(KeyError):other.episode(view.snapshot()['runtimes'][0]['episodes'][0]['episode_id'])

    def test_analytics_null_not_zero_and_finite(self):
        self.assertIsNone(performance([])['net_pnl'])
        self.assertIsNone(performance([{'version':'outcome-v2','net_pnl':None}])['expectancy'])

    def test_outbox_restart_dedup_and_failure(self):
        b,_,_,_=self.setup_view();events=ProductEvents(self.root/'product.sqlite',b.clock);scope=b.auth_policy.scope
        events.publish(scope,'unknown','EXECUTION_UPDATED',{'status':'UNKNOWN'},notify=True,critical=True)
        events.publish(scope,'unknown','EXECUTION_UPDATED',{'status':'UNKNOWN'},notify=True,critical=True)
        self.assertEqual(len(events.read(scope)['events']),1)
        async def fail(*args):raise RuntimeError('never expose token or exception')
        asyncio.run(events.deliver(scope,fail));restarted=ProductEvents(self.root/'product.sqlite',lambda:b.clock()+120000)
        delivered=[]
        async def send(*args):delivered.append(args)
        asyncio.run(restarted.deliver(scope,send));asyncio.run(restarted.deliver(scope,send))
        self.assertEqual(len(delivered),1)
        with restarted.store.transaction() as db:self.assertEqual(db.execute('SELECT status FROM product_outbox').fetchone()[0],'SENT')

    def test_stream_bounded_resume_scope_and_malformed(self):
        b,_,_,_=self.setup_view();events=ProductEvents(self.root/'product.sqlite',b.clock);scope=b.auth_policy.scope
        events.RETENTION=3
        for n in range(6):events.publish(scope,str(n),'HEALTH_UPDATED',{'n':n})
        page=events.read(scope,1,2);self.assertTrue(page['reset_required']);self.assertEqual(len(page['events']),2)
        self.assertEqual(len(events.read(scope,page['cursor'])['events']),1)
        self.assertEqual(events.read(scope.model_copy(update={'tenant':'other'}))['events'],[])
        with events.store.transaction() as db:db.execute("UPDATE product_events SET body='{' WHERE seq=(SELECT MAX(seq) FROM product_events)")
        self.assertEqual(events.read(scope,page['cursor'])['events'],[])

    def live_control(self):
        b,r,before=self.case.live_backend();b.process(r)
        config=self.root/'live.json';config.write_text(json.dumps({'allocation':b.allocation_policy.model_dump(mode='json'),
            'authorization':b.auth_policy.model_dump(mode='json'),'risk':b.risk.policy.model_dump(mode='json')}))
        binding=RuntimeBinding(scope=b.auth_policy.scope,mode='LIVE_CONFIRM',state_path=str(b.store.path),config_path=str(config))
        factory=Mock(return_value=b)
        control=ProductConfirmations(b.auth_policy.scope,binding,self.root,factory,b.clock)
        view=ProductReadModel(self.root,b.auth_policy.scope,[binding],b.clock,research_path=self.case.worker.store.path)
        return b,before,control,factory,view

    def test_live_confirm_exact_grant_full_route_and_duplicate(self):
        from core.foundation.copy_execution import HyperliquidExecutionAdapter,LiveReport
        from core.foundation.contracts import Fill,ExecutionReceipt
        from core.foundation.paper_effect import effect
        b,before,control,factory,view=self.live_control();proposal=control.proposals()[0]
        self.assertEqual(proposal['status'],'PENDING');factory.assert_not_called()
        def submit(adapter,intent,snapshot,now):
            fill=Fill(intent_id=intent.intent_id,instrument=intent.instrument,order_id=intent.intent_id,
                trade_id=intent.intent_id,side=intent.side,size=intent.size,price=intent.limit_price,exchange_ms=now)
            return LiveReport(ExecutionReceipt(intent_id=intent.intent_id,scope=intent.scope,status='FILLED',order_ids=(intent.intent_id,),
                fills=(fill,),reconciliation='CONFIRMED',received_ms=now,provenance='EXCHANGE'),effect(intent,snapshot,(fill,),now))
        with patch.object(HyperliquidExecutionAdapter,'refresh',return_value=before.model_copy(update={'revision':2})),patch.object(HyperliquidExecutionAdapter,'submit',autospec=True,side_effect=submit) as submitted:
            result=control.act(proposal['id'],proposal['proposal_hash'],'7',True)
            self.assertEqual(result['status'],'FILLED');self.assertEqual(submitted.call_count,1)
            control.act(proposal['id'],proposal['proposal_hash'],'7',True);self.assertEqual(submitted.call_count,1)
        self.assertEqual(view.snapshot()['runtimes'][0]['episodes'][0]['state'],'OPEN')

    def test_reject_never_constructs_signer_and_blocks_later_service_confirmation(self):
        b,_,c,factory,_=self.live_control();p=c.proposals()[0]
        self.assertEqual(c.act(p['id'],p['proposal_hash'],'7',False)['status'],'REJECTED');factory.assert_not_called()
        with self.assertRaises(ValueError):b.confirm(p['id'],authenticated_user='7')
        with b.store.transaction() as db:
            self.assertEqual(json.loads(db.execute('SELECT body FROM position_episodes').fetchone()[0])['state'],'REJECTED')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intents').fetchone()[0],0)

    def test_expiry_tenant_hash_and_no_latest_confirmation(self):
        b,_,c,factory,_=self.live_control();p=c.proposals()[0]
        for uid,identity,hashed in [('other',p['id'],p['proposal_hash']),('7','latest',p['proposal_hash']),('7',p['id'],'0'*64)]:
            with self.assertRaises(ValueError):c.act(identity,hashed,uid,True)
        c.clock=lambda:p['intent']['expires_ms']
        with self.assertRaises(ValueError):c.act(p['id'],p['proposal_hash'],'7',True)
        factory.assert_not_called()

    def api(self,view,factory=None,market=None):
        from fastapi import FastAPI,HTTPException
        from fastapi.testclient import TestClient
        from webapp.product_api import router
        def authenticate(token):
            if token!='valid':raise HTTPException(401,'AUTH_REQUIRED')
            return {'id':int(view.scope.tenant)}
        events=ProductEvents(self.root/'data/product.sqlite3',view.clock)
        app=FastAPI();app.include_router(router(authenticate,lambda uid:view,lambda:events,factory or Mock(),market))
        return TestClient(app),events

    def test_product_market_scoped_finite_and_read_only(self):
        b,r,_,view=self.setup_view();reader=Mock(return_value={'candles':[{'t':10,'o':100.,'h':102.,'l':99.,'c':101.}],
            'mark':{'price':101.,'time':b.clock()}});signer=Mock();client,_=self.api(view,signer,reader)
        params={'coin':'BTC','network':view.scope.network};header={'x-telegram-init-data':'valid'}
        self.assertEqual(client.get('/api/product/market/candles',params=params).status_code,401)
        mismatch={**params,'network':'MAINNET' if view.scope.network=='TESTNET' else 'TESTNET'}
        self.assertEqual(client.get('/api/product/market/candles',params=mismatch,headers=header).status_code,409)
        reader.assert_not_called()
        result=client.get('/api/product/market/candles',params=params,headers=header)
        self.assertEqual(result.status_code,200);self.assertEqual(result.json()['scope']['tenant'],view.scope.tenant)
        self.assertIsNone(result.json()['exchange_timestamp']);signer.assert_not_called()

    def test_product_market_invalid_is_unavailable_not_zero(self):
        b,r,_,view=self.setup_view();reader=Mock(return_value={'candles':[{'t':10,'o':float('nan'),'h':102.,'l':99.,'c':101.}]})
        client,_=self.api(view,market=reader);params={'coin':'BTC','network':view.scope.network};h={'x-telegram-init-data':'valid'}
        self.assertEqual(client.get('/api/product/market/candles',params=params,headers=h).status_code,503)
        reader.return_value={'candles':[],'mark':{'price':float('inf'),'time':10}}
        result=client.get('/api/product/market/candles',params=params,headers=h).json();self.assertIsNone(result['mark'])
        self.assertEqual(client.get('/api/product/market/candles',params={**params,'network':'invalid'},headers=h).status_code,422)

    def test_api_contracts_authoritative_views_and_no_signer(self):
        b,r,_,view=self.setup_view();b.process(r);factory=Mock();client,_=self.api(view,factory)
        header={'x-telegram-init-data':'valid'}
        self.assertEqual(client.get('/api/product').status_code,401)
        for path in ('','/positions','/agents','/consensus','/risk','/execution','/capital','/analytics','/health','/discovery','/leaders','/calibration','/manual-copy','/mode'):
            response=client.get('/api/product'+path,headers=header);self.assertEqual(response.status_code,200,path)
        eid=view.snapshot()['runtimes'][0]['episodes'][0]['episode_id']
        self.assertEqual(client.get('/api/product/timeline/'+eid,headers=header).status_code,200)
        self.assertEqual(client.get('/api/product/positions/unknown',headers=header).status_code,404)
        factory.assert_not_called()

    def test_api_live_pending_and_reject_exact_proposal(self):
        b,_,c,factory,view=self.live_control();client,_=self.api(view,factory);header={'x-telegram-init-data':'valid'}
        p=client.get('/api/product/confirmations/pending',headers=header).json()['proposals'][0]
        self.assertEqual(client.post('/api/product/confirmations/'+p['id']+'/approve',headers=header,json={'proposal_hash':'0'*64}).status_code,409)
        response=client.post('/api/product/confirmations/'+p['id']+'/reject',headers=header,json={'proposal_hash':p['proposal_hash']})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['status'],'REJECTED');factory.assert_not_called()

    def test_api_resume_initial_snapshot_and_no_duplicate_events(self):
        b,r,_,view=self.setup_view();b.process(r);client,events=self.api(view);header={'x-telegram-init-data':'valid'}
        first=client.get('/api/product/events/resume',headers=header).json()
        second=client.get('/api/product/events/resume?after='+str(first['cursor']),headers=header).json()
        self.assertIn('snapshot',first);self.assertEqual(second['events'],[])
        self.assertEqual(second['cursor'],first['cursor'])

    def test_outbox_attempt_bound_and_critical_priority(self):
        b,_,_,_=self.setup_view();now=[b.clock()];events=ProductEvents(self.root/'events.sqlite',lambda:now[0]);scope=b.auth_policy.scope
        events.publish(scope,'normal','LEADER_PROMOTED',{'status':'ACTIVE'},notify=True)
        events.publish(scope,'critical','EXECUTION_UPDATED',{'status':'UNKNOWN'},notify=True,critical=True)
        seen=[]
        async def fail(event,identity):seen.append(event['data']['status']);raise TimeoutError()
        for n in range(7):asyncio.run(events.deliver(scope,fail,limit=1));now[0]+=3600000
        self.assertEqual(seen[0],'UNKNOWN')
        with events.store.transaction() as db:self.assertEqual(db.execute("SELECT attempts FROM product_outbox WHERE critical=1").fetchone()[0],5)

    def test_privacy_fields_never_project_and_snapshot_reads_do_not_write(self):
        import hashlib
        b,r,_,view=self.setup_view();b.process(r)
        before=hashlib.sha256(Path(b.store.path).read_bytes()).hexdigest()
        snapshot=view.snapshot();self.assertEqual(before,hashlib.sha256(Path(b.store.path).read_bytes()).hexdigest())
        text=json.dumps(snapshot);self.assertNotIn('private_key',text);self.assertNotIn('telegram_bot_token',text)

    def test_stale_agent_is_not_online_and_missing_metrics_null(self):
        b,r,_,view=self.setup_view();b.process(r);view.clock=lambda:b.clock()+500000
        m=view.snapshot()['runtimes'][0]
        self.assertTrue(all(a['status']=='DEGRADED' for a in m['agents']))
        self.assertIsNone(m['analytics']['profit_factor']);self.assertIsNone(m['analytics']['net_pnl'])
        self.assertIsNone(m['episodes'][0]['current_price']);self.assertIsNone(m['episodes'][0]['pnl'])

    def test_manual_copy_uses_post_fill_capital_not_pretrade_worker_projection(self):
        from test_manual_leader_copy import ManualCopyWorkerTests
        from core.foundation.contracts import Scope
        case=ManualCopyWorkerTests();case.setUp();self.addCleanup(case.doCleanups)
        report=case.assert_action('OPEN');self.assertEqual(report['committed'],0)
        root=Path(case.case.engine.journal.path).parent.parent
        scope=Scope(tenant='1',account=case.f.ACCOUNT,network='TESTNET')
        snapshot=ProductReadModel(root,scope,clock=lambda:int(case.now*1000)).snapshot()
        manual=snapshot['manual_copy']
        self.assertTrue(manual['configured']);self.assertAlmostEqual(manual['committed'],8.)
        self.assertEqual(manual['reserved'],0.);self.assertAlmostEqual(manual['available'],72.)
        self.assertEqual(manual['positions'][0]['origin'],'MANUAL_LEADER_COPY')
        shared_leader_view=ProductReadModel(root,scope,clock=lambda:int(case.now*1000),sources=(case.f.SOURCE_A,)).snapshot()
        self.assertEqual(shared_leader_view['account']['source_allocations'][case.f.SOURCE_A]['committed'],0.)
        self.assertAlmostEqual(shared_leader_view['manual_copy']['committed'],8.)

    def test_telegram_failure_does_not_prevent_subsequent_canonical_close(self):
        b,r,_,view=self.setup_view();b.process(r)
        events=ProductEvents(self.root/'data/product.sqlite3',b.clock);events.ingest(view);events.collect(view.snapshot())
        async def fail(*args):raise TimeoutError()
        asyncio.run(events.deliver(b.auth_policy.scope,fail))
        b.process(self.case.followup(r,'CLOSE',1,0))
        self.assertEqual(view.snapshot()['runtimes'][0]['episodes'][0]['state'],'CLOSED')
        self.assertEqual(view.snapshot()['health']['components']['telegram']['status'],'DEGRADED')

    def test_actual_sse_snapshot_reconnect_and_bounded_generator(self):
        from unittest.mock import AsyncMock
        b,r,_,view=self.setup_view();b.process(r);client,_=self.api(view);header={'x-telegram-init-data':'valid'}
        with patch('webapp.product_api.asyncio.sleep',new_callable=AsyncMock):
            response=client.get('/api/product/events/stream',headers=header)
        self.assertEqual(response.status_code,200)
        records=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        self.assertEqual(len(records),15);self.assertIn('snapshot',records[0])
        self.assertTrue(all(not r['events'] for r in records[1:]))
        resumed=client.get('/api/product/events/resume?after='+str(records[-1]['cursor']),headers=header).json()
        self.assertEqual(resumed['events'],[])

    def test_registry_scope_and_disabled_live_auto(self):
        from core.product_runtime import bindings
        b,r,binding,_=self.setup_view();directory=self.root/'data';directory.mkdir(exist_ok=True)
        path=directory/'product-runtime.json';path.write_text(json.dumps({'version':1,'runtimes':[binding.model_dump(mode='json')]}))
        self.assertEqual(bindings(self.root),(binding,))
        wrong=binding.model_dump(mode='json');wrong['mode']='LIVE_AUTO'
        path.write_text(json.dumps({'version':1,'runtimes':[wrong]}))
        with self.assertRaises(ValueError):bindings(self.root)

    def test_product_event_payload_drops_private_fields(self):
        b,_,_,_=self.setup_view();events=ProductEvents(self.root/'events.sqlite',b.clock)
        events.publish(b.auth_policy.scope,'safety','HEALTH_UPDATED',{'private_key':'synthetic-secret','nested':{'telegram_bot_token':'synthetic'},'capacity':float('nan')})
        data=events.read(b.auth_policy.scope)['events'][0]['data']
        self.assertNotIn('private_key',data);self.assertNotIn('telegram_bot_token',data['nested']);self.assertIsNone(data['capacity'])

    def test_corrupt_embedded_account_scope_is_not_exposed(self):
        b,r,_,view=self.setup_view();b.process(r)
        with b.store.transaction() as db:
            row=db.execute('SELECT body FROM portfolios').fetchone();p=json.loads(row[0]);p['scope']['account']='0x'+'f'*40
            db.execute('UPDATE portfolios SET body=?',(json.dumps(p),))
        mode=view.snapshot()['runtimes'][0]
        self.assertEqual(mode['status'],'UNKNOWN');self.assertNotIn('portfolio',mode)

    def test_corrupt_embedded_proposal_scope_cannot_reach_factory(self):
        b,_,control,factory,_=self.live_control()
        with b.store.transaction() as db:
            row=db.execute('SELECT intent FROM autonomous_decisions').fetchone();intent=json.loads(row[0]);intent['scope']['tenant']='other'
            db.execute('UPDATE autonomous_decisions SET intent=?',(json.dumps(intent),))
        with self.assertRaises(ValueError):control.proposals()
        factory.assert_not_called()

    def test_offline_product_recovers_source_backlog_beyond_snapshot_window(self):
        from core.foundation.contracts import DomainEvent,ExecutionReceipt
        b,_,_,view=self.setup_view();scope=b.auth_policy.scope
        for n in range(110):
            receipt=ExecutionReceipt(intent_id='offline-'+str(n),scope=scope,status='UNKNOWN',reconciliation='RECONCILIATION_REQUIRED',received_ms=b.clock(),provenance='UNKNOWN')
            b.store.append(DomainEvent(event_id='offline-'+str(n),event_type='EXECUTION_UNKNOWN',correlation_id='offline-'+str(n),
                scope=scope,event_ms=b.clock(),received_ms=b.clock(),payload=receipt))
        events=ProductEvents(self.root/'data/product.sqlite3',b.clock)
        for _ in range(8):events.ingest(view)
        with events.store.transaction() as db:
            count=db.execute("SELECT COUNT(*) FROM product_outbox WHERE body LIKE '%offline-%'").fetchone()[0]
        self.assertEqual(count,110)
        restarted=ProductEvents(self.root/'data/product.sqlite3',b.clock);restarted.ingest(view)
        with restarted.store.transaction() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM product_outbox WHERE body LIKE '%offline-%'").fetchone()[0],110)

    def test_source_publish_crash_before_offset_is_idempotent(self):
        from test_autonomous_durability import Crash
        b,r,_,view=self.setup_view();b.process(r);events=ProductEvents(self.root/'data/product.sqlite3',b.clock)
        publish=events.publish
        def crash(*args,**kwargs):publish(*args,**kwargs);raise Crash()
        with patch.object(events,'publish',side_effect=crash),self.assertRaises(Crash):events.ingest(view)
        resumed=ProductEvents(self.root/'data/product.sqlite3',b.clock);resumed.ingest(view)
        items=resumed.read(b.auth_policy.scope,limit=100)['events']
        self.assertEqual(len({e['id'] for e in items}),len(items))

    def test_restored_source_path_preserves_notification_identity(self):
        import sqlite3
        from contextlib import closing
        b,r,binding,view=self.setup_view();b.process(r)
        events=ProductEvents(self.root/'data/product.sqlite3',b.clock);events.ingest(view)
        with events.store.transaction() as db:before=db.execute('SELECT COUNT(*) FROM product_outbox').fetchone()[0]
        restored=self.root/'restored.sqlite'
        with closing(sqlite3.connect(b.store.path)) as source,closing(sqlite3.connect(restored)) as destination:source.backup(destination)
        relocated=ProductReadModel(self.root,b.auth_policy.scope,[binding.model_copy(update={'state_path':str(restored)})],b.clock,research_path=self.case.worker.store.path)
        events.ingest(relocated)
        with events.store.transaction() as db:self.assertEqual(before,db.execute('SELECT COUNT(*) FROM product_outbox').fetchone()[0])

    def test_legacy_rejected_episode_does_not_hide_proven_open_exposure(self):
        b,r,_,view=self.setup_view();b.process(r)
        with b.store.transaction() as db:
            episode=json.loads(db.execute('SELECT body FROM position_episodes').fetchone()[0]);episode['state']='REJECTED'
            db.execute('UPDATE position_episodes SET body=?',(json.dumps(episode),))
        visible=view.snapshot()['runtimes'][0]['episodes'][0]
        self.assertEqual(visible['state'],'RECONCILIATION_REQUIRED');self.assertTrue(visible['unresolved']);self.assertGreater(visible['size'],0)
        with b.store.transaction() as db:self.assertEqual(json.loads(db.execute('SELECT body FROM position_episodes').fetchone()[0])['state'],'REJECTED')

    def test_research_consensus_stream_is_not_account_authorization(self):
        b,r,_,view=self.setup_view();events=ProductEvents(self.root/'events.sqlite',b.clock);events.ingest(view)
        decisions=[e for e in events.read(b.auth_policy.scope,limit=100)['events'] if e['type']=='CONSENSUS_UPDATED']
        self.assertTrue(decisions)
        self.assertTrue(all(e['data']['stage']=='RESEARCH_ONLY' and e['data']['execution_authority'] is False for e in decisions))
        self.assertEqual(b.exchange.calls,0)
