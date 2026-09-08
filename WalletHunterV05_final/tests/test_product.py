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

    def api(self,view,factory=None):
        from fastapi import FastAPI,HTTPException
        from fastapi.testclient import TestClient
        from webapp.product_api import router
        def authenticate(token):
            if token!='valid':raise HTTPException(401,'AUTH_REQUIRED')
            return {'id':int(view.scope.tenant)}
        events=ProductEvents(self.root/'data/product.sqlite3',view.clock)
        app=FastAPI();app.include_router(router(authenticate,lambda uid:view,lambda:events,factory or Mock()))
        return TestClient(app),events

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
