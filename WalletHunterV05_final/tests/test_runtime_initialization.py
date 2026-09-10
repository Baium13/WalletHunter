"""Offline runtime boundary tests; fixtures never load production state."""
import unittest
import json
from pathlib import Path
from unittest.mock import patch
import test_engine_safety as engine_fixture


class RuntimeInitializationTests(unittest.TestCase):
    def test_paused_follower_read_is_bounded_and_never_writes_execution(self):
        from test_manual_leader_copy import ManualCopyWorkerTests
        from core.product_read import ProductReadModel
        case=ManualCopyWorkerTests();case.setUp()
        try:
            case.worker.service.stop(case.account,case.client)
            scope=case.worker.service._scope(case.account,case.client)
            from core.foundation.data import copy_account_snapshot
            with patch('core.foundation.data.copy_account_snapshot',wraps=copy_account_snapshot) as read, \
                    patch.object(case.client,'submit_copy_ioc') as submit:
                first=case.cycle();case.cycle()
                self.assertEqual(read.call_count,1);submit.assert_not_called()
            self.assertEqual(first['status'],'PAUSED')
            self.assertEqual(first['account_evidence']['equity'],100.)
            root=Path(case.case.engine.journal.path).parent.parent
            view=ProductReadModel(root,scope,clock=lambda:int(case.now*1000))
            snapshot=view.snapshot();manual=snapshot['manual_copy']
            self.assertEqual(manual['ui_state'],'PAUSED');self.assertIn('RESUME',manual['valid_actions'])
            self.assertEqual(manual['account_balance'],100.);self.assertEqual(manual['allocation_limit'],80.)
            self.assertEqual(manual['committed'],0.);self.assertEqual(manual['reserved'],0.);self.assertEqual(manual['available'],80.)
            self.assertFalse(case.case.engine.journal.pending(case.f.ACCOUNT))
            self.assertFalse(case.client.calls)
            view.clock=lambda:int(case.now*1000)+120001
            self.assertEqual(view.snapshot()['manual_copy']['capital_status'],'STALE')
        finally:case.doCleanups()

    def test_paused_read_failure_does_not_invent_capital_or_authority(self):
        from test_manual_leader_copy import ManualCopyWorkerTests
        case=ManualCopyWorkerTests();case.setUp()
        try:
            case.worker.service.stop(case.account,case.client)
            with patch('core.foundation.data.copy_account_snapshot',side_effect=ValueError('synthetic private_key')):
                report=case.cycle()
            self.assertIsNone(report.get('account_evidence'))
            self.assertEqual(report['account_read_error'],'ACCOUNT_DATA_UNAVAILABLE')
            self.assertNotIn('private_key',json.dumps(report));self.assertFalse(case.client.calls)
        finally:case.doCleanups()

    def test_selected_draft_refreshes_read_only_account_evidence(self):
        from test_manual_leader_copy import ManualCopyWorkerTests
        from unittest.mock import patch
        case=ManualCopyWorkerTests();case.setUp()
        try:
            # Switching a selected wallet creates a disabled draft with no
            # generation.  It must still refresh the account card, without
            # subscribing or submitting anything.
            case.worker.service.configure(case.account,case.client,case.f.SOURCE_B,80.)
            from core.foundation.data import copy_account_snapshot
            with patch('core.foundation.data.copy_account_snapshot',wraps=copy_account_snapshot) as read, \
                    patch.object(case.client,'submit_copy_ioc') as submit:
                report=case.cycle()
                self.assertEqual(report['status'],'READY')
                self.assertEqual(read.call_count,1)
                submit.assert_not_called()
            self.assertEqual(report['account_evidence']['completeness'],'COMPLETE')
            self.assertEqual(report['allocation_limit'],80.)
        finally:case.doCleanups()

    def test_paused_generation_skips_recovery_and_refreshes_account(self):
        from test_manual_leader_copy import ManualCopyWorkerTests
        case=ManualCopyWorkerTests();case.setUp()
        try:
            case.worker.service.stop(case.account,case.client)
            case.worker.service.start(case.account,case.client,baseline=case.worker.start_baseline(
                case.account,case.client,case.f.SOURCE_A))
            case.worker.service.stop(case.account,case.client)
            with patch('core.manual_copy_worker.recover_pending_manual_leader',
                       side_effect=AssertionError('paused generation must not recover')) as recover, \
                 patch.object(case.client,'submit_copy_ioc') as submit:
                report=case.cycle()
            self.assertEqual(report['status'],'PAUSED')
            recover.assert_not_called();submit.assert_not_called()
            self.assertEqual(report['account_evidence']['completeness'],'COMPLETE')
        finally:case.doCleanups()

    def test_scan_health_is_not_trade_or_promotion_frequency(self):
        import test_product
        case=test_product.ProductTests();case.setUp()
        try:
            b,_,_,view=case.setup_view();worker=case.case.worker;now=b.clock()
            for name in ('discovery','watchlist','leader_detection'):
                worker.health_observation(name,now,details={'scanned':6,'watched':6})
            worker.health_observation('deep_analysis',now,error='HISTORY_INCOMPLETE')
            result=view.discovery()
            self.assertEqual(result['components']['watchlist']['status'],'ACTIVE')
            self.assertEqual(result['components']['leader_detection']['status'],'ACTIVE')
            self.assertEqual(result['components']['deep_analysis']['status'],'DEGRADED')
            self.assertIsNone(result['components']['deep_analysis']['last_success_ms'])
            self.assertTrue(result['components']['deep_analysis']['worker_active'])
            view.clock=lambda:now+120001
            self.assertEqual(view.discovery()['components']['watchlist']['status'],'DEGRADED')
        finally:case.doCleanups()

    def test_idle_notification_loop_not_fake_successful_delivery(self):
        import asyncio,tempfile
        from unittest.mock import AsyncMock
        from core.product_events import ProductEvents
        from core.foundation.contracts import Scope
        with tempfile.TemporaryDirectory() as root:
            events=ProductEvents(Path(root)/'events.sqlite',lambda:1000)
            scope=Scope(tenant='1',account='0x'+'1'*40,network='TESTNET');send=AsyncMock()
            asyncio.run(events.deliver(scope,send));send.assert_not_called()
            with events.store.transaction() as db:row=json.loads(db.execute('SELECT body FROM product_delivery_health').fetchone()[0])
            self.assertEqual(row['status'],'READY');self.assertNotIn('last_success_ms',row)

    def test_unexecutable_manual_delta_never_prepares_or_submits(self):
        from core.manual_leader_copy import execute_manual_leader
        for margin in (0., .00000001, .01, 100.):
            with self.subTest(margin=margin):
                case=engine_fixture.EngineSafetyTests();case.setUp()
                try:
                    with patch.object(case.engine.journal,'prepare',wraps=case.engine.journal.prepare) as prepare, \
                         patch.object(case.client,'submit_copy_ioc') as submit:
                        result=execute_manual_leader(engine=case.engine,
                            account={'_tenant':'1','address':engine_fixture.ACCOUNT},client=case.client,
                            operation=None,event_id='tiny-'+str(margin),action='OPEN',leader_margin=margin,
                            leader_capital=100000.,allocatable_capital=100.,current_margin=0.,
                            spec={'leader':engine_fixture.SOURCE_A,'allocation_pct':80.,'coin':'BTC',
                                  'side':'LONG','leverage':5,'market_type':'CRYPTO'})
                        self.assertIn(result['status'],('NO_CHANGE','BELOW_EXECUTABLE_MINIMUM'))
                        prepare.assert_not_called();submit.assert_not_called()
                        self.assertFalse(case.engine.journal.pending(engine_fixture.ACCOUNT))
                finally:case.doCleanups()

    def test_existing_nonexecution_resolution_preserves_audit(self):
        case=engine_fixture.EngineSafetyTests();case.setUp()
        try:
            oid=case.engine.journal.prepare(engine_fixture.ACCOUNT,'0G|',{'action':'MANUAL_LEADER_OPEN','network':'TESTNET','before':None})
            case.engine.journal.finish(oid,{'ok':True,'action':'NOT_EXECUTED','resolution':'RECONCILED_NO_SUBMISSION'})
            with case.engine.journal.connect() as db:
                row=db.execute('SELECT status,intent,outcome FROM operations WHERE id=?',(oid,)).fetchone()
            self.assertEqual(row['status'],'CONFIRMED');self.assertIn('NOT_EXECUTED',row['outcome'])
            self.assertIn('MANUAL_LEADER_OPEN',row['intent'])
            self.assertFalse(case.engine.journal.owned(engine_fixture.ACCOUNT))
            self.assertFalse(case.engine.journal.pending(engine_fixture.ACCOUNT))
        finally:case.doCleanups()

    def test_idle_readiness_requires_fresh_worker_not_fake_agent_outputs(self):
        import test_product
        case=test_product.ProductTests();case.setUp()
        try:
            backend,record,_,view=case.setup_view()
            with case.case.worker.store.transaction() as db:
                tail=db.execute('SELECT MAX(rowid) FROM intelligence_records').fetchone()[0]
            backend.jobs.advance(tail);backend.drain(case.case.worker)
            snapshot=view.snapshot();agents=snapshot['runtimes'][0]['agents']
            self.assertEqual([a['status'] for a in agents],['READY']*7)
            self.assertTrue(all(a['result'] is None and a['last_success_ms'] is None for a in agents))
            for component in ('agents','risk','consensus','reconciliation'):
                self.assertEqual(snapshot['health']['components'][component]['status'],'READY')
            now=backend.clock();view.clock=lambda:now+90001
            self.assertTrue(all(a['status']!='READY' for a in view.snapshot()['runtimes'][0]['agents']))
        finally:case.doCleanups()

    def test_hundred_dollar_paper_shadow_outcomes_and_restart(self):
        import test_intelligence
        from core.autonomous import load_paper_backend
        case=test_intelligence.IntelligenceTests();case.setUp()
        try:
            template,record=case.paper_backend()
            for mode in ('PAPER_AUTO','SHADOW'):
                with self.subTest(mode=mode):
                    config={'allocation':dict(template.allocation_policy.model_dump(mode='json'),
                        allocation_limit=100.,other_allocation_limits=[],entry_fraction=.1,max_position_margin=25.,max_leverage=5),
                        'authorization':dict(template.auth_policy.model_dump(mode='json'),mode=mode),
                        'risk':template.risk.policy.model_dump(mode='json'),'initial_paper_equity':100.}
                    path=Path(case.temp.name)/(mode+'.json');path.write_text(json.dumps(config))
                    directory=Path(case.temp.name)/(mode+'-isolated')
                    backend=load_paper_backend(path,directory,'TESTNET',template.clock)
                    self.assertEqual(backend.store.portfolio(backend.auth_policy.scope).equity,100.)
                    opened=backend.process(record);self.assertEqual(opened['episode']['state'],'OPEN')
                    closed=backend.process(case.followup(record,'CLOSE',1,0))
                    self.assertEqual(closed['episode']['state'],'CLOSED')
                    with backend.store.transaction() as db:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_outcomes').fetchone()[0],1)
                        self.assertGreater(db.execute('SELECT COUNT(*) FROM calibration_records').fetchone()[0],0)
                    equity=backend.store.portfolio(backend.auth_policy.scope).equity
                    restarted=load_paper_backend(path,directory,'TESTNET',template.clock)
                    self.assertEqual(restarted.store.portfolio(restarted.auth_policy.scope).equity,equity)
                    if mode=='SHADOW':self.assertEqual(backend.exchange.calls,0)
        finally:case.doCleanups()
