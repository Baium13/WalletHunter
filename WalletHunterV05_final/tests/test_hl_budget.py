import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import requests
from core.hl_budget import Budget,BudgetSession,BudgetUnavailable,weights,snapshot


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'budget.sqlite';self.b=Budget(self.path)
    def response(self,status=200,body=None):
        r=requests.Response();r.status_code=status;r._content=json.dumps(body or {}).encode();return r
    def test_official_weights_and_response_blocks(self):
        self.assertEqual(weights('allMids',{}),2)
        self.assertEqual(weights('userRole',{}),60)
        self.assertEqual(weights('meta',{}),20)
        self.assertEqual(weights('userFillsByTime',{},[{}]*2000),120)
        self.assertEqual(weights('candleSnapshot',{},[{}]*61),22)
        self.assertEqual(weights('exchange',{'action':{'orders':[{}]*79}}),2)
    def test_shared_rolling_budget_reserves_for_safety(self):
        with self.b.db() as db:db.execute('UPDATE limits SET soft=120,hard=240,enforce=1')
        a=self.b.begin('userFillsByTime',{},'deep',5)
        other=Budget(self.path)
        with self.assertRaises(BudgetUnavailable):other.begin('allMids',{},'research',2)
        other.begin('orderStatus',{},'reconciliation',0)
        self.b.finish(a,'userFillsByTime',{},self.response(body=[]),.1)
        other.begin('allMids',{},'research',2)
        self.assertEqual(snapshot(self.path)['counters']['budget_deferred'],1)
    def test_no_retry_and_no_sensitive_payload_persistence(self):
        with patch('core.hl_budget.configured',return_value=self.b),patch.object(requests.Session,'request',return_value=self.response(429)) as transport:
            BudgetSession().post('https://api.hyperliquid.xyz/exchange',json={'action':{'type':'order'},'signature':'SECRET'})
        self.assertEqual(transport.call_count,1)
        self.assertNotIn(b'SECRET',self.path.read_bytes())
        self.assertEqual(snapshot(self.path)['http_429'],1)
        with self.b.db() as db:db.execute('UPDATE limits SET enforce=1')
        with self.assertRaises(BudgetUnavailable):self.b.begin('orderStatus',{},'safety',0)
    def test_observe_records_but_does_not_throttle(self):
        for _ in range(12):self.b.begin('userFillsByTime',{},'deep',5)
        self.assertGreater(snapshot(self.path)['rest_weight_1m'],1200)
    def test_sdk_including_constructor_routes_through_transport(self):
        from core.hl_budget import install_sdk
        from hyperliquid.api import API
        install_sdk();api=API('https://api.hyperliquid.xyz',timeout=20)
        self.assertIsInstance(api.session,BudgetSession)
        with patch('core.hl_budget.configured',return_value=self.b),patch.object(requests.Session,'request',return_value=self.response(body={'universe':[]})):
            api.post('/info',{'type':'meta'})
        api.session.close()
        self.assertEqual(snapshot(self.path)['rest_weight_1m'],20)
    def test_timeout_is_not_zero_and_account_calls_never_cached(self):
        with patch('core.hl_budget.configured',return_value=self.b),patch.object(requests.Session,'request',side_effect=requests.Timeout) as transport:
            for _ in range(2):
                with self.assertRaises(requests.Timeout):BudgetSession().post('https://api.hyperliquid.xyz/info',json={'type':'clearinghouseState'})
        self.assertEqual(transport.call_count,2);self.assertEqual(snapshot(self.path)['timeouts'],2)


if __name__=='__main__':unittest.main()
