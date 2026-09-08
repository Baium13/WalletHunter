"""Offline runtime boundary tests; fixtures never load production state."""
import unittest
import json
from pathlib import Path
from unittest.mock import patch
import test_engine_safety as engine_fixture


class RuntimeInitializationTests(unittest.TestCase):
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
