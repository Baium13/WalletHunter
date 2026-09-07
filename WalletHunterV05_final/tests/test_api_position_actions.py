"""Authenticated existing-position forms; all clients/decisions mocked."""
import unittest
from unittest.mock import Mock, patch
import test_api_safety as fixtures
from core.ai_review import account_guard


class ApiPositionActionsTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ApiSafetyTests('test_dashboard_isolates_user_account_events_and_settings')
        self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        self.server,self.store,self.request=self.fixture.server,self.fixture.store,self.fixture.request
        self.service=Mock()
        self.service.summary.return_value={'pending':[],'history':[],'holds':[],'availability':[]}
        self.service.prepare.return_value=self.service.summary.return_value
        self.service.decide.return_value={'status':'DECLINED'}
        self.service.release.return_value={'status':'RELEASED'}
        self.service.reserved_markets.return_value={}
        self.fixture.stack.enter_context(patch.object(self.server,'ai_position_actions',self.service))
        self.public=self.fixture.stack.enter_context(patch.object(self.server,'public_account_for',return_value=object()))

    def test_authentication_and_immutable_body(self):
        for url,body in (('/api/ai/positions/prepare',{}),('/api/ai/positions/abc/decision',{'confirm':True}),
                         ('/api/ai/positions/resume',{'market':'ETH|'})):
            self.assertEqual(self.request('POST',url,body=body,data='').status_code,401)
            self.assertEqual(self.request('POST',url,body={**body,'user_id':2}).status_code,422)
        for v in ('true',1,0,None):
            self.assertEqual(self.request('POST','/api/ai/positions/abc/decision',body={'confirm':v}).status_code,422)
        for field in ('action','size','leverage','limit_price','source_wallet'):
            self.assertEqual(self.request('POST','/api/ai/positions/abc/decision',body={'confirm':True,field:100}).status_code,422)
        self.service.decide.assert_not_called()

    def test_prepare_is_readonly_even_with_copying_enabled(self):
        before=self.store.load()
        result=self.request('POST','/api/ai/positions/prepare',body={})
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(self.service.prepare.call_args.kwargs['force_refresh'])
        self.assertEqual(self.service.prepare.call_args.args[0],1)
        self.server.account_client_for.assert_not_called()
        self.assertEqual(self.store.load(),before)
        self.assertTrue(self.store.profile(1)[1]['copy_enabled'])

    def test_decline_never_reads_exchange_or_constructs_signer(self):
        result=self.request('POST','/api/ai/positions/abc/decision',body={'confirm':False})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.service.decide.call_args.args[:3],(1,'abc',False))
        self.assertIsNone(self.service.decide.call_args.args[4])
        self.public.assert_not_called();self.server.account_client_for.assert_not_called()

    def test_confirm_passes_lazy_factory_not_a_signing_client(self):
        result=self.request('POST','/api/ai/positions/abc/decision',body={'confirm':True})
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(callable(self.service.decide.call_args.args[5]))
        self.server.account_client_for.assert_not_called()

    def test_busy_account_blocks_prepare_and_decision(self):
        with account_guard(self.fixture.tmp,fixtures.ACCOUNT_A):
            for path,body in (('prepare',{}),('abc/decision',{'confirm':True})):
                self.assertEqual(self.request('POST','/api/ai/positions/'+path,body=body).status_code,409)
        self.service.prepare.assert_not_called();self.service.decide.assert_not_called()

    def test_foreign_user_id_cannot_be_selected_by_body(self):
        self.assertEqual(self.request('POST','/api/ai/positions/abc/decision',uid=2,body={'confirm':False}).status_code,200)
        args=self.service.decide.call_args.args
        self.assertEqual(args[0],2);self.assertEqual(args[3]['account']['address'],fixtures.ACCOUNT_B)

    def test_resume_uses_public_reconciliation_never_signer(self):
        result=self.request('POST','/api/ai/positions/resume',body={'market':'ETH|'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.service.release.call_args.args[:2],(1,'ETH|'))
        self.server.account_client_for.assert_not_called()

    def test_unknown_outcome_is_sanitized_and_never_retried(self):
        self.service.decide.side_effect=RuntimeError('private response must not leak')
        result=self.request('POST','/api/ai/positions/abc/decision',body={'confirm':True})
        self.assertEqual(result.status_code,503);self.assertNotIn('private response',result.text)
        self.assertEqual(self.service.decide.call_count,1)

