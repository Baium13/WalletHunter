"""Capital availability is distinct from zero; no network or orders."""
from unittest.mock import patch
import unittest

import test_api_safety as fixtures
from core.capital_snapshot import UnsupportedCapitalMode


class ApiCapitalTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ApiSafetyTests('test_dashboard_isolates_user_account_events_and_settings')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.server,self.store=self.fixture.server,self.fixture.store
        self.request=self.fixture.request
        _,p=self.store.profile(1)
        p['leaders']=fixtures.LEADERS[:1]
        p['copy_enabled']=False
        self.store.update_profile(1,p)
        self.fixture.reader.rows[fixtures.ACCOUNT_A]=[fixtures.position()]

    def test_unsupported_mode_keeps_positions_and_null_balance_visible(self):
        with patch.object(self.fixture.reader,'balance',side_effect=UnsupportedCapitalMode('portfolioMargin')):
            response=self.request('GET','/api/dashboard')
        self.assertEqual(response.status_code,200,response.text)
        self.assertIsNone(response.json()['balance'])
        self.assertEqual(response.json()['balance_error'],'CAPITAL_MODE_UNSUPPORTED')
        self.assertEqual(response.json()['positions'][0]['coin'],'BTC')

    def test_balance_failure_is_not_a_zero_or_empty_portfolio(self):
        with patch.object(self.fixture.reader,'balance',side_effect=RuntimeError('offline')):
            response=self.request('GET','/api/dashboard')
        self.assertEqual(response.status_code,200,response.text)
        self.assertIsNone(response.json()['balance'])
        self.assertEqual(response.json()['balance_error'],'BALANCE_UNAVAILABLE')
        self.assertEqual(len(response.json()['positions']),1)

    def test_bad_or_unsupported_capital_cannot_enable_copy(self):
        for failure in (UnsupportedCapitalMode('portfolioMargin'),RuntimeError('offline')):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(self.fixture.reader,'balance',side_effect=failure):
                    response=self.request('POST','/api/copy',body={'value':True})
                self.assertEqual(response.status_code,503,response.text)
                self.assertFalse(self.store.profile(1)[1]['copy_enabled'])
                self.assertEqual(self.fixture.accounts[fixtures.ACCOUNT_A].calls,[])

    def test_zero_nonfinite_and_bad_source_capital_cannot_enable_copy(self):
        for value in (0,float('nan'),float('inf')):
            with self.subTest(value=value):
                with patch.object(self.fixture.reader,'balance',side_effect=[100,value]):
                    response=self.request('POST','/api/copy',body={'value':True})
                self.assertEqual(response.status_code,503,response.text)
                self.assertFalse(self.store.profile(1)[1]['copy_enabled'])

    def test_pausing_does_not_need_balance_and_does_not_close_positions(self):
        with patch.object(self.fixture.reader,'balance',side_effect=RuntimeError('offline')) as read:
            response=self.request('POST','/api/copy',body={'value':False,'confirm_open_positions':True})
        self.assertEqual(response.status_code,200,response.text)
        read.assert_not_called()
        self.assertEqual(self.fixture.accounts[fixtures.ACCOUNT_A].calls,[])
