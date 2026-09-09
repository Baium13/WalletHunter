"""Actual preview function + account/leader bridge, with fake exchange reads."""
import ast
import math
import re
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from fastapi import HTTPException
from core.manual_copy_worker import ManualCopyWorker
from core.hl_budget import BudgetUnavailable,origin


class ManualPreviewTests(unittest.TestCase):
    def setUp(self):
        from test_manual_leader_copy import ManualCopyWorkerTests
        self.fixture=ManualCopyWorkerTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        with f.case.engine.journal.connect() as db:
            db.execute('DELETE FROM manual_leader_configs');db.commit()
        tree=ast.parse((Path(__file__).parents[1]/'webapp/server.py').read_text(encoding='utf8'))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='preview_manual_copy')
        fn.decorator_list=[]
        self.analysis=Mock(side_effect=HTTPException(503,{'code':'HISTORY_INCOMPLETE'}))
        globals_={'ManualCopyInput':SimpleNamespace,'Header':lambda **kw:None,'HTTPException':HTTPException,
            'require_user':lambda _:dict(id=1),'storage':f.case.store,
            'manual_leader_account':lambda *args:(f.account,f.client),'ManualCopyWorker':ManualCopyWorker,
            'engine':f.case.engine,'reader':f.reader,'analysis_cache':{},'analyse_for_user':self.analysis,
            're':re,'math':math,'time':time}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<actual preview>','exec'),globals_)
        self.call=lambda:globals_['preview_manual_copy'](SimpleNamespace(leader=f.f.SOURCE_B,allocation_pct=80))

    def test_off_preview_gets_fresh_data_without_history_or_start(self):
        f=self.fixture;f.reads.clear();calls=len(f.client.calls)
        result=self.call()
        self.assertEqual(result['analysis_status'],'PENDING');self.assertIsNone(result['analysis'])
        self.analysis.assert_not_called()
        self.assertEqual(result['account_balance'],100.)
        self.assertEqual(result['allocation_limit'],80.)
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(len(f.client.calls),calls)
        self.assertEqual(f.reads,[f.f.SOURCE_B,f.f.SOURCE_B])
        self.assertIsNone(f.worker.service.config(f.account,f.client))
        with f.case.engine.journal.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM grants').fetchone()[0],0)

    def test_on_demand_reads_stay_governed_p2(self):
        original=self.fixture.reader.state
        def checked(*args):
            self.assertEqual(origin(),('manual_preview.current_evidence',2));return original(*args)
        with patch.object(self.fixture.reader,'state',side_effect=checked):self.call()

    def test_budget_cause_survives_canonical_optional_capacity(self):
        with patch.object(self.fixture.client,'capital_snapshot',side_effect=BudgetUnavailable('HL_API_BUDGET_DEFERRED')):
            with self.assertRaises(HTTPException) as error:self.call()
        self.assertEqual(error.exception.detail['code'],'API_BUDGET_CONSTRAINED')
        self.assertEqual(error.exception.detail['stage'],'FOLLOWER_ACCOUNT')
        self.assertEqual(error.exception.status_code,503)

    def test_stale_leader_and_network_remain_fail_closed(self):
        self.fixture.stale=True
        with self.assertRaises(HTTPException) as error:self.call()
        self.assertEqual(error.exception.detail['code'],'LEADER_DATA_STALE')
        self.fixture.stale=False;self.fixture.reader.network='MAINNET'
        with self.assertRaises(HTTPException) as error:self.call()
        self.assertEqual(error.exception.detail['code'],'NETWORK_MISMATCH')

    def test_no_secret_in_unexpected_error(self):
        with patch.object(self.fixture.reader,'state',side_effect=RuntimeError('secret-value')):
            with self.assertRaises(HTTPException) as error:self.call()
        self.assertNotIn('secret-value',str(error.exception.detail))

