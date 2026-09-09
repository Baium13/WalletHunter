import json
import sqlite3
import time
import unittest
from unittest.mock import patch
from core.manual_copy_reset import abandon, history, REASON, STATE
from core.foundation.contracts import OrderIntent


class ResetTests(unittest.TestCase):
    def setUp(self):
        from test_execution_quarantine import QuarantineTests
        self.q=QuarantineTests();self.q.setUp();self.addCleanup(self.q.doCleanups)
        self.f=self.q.f;self.path=self.q.path;self.scope=self.q.scope
        self.now=int(time.time()*1000)
        from core.foundation.data import copy_account_snapshot
        p=copy_account_snapshot(self.f.client,self.scope,1,lambda:self.now,'')
        self.now=int(time.time()*1000)
        self.evidence=dict(intent_id=self.q.id,scope=self.scope.model_dump(mode='json'),
            portfolio=p.model_dump(mode='json'),checked_ms=self.now,
            cloid_result={'status':'unknownOid'},open_orders={'':[],'xyz':[]},recent_orders=[],fills=[])

    def reset(self,**kw):
        return abandon(self.path,self.q.id,operator=self.scope.tenant,operator_requested_abandonment=True,
                       evidence=kw.get('evidence',self.evidence),now_ms=self.now,reason=REASON)

    def test_archive_unknown_release_audit_and_effective_capital(self):
        a=self.reset()
        self.assertGreater(a['released_margin'],0)
        self.assertEqual(a['financial_outcome'],'UNKNOWN')
        self.assertEqual(a['capital_administration'],'RELEASED_BY_OPERATOR')
        from core.execution_quarantine import active_in,read_only_deployment_gate
        with sqlite3.connect(self.path) as db:
            after=list(db.execute('SELECT * FROM intents'))[0]
            before=self.q.before[0]
            self.assertEqual(after[:3],before[:3]);self.assertEqual(after[4:],before[4:])
            self.assertEqual(after[3],STATE)
            self.assertEqual(active_in(db,self.scope),[])
            self.assertTrue(read_only_deployment_gate(db,{})['read_only_deployment_safe'])
        self.assertFalse(self.f.case.engine.journal.pending(self.scope.account))
        self.assertIsNone(self.f.worker.service.config(self.f.account,self.f.client))
        self.assertIsNone(self.f.cycle())
        from pathlib import Path
        from core.product_read import ProductReadModel
        m=ProductReadModel(Path(self.path).parent.parent,self.scope).snapshot()['manual_copy']
        self.assertEqual(m['status'],'OFF');self.assertEqual(m['allocation_pct'],0)
        self.assertIsNone(m['selected_leader']);self.assertEqual(m['reserved'],0.)
        self.assertEqual(m['archived_operations'][0]['financial_outcome'],'UNKNOWN')

    def test_tombstones_prevent_requeue_grants_and_audit_edits(self):
        a=self.reset()
        with sqlite3.connect(self.path) as db:
            for sql,args in [("UPDATE intents SET status='UNKNOWN' WHERE id=?",(self.q.id,)),
                ("DELETE FROM intents WHERE id=?",(self.q.id,)),
                ("UPDATE operations SET status='PREPARED' WHERE id=?",(a['parent_id'],)),
                ("DELETE FROM manual_copy_abandonments WHERE intent_id=?",(self.q.id,)),
                ("INSERT INTO grants VALUES(?,?,'hash')",(self.q.id,'scope'))]:
                with self.assertRaises(sqlite3.IntegrityError):db.execute(sql,args)
        from core.manual_leader_copy import recover_pending_manual_leader
        with patch.object(self.f.client,'submit_copy_ioc') as submit,patch.object(self.f.client,'query_order_by_cloid') as query:
            self.assertFalse(recover_pending_manual_leader(self.f.case.engine,self.f.account,self.f.client))
        submit.assert_not_called();query.assert_not_called()
        from core.foundation.store import Store
        from core.foundation.execution import ExecutionGateway
        from core.foundation.copy_execution import HyperliquidExecutionAdapter
        from core.foundation.risk import RiskGateway,RiskPolicy
        store=Store(self.path)
        i=OrderIntent.model_validate_json(self.q.before[0][2])
        adapter=HyperliquidExecutionAdapter(self.f.client,self.scope,lambda:self.now,store.portfolio(self.scope))
        with sqlite3.connect(self.path) as db:
            policy=RiskPolicy.model_validate_json(db.execute('SELECT body FROM policies').fetchone()[0])
        gateway=ExecutionGateway(store,RiskGateway(policy),adapter,lambda:self.now)
        with patch.object(adapter,'query') as query,patch.object(adapter,'submit') as submit:
            self.assertEqual(gateway.recover(i).status,'UNKNOWN')
            self.assertEqual(gateway.execute(i,None).status,'UNKNOWN')
            with self.assertRaises(ValueError):gateway.authorize_copy(i)
        submit.assert_not_called();query.assert_not_called()

    def test_operator_authority_and_audit_cannot_be_inferred(self):
        with self.assertRaisesRegex(ValueError,'EXPLICIT_OPERATOR'):
            abandon(self.path,self.q.id,operator=self.scope.tenant,operator_requested_abandonment=False,
                evidence=self.evidence,now_ms=self.now,reason=REASON)
        with self.assertRaises(ValueError):
            self.reset(evidence=dict(self.evidence,portfolio=dict(self.evidence['portfolio'],equity=None)))
        self.assertEqual(self.f.worker.service.config(self.f.account,self.f.client).leader,self.f.f.SOURCE_A)

    def test_wrong_operator_stale_orders_fills_and_grants_fail_closed(self):
        with self.assertRaisesRegex(ValueError,'OPERATOR_SCOPE'):
            abandon(self.path,self.q.id,operator='other',operator_requested_abandonment=True,
                evidence=self.evidence,now_ms=self.now,reason=REASON)
        for change in [{'checked_ms':0},{'fills':[{}]},{'recent_orders':[{}]},
                       {'open_orders':{'':[{}],'xyz':[]}},{'cloid_result':{'status':'order'}}]:
            with self.assertRaises(ValueError):self.reset(evidence=dict(self.evidence,**change))
        from core.foundation.store import scope_key
        with sqlite3.connect(self.path) as db:db.execute("INSERT INTO grants VALUES('other',?,'h')",(scope_key(self.scope),))
        with self.assertRaisesRegex(ValueError,'GRANT'):self.reset()

    def test_reset_new_wallet_real_service_e2e_and_restart(self):
        audit=self.reset();s=self.f.worker.service;c=self.f.client;account=self.f.account
        source=self.f.f.SOURCE_B
        self.f.leaders[source]={'BTC':(10000.,'LONG',5)}
        draft=s.configure(account,c,source,80.)
        self.assertFalse(draft.enabled);self.assertIsNone(draft.generation_id)
        calls=len(c.calls);self.assertEqual(self.f.cycle()['status'],'READY');self.assertEqual(calls,len(c.calls))
        with self.assertRaisesRegex(ValueError,'BASELINE'):s.start(account,c)
        fresh=s.start(account,c,baseline=self.f.worker.start_baseline(account,c,source))
        self.assertIsNotNone(fresh.generation_id)
        self.assertNotEqual(fresh.generation_id,audit['parent_id'])
        self.assertEqual(s.process(account,c,event_id='old',action='OPEN',leader_margin=1.,leader_capital=100.,
                                  allocatable_capital=100.,event_ms=fresh.watermark_ms-1)['status'],'STALE_GENERATION_EVENT')
        self.f.assert_action('OPEN')
        self.f.leaders[source]['BTC']=(20000.,'LONG',5);self.f.assert_action('ADD')
        self.f.leaders[source]['BTC']=(5000.,'LONG',5);self.f.assert_action('REDUCE')
        self.f.leaders[source]['BTC']=(10000.,'SHORT',5);self.f.assert_action('REVERSE')
        self.f.leaders[source]={};self.f.assert_action('CLOSE')
        from core.manual_copy_worker import ManualCopyWorker
        restarted=ManualCopyWorker(self.f.case.engine,self.f.reader)
        self.assertEqual(restarted.service.config(account,c).generation_id,fresh.generation_id)
        self.assertEqual(self.reset(),audit)  # Idempotent old reset cannot clear new wallet.
        self.assertEqual(restarted.service.config(account,c).leader,source)
        with sqlite3.connect(self.path) as db:
            parents=[json.loads(r[0]) for r in db.execute("SELECT intent FROM operations WHERE status='CONFIRMED'")]
            self.assertTrue(parents);self.assertTrue(all(p['generation_id']==fresh.generation_id for p in parents))
            self.assertEqual(len(history(db,self.scope)),1)


if __name__=='__main__':unittest.main()
