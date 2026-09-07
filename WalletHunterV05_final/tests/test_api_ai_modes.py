"""Isolated mode controls; preferences never submit financial orders."""
import unittest
from unittest.mock import patch
import test_api_safety as fixtures
from core.ai_review import account_guard
from core.ai_modes import AiModes
from scripts.enable_ai_modes import enable


class ApiAiModesTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ApiSafetyTests('test_dashboard_isolates_user_account_events_and_settings')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.server,self.store=self.fixture.server,self.fixture.store
        self.request=self.fixture.request
        _,p=self.store.profile(1)
        p.update(leaders=fixtures.LEADERS[:2],ai_slot_selected=True)
        self.store.update_profile(1,p)

    def test_two_switches_are_independent_and_preserve_financial_settings(self):
        original=self.store.profile(1)[1]
        preserved={k:original.get(k) for k in ('copy_enabled','account','leaders','runtime','max_leverage')}
        self.assertEqual(self.request('POST','/api/ai/modes',body={'mode':'trader','enabled':True}).status_code,200)
        self.assertEqual(self.request('POST','/api/ai/modes',body={'mode':'rescue','enabled':False}).status_code,200)
        p=self.store.profile(1)[1]
        self.assertTrue(p['ai_trader_enabled']);self.assertFalse(p['ai_review_enabled'])
        self.assertEqual({k:p.get(k) for k in preserved},preserved)
        self.assertFalse(self.store.profile(2)[1].get('ai_trader_enabled',False))
        self.assertEqual(self.fixture.accounts[fixtures.ACCOUNT_A].calls,[])
        modes=self.request('GET','/api/ai').json()['modes']
        self.assertEqual(modes['trader']['execution_mode'],'PAPER')
        self.assertFalse(modes['trader']['real_execution_available'])
        self.assertFalse(modes['rescue']['real_execution_available'])

    def test_three_wallets_or_missing_ai_slot_prevent_trader_enable(self):
        for wallets,selected in ((fixtures.LEADERS[:3],True),(fixtures.LEADERS[:2],False)):
            _,p=self.store.profile(1);p.update(leaders=wallets,ai_slot_selected=selected)
            self.store.update_profile(1,p)
            response=self.request('POST','/api/ai/modes',body={'mode':'trader','enabled':True})
            self.assertEqual(response.status_code,409,response.text)
            self.assertFalse(self.store.profile(1)[1]['ai_trader_enabled'])
            self.assertEqual(self.request('POST','/api/ai/modes',body={'mode':'rescue','enabled':True}).status_code,200)

    def test_live_promotion_and_foreign_fields_are_rejected(self):
        for extra in ({'execution_mode':'LIVE'},{'user_id':2},{'copy_enabled':True},{'ai_live_enabled':True}):
            result=self.request('POST','/api/ai/modes',body={'mode':'trader','enabled':True,**extra})
            self.assertEqual(result.status_code,422,result.text)
        for enabled in ('true',1,None):
            self.assertEqual(self.request('POST','/api/ai/modes',body={'mode':'trader','enabled':enabled}).status_code,422)
        self.assertFalse(self.store.profile(1)[1]['ai_trader_enabled'])

    def test_busy_profile_blocks_preference_mutation(self):
        with account_guard(self.fixture.tmp,'telegram-profile:1'):
            result=self.request('POST','/api/ai/modes',body={'mode':'trader','enabled':True})
        self.assertEqual(result.status_code,409,result.text)
        self.assertFalse(self.store.profile(1)[1]['ai_trader_enabled'])

    def test_paper_books_are_private_by_user_and_account(self):
        self.server.ai_modes.paper.tick(1,fixtures.ACCOUNT_A,100,[],1788714000000)
        self.server.ai_modes.paper.tick(2,fixtures.ACCOUNT_B,200,[],1788714000000)
        first=self.request('GET','/api/ai').json()['modes']['trader']['paper']
        second=self.request('GET','/api/ai',uid=2).json()['modes']['trader']['paper']
        self.assertEqual(first['budget_usdc'],100)
        self.assertEqual(second['budget_usdc'],200)
        restarted=AiModes(self.fixture.tmp).summary(1,self.store.profile(1)[1])
        self.assertEqual(restarted['trader']['paper']['budget_usdc'],100)
        _,p=self.store.profile(1);p['account']=None;self.store.update_profile(1,p)
        self.assertEqual(self.request('GET','/api/ai').json()['modes']['trader']['paper']['positions'],[])

    def test_enable_script_dry_run_and_apply_are_paper_only(self):
        _,p=self.store.profile(1);p['copy_enabled']=False;self.store.update_profile(1,p)
        before=self.store.load()
        self.assertFalse(enable(self.fixture.tmp,self.store,1)['applied'])
        self.assertEqual(self.store.load(),before)
        outcome=enable(self.fixture.tmp,self.store,1,True)
        self.assertTrue(outcome['applied']);self.assertFalse(outcome['autonomous_real_orders'])
        after=self.store.profile(1)[1]
        self.assertTrue(after['ai_trader_enabled']);self.assertTrue(after['ai_review_enabled'])
        self.assertFalse(after['copy_enabled'])
        self.assertEqual(after['account'],before['profiles']['1']['account'])
        self.assertEqual(self.fixture.accounts[fixtures.ACCOUNT_A].calls,[])

    def test_shared_learning_export_is_authenticated_and_does_not_touch_accounts(self):
        self.server.ai_learning.learner.summary = lambda now_ms=None: {
            'status':'COLLECTING','counts':{'candles':77},'model':None,'predictions':[]}
        first=self.request('GET','/api/ai',uid=1).json()['learning']
        second=self.request('GET','/api/ai',uid=2).json()['learning']
        self.assertEqual(first,second)
        self.assertFalse(first['real_execution_available'])
        self.assertEqual(first['counts']['candles'],77)
        self.assertNotIn(fixtures.ACCOUNT_A,str(first))
        self.assertNotIn(fixtures.ACCOUNT_B,str(first))
        self.assertEqual(self.fixture.accounts[fixtures.ACCOUNT_A].calls,[])
        self.assertEqual(self.request('GET','/api/ai',data='').status_code,401)