class ManualStartTests(unittest.TestCase):
    def setUp(self):
        from test_manual_leader_copy import ManualCopyWorkerTests
        from contextlib import nullcontext
        self.f=ManualCopyWorkerTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f
        f.worker.service.stop(f.account,f.client)
        self.before=f.worker.service.config(f.account,f.client)
        tree=ast.parse((Path(__file__).parents[1]/'webapp/server.py').read_text(encoding='utf8'))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='configure_manual_copy')
        fn.decorator_list=[]
        self.exchange_reads=[]
        def account(*args,read_exchange=True):
            self.exchange_reads.append(read_exchange)
            if read_exchange:self.assertEqual(origin(),('manual_start.current_evidence',2))
            return f.account,f.client
        self.g={'ManualCopyInput':SimpleNamespace,'Header':lambda **kw:None,'HTTPException':HTTPException,
            'require_user':lambda _:dict(id=1),'storage':f.case.store,'manual_leader_account':account,
            'ManualCopyWorker':ManualCopyWorker,'engine':f.case.engine,'reader':f.reader,
            'manual_leader_controller':lambda:f.worker.service,'account_guard':lambda *a:nullcontext(),
            'ROOT':Path('.'),'re':re}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<actual configure>','exec'),self.g)
        self.calls=len(f.client.calls);f.reads.clear()

    def call(self,action='start',leader=None,allocation=85):
        return self.g['configure_manual_copy'](SimpleNamespace(action=action,leader=leader or self.f.f.SOURCE_B,allocation_pct=allocation))

    def unchanged(self):
        self.assertEqual(self.f.worker.service.config(self.f.account,self.f.client),self.before)
        self.assertEqual(len(self.f.client.calls),self.calls)

    def test_add_wallet_is_only_draft_no_api_no_generation(self):
        result=self.call(None)
        self.assertFalse(result['enabled']);self.assertIsNone(result['generation_id'])
        self.assertEqual(result['leader'],self.f.f.SOURCE_B)
        self.assertEqual(self.exchange_reads,[False]);self.assertEqual(self.f.reads,[])
        self.assertEqual(len(self.f.client.calls),self.calls)

    def test_start_commits_new_leader_allocation_generation_together(self):
        result=self.call()
        self.assertTrue(result['enabled']);self.assertEqual(result['allocation_pct'],85)
        self.assertNotEqual(result['generation_id'],self.before.generation_id)
        self.assertEqual(self.exchange_reads,[False,True])
        self.assertEqual(self.f.reads,[self.f.f.SOURCE_B]*2)
        self.assertEqual(len(self.f.client.calls),self.calls)
        config=self.f.worker.service.config(self.f.account,self.f.client)
        self.assertEqual(config.start_evidence['wallet'],result['leader'])
        self.assertGreater(config.watermark_ms,0)

    def test_budget_rejection_is_503_preserves_whole_configuration(self):
        with patch.object(self.f.reader,'state',side_effect=BudgetUnavailable('HL_API_BUDGET_DEFERRED')):
            with self.assertRaises(HTTPException) as error:self.call()
        self.assertEqual(error.exception.status_code,503)
        self.assertEqual(error.exception.detail['code'],'API_BUDGET_CONSTRAINED')
        self.unchanged()

    def test_stale_failure_does_not_save_new_wallet_or_allocation(self):
        self.f.stale=True
        with self.assertRaises(HTTPException):self.call()
        self.unchanged()

    def test_network_mismatch_prevents_generation_and_mutation(self):
        self.f.reader.network='MAINNET'
        with self.assertRaises(HTTPException):self.call()
        self.unchanged()

    def test_invalid_allocation_rolls_back_start(self):
        with self.assertRaises(HTTPException):self.call(allocation=float('nan'))
        self.unchanged()

    def test_response_loss_then_second_explicit_start_cannot_replace_active_generation(self):
        first=self.call()
        with self.assertRaises(HTTPException) as error:self.call()
        self.assertEqual(error.exception.detail['code'],'MANUAL_COPY_ALREADY_ACTIVE')
        self.assertEqual(self.f.worker.service.config(self.f.account,self.f.client).generation_id,first['generation_id'])

    def test_stop_uses_no_api_and_does_not_apply_payload_leader(self):
        self.call('stop')
        self.assertEqual(self.exchange_reads,[False]);self.assertEqual(self.f.reads,[])
        self.assertEqual(self.f.worker.service.config(self.f.account,self.f.client).leader,self.before.leader)

    def test_initial_account_identity_has_no_sdk_construction(self):
        tree=ast.parse((Path(__file__).parents[1]/'webapp/server.py').read_text(encoding='utf8'))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='manual_leader_account')
        constructor=Mock(side_effect=AssertionError('No SDK for configuration'))
        g={'HTTPException':HTTPException,'settings':SimpleNamespace(hl_mode='TESTNET'),'HyperliquidAccount':constructor}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<actual account>','exec'),g)
        scoped,client=g['manual_leader_account'](1,{'account':{'address':self.f.f.ACCOUNT,'private_key':'secret'}},read_exchange=False)
        self.assertEqual(scoped,dict(address=self.f.f.ACCOUNT,_tenant='1'))
        self.assertEqual(client.network,'TESTNET');constructor.assert_not_called()

    def test_pending_check_is_repeated_under_write_transaction(self):
        from core.execution_quarantine import require_unblocked
        calls=[]
        def checked(db,scope):
            calls.append(db.in_transaction);return require_unblocked(db,scope)
        with patch('core.execution_quarantine.require_unblocked',side_effect=checked):self.call()
        self.assertIn(True,calls)

    def test_unexpected_error_does_not_leak_secret_or_save_draft(self):
        with patch.object(self.f.reader,'state',side_effect=RuntimeError('secret-key-value')):
            with self.assertRaises(HTTPException) as error:self.call()
        self.assertNotIn('secret-key-value',str(error.exception.detail));self.unchanged()


if __name__=='__main__':unittest.main()
