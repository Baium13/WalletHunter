import json
import sqlite3
import unittest
from unittest.mock import patch
from core.execution_quarantine import quarantine,active_in,read_only_deployment_gate


class QuarantineTests(unittest.TestCase):
    def setUp(self):
        from test_manual_leader_copy import ManualCopyWorkerTests
        self.f=ManualCopyWorkerTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        with patch.object(self.f.client,'submit_copy_ioc',side_effect=TimeoutError('possible submission')):
            self.f.cycle()
        self.path=self.f.case.engine.journal.path
        self.scope=self.f.worker.service._scope(self.f.account,self.f.client)
        self.f.worker.service.stop(self.f.account,self.f.client)
        with sqlite3.connect(self.path) as db:
            self.id=db.execute('SELECT id FROM intents').fetchone()[0]
            self.before=list(db.execute('SELECT * FROM intents'))
        self.add()

    def add(self):return quarantine(self.path,self.id,evidence_sha256='a'*64,now_ms=123)

    def test_quarantine_is_overlay_reservation_and_identity_unchanged(self):
        with sqlite3.connect(self.path) as db:
            self.assertEqual(self.before,list(db.execute('SELECT * FROM intents')))
            q=active_in(db,self.scope)[0]
            self.assertGreater(q['reservation']['margin'],0)
            self.assertEqual(q['state'],'QUARANTINED_UNKNOWN')

    def test_restart_and_repeated_operator_request_preserve_original_audit(self):
        first=self.add();self.assertEqual(first,self.add())
        with sqlite3.connect(self.path) as db:self.assertEqual(first,active_in(db,self.scope))

    def test_resume_and_reconfigure_enabled_are_blocked(self):
        with self.assertRaisesRegex(ValueError,'QUARANTINED'):
            self.f.worker.service.start(self.f.account,self.f.client)
        with self.assertRaisesRegex(ValueError,'QUARANTINED'):
            self.f.worker.service._write(self.f.worker.service.config(self.f.account,self.f.client).model_copy(update={'enabled':True}))

    def test_paused_hold_monitor_never_queries_retry_queue_or_submits(self):
        with patch.object(self.f.client,'submit_copy_ioc') as submit,patch.object(self.f.client,'query_order_by_cloid') as query:
            report=self.f.cycle()
        self.assertEqual(report['status'],'PAUSED');self.assertEqual(report['recovery'],[])
        submit.assert_not_called();query.assert_not_called()

    def test_read_only_deployment_allowed_not_live(self):
        with sqlite3.connect(self.path) as db:
            result=read_only_deployment_gate(db,{'1':{'copy_enabled':False}})
        self.assertTrue(result['read_only_deployment_safe']);self.assertFalse(result['live_manual_copy_safe'])

    def test_new_grant_or_unclassified_parent_blocks_deployment(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO grants VALUES('new','scope','hash')")
            with self.assertRaisesRegex(ValueError,'LIVE_GRANT'):read_only_deployment_gate(db,{})
            db.rollback()
            db.execute("INSERT INTO operations(id,account,market,created,updated,status,intent) VALUES('other','account','ETH|',0,0,'PREPARED','{}')")
            with self.assertRaisesRegex(ValueError,'UNCLASSIFIED_PREPARED'):read_only_deployment_gate(db,{})

    def test_scope_isolation_and_paper_gateway_continue(self):
        with sqlite3.connect(self.path) as db:
            self.assertEqual(active_in(db,self.scope.model_copy(update={'network':'MAINNET'})),[])
            self.assertEqual(active_in(db,self.scope.model_copy(update={'tenant':'other'})),[])
        from test_product import ProductTests
        case=ProductTests();case.setUp();self.addCleanup(case.doCleanups)
        b,event,_,view=case.setup_view();b.process(event)
        self.assertTrue(view.snapshot()['runtimes'][0]['portfolio']['positions'])
        case=ProductTests();case.setUp();self.addCleanup(case.doCleanups)
        shadow,event,_,shadow_view=case.setup_view('SHADOW');shadow.process(event)
        self.assertEqual(shadow.exchange.calls,0)
        self.assertEqual(shadow_view.snapshot()['runtimes'][0]['runtime_mode'],'SHADOW')

    def test_read_model_quarantine_is_degraded_not_global_failure(self):
        from core.product_read import ProductReadModel
        from pathlib import Path
        view=ProductReadModel(Path(self.path).parent.parent,self.scope)
        s=view.snapshot()
        self.assertEqual(s['manual_copy']['quarantine']['count'],1)
        self.assertEqual(s['manual_copy']['valid_actions'],[])
        self.assertEqual(s['health']['components']['execution']['status'],'DEGRADED')
        self.assertNotEqual(s['health']['status'],'UNHEALTHY')
