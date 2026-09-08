"""Actual autonomous service/worker and independent fake transport crash tests."""
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
import test_intelligence as fixture
from core.autonomous import AutonomousBackend
from core.foundation.execution import FakeExchange
from core.foundation.store import Store,scope_key


class Crash(BaseException):pass


class DurabilityTests(unittest.TestCase):
    def setUp(self):
        self.case=fixture.IntelligenceTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
    def build(self,mode='PAPER_AUTO'):
        return self.case.paper_backend(mode)
    def restart(self,b):
        return AutonomousBackend(Store(b.store.path),FakeExchange(b.exchange.storage.path),b.allocation_policy,b.auth_policy,b.risk.policy,b.clock)
    def follow(self,r,action,before,after,side='SELL',suffix='next'):
        return self.case.followup(r,action,before,after,side,suffix)
    def count(self,b,table):
        with b.store.transaction() as db:return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    def outcome(self,b):
        with b.store.transaction() as db:return json.loads(db.execute('SELECT body FROM autonomous_outcomes ORDER BY rowid DESC').fetchone()[0])

    def test_crash_after_claim_recovers_same_event(self):
        b,r=self.build();original=b.jobs.claim
        def crash(*args):original(*args);raise Crash()
        with patch.object(b.jobs,'claim',side_effect=crash),self.assertRaises(Crash):b.process(r)
        restored=self.restart(b);restored.recover()
        self.assertEqual(restored.exchange.calls,1);self.assertEqual(self.count(b,'intents'),1)
    def test_crash_after_analysis_checkpoint(self):
        b,r=self.build()
        with patch.object(b.authorization,'decide',side_effect=Crash()),self.assertRaises(Crash):b.process(r)
        restored=self.restart(b);restored.recover();self.assertEqual(restored.exchange.calls,1)
        self.assertEqual(self.count(b,'autonomous_analysis'),1)
    def test_crash_after_authorization_before_decision_recovers(self):
        b,r=self.build();original=b.jobs.stage
        def crash(event,stage,*args):
            original(event,stage,*args)
            if stage=='AUTHORIZED':raise Crash()
        with patch.object(b.jobs,'stage',side_effect=crash),self.assertRaises(Crash):b.process(r)
        restored=self.restart(b);restored.clock=restored.gateway.clock=lambda:fixture.NOW+2000
        restored.recover();self.assertEqual(restored.exchange.calls,1);self.assertEqual(self.count(b,'authorization_requests'),1)
    def test_crash_after_decision_before_reservation(self):
        b,r=self.build()
        with patch.object(b.gateway,'authorize_paper',side_effect=Crash()),self.assertRaises(Crash):b.process(r)
        self.assertEqual(self.count(b,'intents'),0)
        restored=self.restart(b);restored.recover();self.assertEqual(restored.exchange.calls,1)
        restored.recover();self.assertEqual(restored.exchange.calls,1)
    def test_crash_after_reservation_before_possible_submit_is_query_only(self):
        b,r=self.build()
        with patch.object(FakeExchange,'submit',side_effect=Crash()),self.assertRaises(Crash):b.process(r)
        restored=self.restart(b);restored.recover();restored.recover()
        self.assertEqual(restored.exchange.calls,0)
        with b.store.transaction() as db:
            row=db.execute('SELECT status,reservation FROM intents').fetchone()
        self.assertEqual(row['status'],'UNKNOWN');self.assertGreater(json.loads(row['reservation'])['margin'],0)
    def test_crash_after_exchange_acceptance_before_receipt(self):
        b,r=self.build();original=FakeExchange.submit
        def crash(exchange,*args):original(exchange,*args);raise Crash()
        with patch.object(FakeExchange,'submit',crash),self.assertRaises(Crash):b.process(r)
        restored=self.restart(b);restored.recover()
        self.assertEqual(restored.exchange.calls,0);self.assertEqual(self.count(b,'intents'),1)
        self.assertEqual(restored.store.portfolio(b.auth_policy.scope).positions[0].evidence,'VERIFIED')
    def test_crash_after_receipt_before_episode(self):
        b,r=self.build()
        with patch.object(b.episodes,'sync',side_effect=Crash()),self.assertRaises(Crash):b.process(r)
        restored=self.restart(b);restored.recover()
        self.assertEqual(restored.exchange.calls,0)
        with b.store.transaction() as db:episode=json.loads(db.execute('SELECT body FROM position_episodes').fetchone()[0])
        self.assertEqual(episode['state'],'OPEN')
    def test_partial_recovery_commits_and_reserves_remainder(self):
        b,r=self.build();b.exchange.behavior='PARTIAL';result=b.process(r)
        restored=self.restart(b);restored.recover();restored.recover()
        self.assertEqual(restored.exchange.calls,0)
        p=restored.store.portfolio(b.auth_policy.scope).positions[0]
        with b.store.transaction() as db:row=db.execute('SELECT reservation,status FROM intents').fetchone()
        self.assertEqual(row['status'],'PARTIAL')
        self.assertAlmostEqual(json.loads(row['reservation'])['margin'],p.margin)
        self.assertEqual(self.count(b,'autonomous_outcomes'),0)
    def test_unknown_acceptance_recovers_without_order_retry(self):
        b,r=self.build();b.exchange.behavior='ACK_LOSS';self.assertEqual(b.process(r)['status'],'UNKNOWN')
        restored=self.restart(b);restored.recover()
        self.assertEqual(restored.exchange.calls,0)
        with b.store.transaction() as db:self.assertEqual(db.execute('SELECT status FROM intents').fetchone()[0],'FILLED')
    def test_risk_rejected_followups_preserve_active_episode_then_close(self):
        b,r=self.build();opened=b.process(r);old=b.risk.policy
        for action,before,after,side in [('ADD',1,2,'BUY'),('REDUCE',2,1,'SELL'),('CLOSE',1,0,'SELL')]:
            b.risk.policy=old.model_copy(update={'enabled':False})
            response=b.process(self.follow(r,action,before,after,side,action+'-rejected'))
            self.assertEqual(response['status'],'REJECTED',response)
            self.assertEqual(response['episode']['state'],'OPEN');self.assertEqual(self.count(b,'autonomous_outcomes'),0)
        b.risk.policy=old
        closed=b.process(self.follow(r,'CLOSE',1,0,suffix='valid-close'))
        self.assertEqual(closed['episode']['state'],'CLOSED')
        self.assertEqual(closed['episode']['episode_id'],opened['episode']['episode_id'])
    def test_close_receipt_crash_then_outcome_once(self):
        b,r=self.build();b.process(r);closing=self.follow(r,'CLOSE',1,0)
        with patch.object(b.episodes,'sync',side_effect=Crash()),self.assertRaises(Crash):b.process(closing)
        restored=self.restart(b);restored.recover();restored.recover()
        self.assertEqual(restored.exchange.calls,0);self.assertEqual(self.count(b,'autonomous_outcomes'),1)
        self.assertEqual(self.count(b,'calibration_records'),1)
    def test_crash_after_outcome_before_cursor_does_not_duplicate(self):
        b,r=self.build();b.process(r);closing=self.follow(r,'CLOSE',1,0)
        original=b.jobs.stage
        def crash(event,stage,*args):
            if stage=='COMPLETED':raise Crash()
            return original(event,stage,*args)
        with patch.object(b.jobs,'stage',side_effect=crash),self.assertRaises(Crash):b.process(closing)
        restored=self.restart(b);restored.recover();self.assertEqual(self.count(b,'autonomous_outcomes'),1)
    def test_actual_drain_crash_before_cursor_commit(self):
        b,r=self.build();b.process(r);closing=self.follow(r,'CLOSE',1,0)
        research=self.case.worker
        with research.store.transaction() as db:
            db.execute('INSERT INTO intelligence_records VALUES(?,?,?,?,?)',('closing','TESTNET','DECISION',fixture.NOW,json.dumps(closing)))
            seq=db.execute("SELECT rowid FROM intelligence_records WHERE id='closing'").fetchone()[0]
        original=b.jobs.advance
        def crash(cursor):
            if cursor==seq:raise Crash()
            original(cursor)
        with patch.object(b.jobs,'advance',side_effect=crash),self.assertRaises(Crash):b.drain(research)
        restored=self.restart(b);restored.drain(research)
        self.assertEqual(restored.exchange.calls,0);self.assertEqual(self.count(restored,'autonomous_outcomes'),1)
        with restored.store.transaction() as db:self.assertEqual(db.execute('SELECT seq FROM autonomous_cursors').fetchone()[0],seq)
    def test_crash_before_outcome_rolls_back_projection_not_fill(self):
        b,r=self.build();b.process(r)
        with patch('core.autonomous_outcomes.record_outcome',side_effect=Crash()),self.assertRaises(Crash):
            b.process(self.follow(r,'CLOSE',1,0))
        self.assertEqual(self.count(b,'autonomous_outcomes'),0)
        restored=self.restart(b);restored.recover()
        self.assertEqual(restored.exchange.calls,0);self.assertEqual(self.count(b,'autonomous_outcomes'),1)
    def test_poison_and_noise_do_not_starve_fresh_work(self):
        b,r=self.build();research=self.case.worker
        with research.store.transaction() as db:
            cursor=db.execute('SELECT MAX(rowid) FROM intelligence_records').fetchone()[0]
            db.execute('INSERT INTO intelligence_records VALUES(?,?,?,?,?)',('poison','TESTNET','DECISION',fixture.NOW,'{bad'))
            for i in range(500):db.execute('INSERT INTO intelligence_records VALUES(?,?,?,?,?)',(str(i),'TESTNET','NOISE',fixture.NOW,'{}'))
            db.execute('INSERT INTO intelligence_records VALUES(?,?,?,?,?)',('valid','TESTNET','DECISION',fixture.NOW,json.dumps(r)))
        with b.store.transaction() as db:db.execute('INSERT INTO autonomous_cursors VALUES(?,?)',(scope_key(b.auth_policy.scope),cursor))
        b.drain(research);self.assertEqual(b.exchange.calls,1);self.assertEqual(self.count(b,'autonomous_quarantine'),1)
        b.drain(research);self.assertEqual(b.exchange.calls,1)
    def test_stale_before_submission_expires_then_fresh_continues(self):
        b,r=self.build()
        with patch.object(b.gateway,'authorize_paper',side_effect=Crash()),self.assertRaises(Crash):b.process(r)
        b.clock=b.gateway.clock=lambda:fixture.NOW+100000
        b.recover();self.assertEqual(b.exchange.calls,0)
        with b.store.transaction() as db:body=json.loads(db.execute('SELECT body FROM autonomous_decisions').fetchone()[0])
        self.assertEqual(body['status'],'EXPIRED_BEFORE_SUBMISSION')
    def test_paper_costs_and_immutable_numerical_outcome(self):
        b,r=self.build();opened=b.process(r)
        closed=b.process(self.follow(r,'CLOSE',1,0));outcome=self.outcome(b)
        entry=opened['receipt']['fills'][0];exit=closed['receipt']['fills'][0]
        size=entry['size'];gross=(exit['price']-entry['price'])*size;fees=(entry['price']+exit['price'])*size*.0005
        self.assertAlmostEqual(outcome['gross_pnl'],gross);self.assertAlmostEqual(outcome['fees'],fees)
        self.assertAlmostEqual(outcome['net_pnl'],gross-fees);self.assertEqual(outcome['mode'],'PAPER')
        self.assertEqual(outcome['evidence'],'SIMULATED');self.assertEqual(outcome['exit_ms'],exit['exchange_ms'])
        with self.assertRaises(sqlite3.IntegrityError):
            with b.store.transaction() as db:db.execute("UPDATE autonomous_outcomes SET body='{}'")
    def test_shadow_numeric_outcome_and_zero_exchange_orders(self):
        b,r=self.build('SHADOW');opened=b.process(r);b.process(self.follow(r,'CLOSE',1,0))
        outcome=self.outcome(b);self.assertEqual(outcome['mode'],'SHADOW');self.assertEqual(outcome['evidence'],'HYPOTHETICAL')
        self.assertLess(outcome['net_pnl'],0);self.assertGreater(outcome['fees'],0)
        self.assertEqual(b.exchange.calls,0);self.assertEqual(self.count(b,'intents'),0)
        self.assertEqual(self.count(b,'hypothetical_executions'),2)
    def test_weighted_add_reduce_pnl_values(self):
        from core.autonomous_outcomes import calculate
        def execution(action,size,price,stamp):
            return {'intent_id':str(stamp),'action':action,'reference_price':price-1,
                'fills':[{'size':size,'price':price,'fee':size*price*.001,'side':'BUY' if action in {'OPEN','ADD'} else 'SELL','exchange_ms':stamp}]}
        rows=[execution('OPEN',2,100,1),execution('ADD',1,130,2),execution('REDUCE',1,120,3),execution('CLOSE',2,90,4)]
        out=calculate(rows,'LONG')
        self.assertEqual(out['gross_pnl'],-30.);self.assertAlmostEqual(out['fees'],.63);self.assertAlmostEqual(out['net_pnl'],-30.63)
        self.assertEqual(out['duration_ms'],3)
        win=calculate([execution('OPEN',.5,100,1),execution('REDUCE',.2,120,2),execution('CLOSE',.3,130,3)],'LONG')
        self.assertAlmostEqual(win['gross_pnl'],13.);self.assertAlmostEqual(win['net_pnl'],12.887)
    def test_live_missing_fees_remain_unknown_not_simulated(self):
        from core.autonomous_outcomes import calculate
        rows=[{'intent_id':'a','action':'OPEN','fills':[{'size':1.,'price':100.,'side':'BUY','exchange_ms':1}]},
              {'intent_id':'b','action':'CLOSE','fills':[{'size':1.,'price':110.,'side':'SELL','exchange_ms':2}]}]
        result=calculate(rows,'LONG');self.assertEqual(result['gross_pnl'],10);self.assertIsNone(result['net_pnl'])

    def test_reverse_rejected_opposite_keeps_old_episode_closed(self):
        b,r=self.build();first=b.process(r);original=FakeExchange.submit
        def close_then_block(exchange,intent,*args):
            result=original(exchange,intent,*args)
            if intent.action=='CLOSE':b.risk.policy=b.risk.policy.model_copy(update={'enabled':False})
            return result
        with patch.object(FakeExchange,'submit',close_then_block):
            response=b.process(self.follow(r,'REVERSE',1,-1))
        self.assertEqual(response['close']['episode']['state'],'CLOSED')
        self.assertNotEqual(response['open']['status'],'FILLED')
        self.assertFalse(b.store.portfolio(b.auth_policy.scope).positions)
        self.assertEqual(self.count(b,'autonomous_outcomes'),1)

    def test_demoted_leader_close_survives_action_rejection_and_restart(self):
        b,r=self.build();b.process(r)
        rejected=self.follow(r,'ADD',1,2,'BUY','demoted-add');rejected['admission_allowed']=False
        self.assertEqual(b.process(rejected)['status'],'LEADER_ADMISSION_HOLD')
        restored=self.restart(b);closing=self.follow(r,'CLOSE',1,0);closing['admission_allowed']=False
        self.assertEqual(restored.process(closing)['episode']['state'],'CLOSED')

    def test_confirmed_live_outcome_has_exchange_label_and_no_invented_fees(self):
        from core.foundation.copy_execution import HyperliquidExecutionAdapter,LiveReport
        from core.foundation.contracts import Fill,ExecutionReceipt
        from core.foundation.paper_effect import effect
        b,r,before=self.case.live_backend()
        def refresh(adapter,revision,dex=''):
            return b.store.portfolio(b.auth_policy.scope).model_copy(update={'revision':revision})
        def submit(adapter,intent,snapshot,now):
            fill=Fill(intent_id=intent.intent_id,instrument=intent.instrument,order_id=intent.intent_id,
                trade_id=intent.intent_id,side=intent.side,size=intent.size,price=intent.limit_price,exchange_ms=now)
            after=effect(intent,snapshot,(fill,),now)
            return LiveReport(ExecutionReceipt(intent_id=intent.intent_id,scope=intent.scope,status='FILLED',
                order_ids=(intent.intent_id,),fills=(fill,),reconciliation='CONFIRMED',received_ms=now,provenance='EXCHANGE'),after)
        with patch.object(HyperliquidExecutionAdapter,'refresh',refresh),patch.object(HyperliquidExecutionAdapter,'submit',submit):
            proposal=b.process(r);self.assertEqual(self.count(b,'intents'),0)
            b.confirm(proposal['authorization']['decision_id'],authenticated_user='7')
            close=b.process(self.follow(r,'CLOSE',1,0))
            b.confirm(close['authorization']['decision_id'],authenticated_user='7')
        outcome=self.outcome(b)
        self.assertEqual(outcome['mode'],'LIVE');self.assertEqual(outcome['evidence'],'EXCHANGE')
        self.assertIsNone(outcome['fees']);self.assertIsNone(outcome['net_pnl']);self.assertIsNone(outcome['cost_model'])

    def test_full_runtime_manifest_restore_preserves_unknown_and_outcome(self):
        import importlib.util
        from contextlib import redirect_stdout
        import io
        b,r=self.build();b.process(r);b.process(self.follow(r,'CLOSE',1,0))
        again=self.follow(r,'OPEN',0,1,'BUY','second-entry');b.exchange.behavior='ACK_LOSS'
        result=b.process(again);self.assertEqual(result['status'],'UNKNOWN')
        root=Path(self.case.temp.name);(root/'data').mkdir(exist_ok=True)
        (root/'data/state.json').write_text('{"profiles":{}}')
        manifest={'version':1,'artifacts':[
            {'path':str(b.store.path),'archive':'autonomy.store','kind':'sqlite','required':True},
            {'path':str(b.exchange.storage.path),'archive':'fake.store','kind':'sqlite','required':True},
            {'path':str(self.case.worker.store.path),'archive':'research.store','kind':'sqlite','required':True},
            {'path':'data/state.json','archive':'data/state.json','kind':'state','required':True}]}
        path=root/'manifest.json';path.write_text(json.dumps(manifest))
        source=Path(__file__).parents[1]/'scripts/backup_runtime.py'
        spec=importlib.util.spec_from_file_location('isolated_block2_backup',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.ROOT=root
        output=io.StringIO()
        with redirect_stdout(output):module.run(path)
        saved=json.loads(output.getvalue());restored_root=root/'restored'
        restored_manifest=module.restore(root/'backups'/saved['backup'],restored_root)
        self.assertTrue(restored_manifest['explicit_runtime_manifest'])
        restored=AutonomousBackend(Store(restored_root/'autonomy.store'),FakeExchange(restored_root/'fake.store'),
            b.allocation_policy,b.auth_policy,b.risk.policy,b.clock)
        restored.recover();self.assertEqual(restored.exchange.calls,0)
        self.assertEqual(self.count(restored,'autonomous_outcomes'),1)
        self.assertEqual(self.count(restored,'calibration_records'),1)
        self.assertEqual(len(restored.store.portfolio(b.auth_policy.scope).positions),1)
        with restored.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM intents WHERE id=?',(result['authorization']['decision_id'],)).fetchone()[0],'FILLED')
        with Store(restored_root/'research.store').transaction() as db:
            self.assertGreater(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],0)
