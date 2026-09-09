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
        with self.b.db() as db:db.execute('UPDATE limits SET enforce=0')
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

    def test_metadata_ttl_network_pool_and_no_sliding_renewal(self):
        body={'universe':[]}
        with patch('core.hl_budget.configured',return_value=self.b),patch.object(requests.Session,'request',return_value=self.response(body=body)) as transport:
            session=BudgetSession()
            for _ in range(3):session.post('https://api.hyperliquid.xyz/info',json={'type':'meta'})
            self.assertEqual(transport.call_count,1)
            session.post('https://api.hyperliquid-testnet.xyz/info',json={'type':'meta'})
            session.post('https://api.hyperliquid.xyz/info',json={'type':'meta','dex':'xyz'})
            self.assertEqual(transport.call_count,3)
            with self.b.db() as db:db.execute('UPDATE metadata SET expires=0')
            session.post('https://api.hyperliquid.xyz/info',json={'type':'meta'})
            self.assertEqual(transport.call_count,4)

    def test_single_flight_shared_across_sessions(self):
        import threading,time
        from concurrent.futures import ThreadPoolExecutor
        arrived=threading.Event()
        def upstream(*a,**k):
            arrived.set();time.sleep(.1);return self.response(body={'universe':[]})
        def call():return BudgetSession().post('https://api.hyperliquid.xyz/info',json={'type':'meta'}).json()
        with patch('core.hl_budget.configured',return_value=self.b),patch.object(requests.Session,'request',side_effect=upstream) as transport:
            with ThreadPoolExecutor(2) as pool:
                first=pool.submit(call);arrived.wait(1);second=pool.submit(call)
                self.assertEqual(first.result(),second.result())
            self.assertEqual(transport.call_count,1)

    def test_failed_metadata_not_cached_and_no_exchange_coalescing(self):
        with patch('core.hl_budget.configured',return_value=self.b),patch.object(requests.Session,'request',return_value=self.response(503)) as transport:
            for _ in range(2):BudgetSession().post('https://api.hyperliquid.xyz/info',json={'type':'meta'})
            for _ in range(2):BudgetSession().post('https://api.hyperliquid.xyz/exchange',json={'action':{'type':'order'}})
            self.assertEqual(transport.call_count,4)

    def test_priority_scope_does_not_leak(self):
        from core.hl_budget import priority_scope,origin
        before=origin()
        with priority_scope('position_owned.detect',1):self.assertEqual(origin(),('position_owned.detect',1))
        self.assertEqual(origin(),before)

    def test_deferred_low_priority_ages_without_using_safety_reserve(self):
        with patch('core.hl_budget.time.time',return_value=1000):
            with self.b.db() as db:db.execute('UPDATE limits SET soft=120,hard=240')
            self.b.begin('userFillsByTime',{},'busy',3)
            with self.assertRaises(BudgetUnavailable):self.b.begin('userFillsByTime',{},'deep',5)
            self.b.begin('orderStatus',{},'reconcile',0)
        with patch('core.hl_budget.time.time',return_value=1061):
            # Waiter survives, then receives service after rolling budget frees.
            self.b.begin('userFillsByTime',{},'deep',5)
            with self.assertRaises(BudgetUnavailable):self.b.begin('allMids',{},'new-discovery',6)
            self.b.begin('orderStatus',{},'reconcile',0)
            self.assertLessEqual(snapshot(self.path)['rest_weight_1m'],240)

    def test_websocket_failure_backoff_and_jitter_no_tight_retry(self):
        from core.intelligence.service import PublicTrades
        stream=PublicTrades('TESTNET')
        with patch('websocket.create_connection',side_effect=OSError),patch('core.hl_budget.configured',return_value=self.b) as config:
            with self.assertRaises(ValueError):stream.poll()
            deadline=stream.retry_at
            with self.assertRaisesRegex(ValueError,'BACKOFF'):stream.poll()
            self.assertEqual(stream.retry_at,deadline);self.assertEqual(stream.failures,1)
        self.assertEqual(snapshot(self.path)['ws_connections'],0)


if __name__=='__main__':unittest.main()
