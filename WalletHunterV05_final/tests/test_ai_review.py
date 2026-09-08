import json
import tempfile
import time
import unittest
from copy import deepcopy
from contextlib import closing
from unittest.mock import patch
from cryptography.fernet import Fernet
from core.ai_review import AiReview, analyse, identity, market_key, account_guard
from core.storage import Storage


class FakeClient:
    exchange = object()
    network = 'TESTNET'
    address = '0x'+'a'*40
    def __init__(self):
        self.position = {"coin": "BTC", "dex": "", "side": "LONG", "size": 2.,
                         "entry_price": 120., "leverage": 5., "roe": -50.}
        self.price = 100.
        self.calls = 0
        self.fail = False
        self.partial = False
        self.info=self;self.orders={};self.fills=[]
        self.position.update(position_value=200.,margin_used=40.)
    def user_state(self,*args): return {'time':int(time.time()*1000)}
    def frontend_open_orders(self,*args): return []
    def post(self,*args): return {'time':int(time.time()*1000),'levels':[[{'px':'99.99'}],[{'px':'100.01'}]]}
    def round_price(self,coin,price,dex=''): return price
    def response_error(self,response): return ''
    def query_order_by_cloid(self,cloid):return self.orders.get(cloid,{'status':'unknownOid'})
    def user_fills_by_time(self,account,start,end):return [f for f in self.fills if start<=f['time']<=end]
    def submit_copy_ioc(self,coin,buy,size,limit,reduce_only,cloid,dex='',*,expires_ms):
        assert reduce_only
        now=int(time.time()*1000)
        before=self.position['size'];self.market_reduce(coin,buy,size,dex,.5)
        filled=before-self.position['size'];self.position['position_value']=self.position['size']*self.price
        self.position['margin_used']=self.position['position_value']/self.position['leverage']
        self.orders[cloid]={'status':'order','order':{'status':'iocCancel' if self.partial else 'filled','statusTimestamp':now,
            'order':{'coin':coin,'side':'B' if buy else 'A','oid':1,'cloid':cloid,'origSz':str(size),'limitPx':str(limit),'reduceOnly':True,'timestamp':now}}}
        self.fills.append({'coin':coin,'side':'B' if buy else 'A','oid':1,'tid':1,'time':now,'sz':str(filled),'px':str(self.price)})
        return {'status':'ok'}
    def positions(self, *args): return [deepcopy(self.position)]
    def balance(self): return 300.
    def mid(self, *args): return self.price
    def size_step(self, *args): return .01
    def round_size(self, coin, value, dex): return round(value, 2)
    def market_reduce(self, *args):
        self.calls += 1
        if self.fail: raise TimeoutError("uncertain response")
        self.position["size"] -= .1 if self.partial else args[2]
        return {"ok": True}
    def order_error(self, response): return ""


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Storage(self.tmp.name, Fernet.generate_key())
        d, p = self.store.profile(1)
        p["account"] = {"address": FakeClient.address, "id": "a"}
        self.store.save(d)
        self.ai = AiReview(self.tmp.name)
        self.client = FakeClient()

    def seed(self):
        payload = {"position": identity(self.client.position), "price": 100., "size": .5, "action": "REDUCE"}
        with closing(self.ai.connect()) as db:
            db.execute("INSERT INTO proposals(id,user_id,account,market,created,expires,status,payload) VALUES(?,?,?,?,?,?,?,?)",
                       ("test", "1", FakeClient.address, "BTC|", time.time(), time.time()+300, "PENDING", json.dumps(payload)))
            db.commit()
        return "test"

    def allowed(self):
        # Test fixture ONLY. Production gate is intentionally closed without
        # verified ownership/budget and a calibrated model; no real SDK imported.
        return patch.object(self.ai, "intervention_gate", return_value={"allowed": True})

    def test_no_confirm_no_trade_and_decline_persists(self):
        self.seed()
        self.ai.decide(1, "test", False, self.store, lambda _: self.client)
        self.assertEqual(AiReview(self.tmp.name).list(1)[0]["status"], "DECLINED")
        self.assertEqual(self.client.calls, 0)

    def test_cross_user_cannot_read_or_confirm(self):
        self.seed()
        self.assertEqual(self.ai.list(2), [])
        with self.assertRaises(ValueError): self.ai.decide(2, "test", True, self.store, lambda _: self.client)
        self.assertEqual(self.ai.list(1)[0]["status"], "PENDING")

    def test_missing_calibrated_model_blocks_confirmation(self):
        self.seed()
        with self.assertRaisesRegex(ValueError, "60%"):
            self.ai.decide(1, "test", True, self.store, lambda _: self.client)
        self.assertEqual(self.client.calls, 0)

    def test_disabled_review_invalidates_previous_confirmation_without_signing_client(self):
        self.seed()
        _, profile = self.store.profile(1)
        profile['ai_review_enabled'] = False
        self.store.update_profile(1, profile)
        def forbidden_client(_):
            raise AssertionError('Disabled mode must not acquire a signing client')
        with self.assertRaisesRegex(ValueError, 'AI review is disabled'):
            self.ai.decide(1, 'test', True, self.store, forbidden_client)
        self.assertEqual(self.ai.list(1)[0]['status'], 'INVALIDATED')
        self.assertEqual(self.client.calls, 0)

    def test_one_confirmation_one_fake_order(self):
        self.seed()
        with self.allowed():
            result = self.ai.decide(1, "test", True, self.store, lambda _: self.client)
            self.assertEqual(result["status"], "EXECUTED")
            with self.assertRaises(ValueError): self.ai.decide(1, "test", True, self.store, lambda _: self.client)
        self.assertEqual(self.client.calls, 1)
        self.assertIn("BTC|", self.store.profile(1)[1]["runtime"]["ai_hold_keys"])

    def test_changed_price_blocks(self):
        self.seed(); self.client.price = 101
        with self.allowed(), self.assertRaisesRegex(ValueError, "Price moved"):
            self.ai.decide(1, "test", True, self.store, lambda _: self.client)
        self.assertEqual(self.client.calls, 0)

    def test_changed_position_blocks(self):
        self.seed(); self.client.position["size"] = 3
        with self.allowed(), self.assertRaisesRegex(ValueError, "Position changed"):
            self.ai.decide(1, "test", True, self.store, lambda _: self.client)

    def test_expired_confirmation_is_not_executable(self):
        self.seed()
        with closing(self.ai.connect()) as db:
            db.execute("UPDATE proposals SET expires=0")
            db.commit()
        with self.assertRaisesRegex(ValueError, "expired"):
            self.ai.decide(1, "test", True, self.store, lambda _: self.client)

    def test_unknown_execution_is_not_retried_after_restart(self):
        self.seed(); self.client.fail = True
        with self.allowed(), self.assertRaisesRegex(ValueError, "UNKNOWN"):
            self.ai.decide(1, "test", True, self.store, lambda _: self.client)
        with self.assertRaises(ValueError):
            AiReview(self.tmp.name).decide(1, "test", True, self.store, lambda _: self.client)
        self.assertEqual(self.client.calls, 1)
        with self.assertRaises(ValueError): self.ai.resume(1, "BTC|", self.store)

    def test_partial_execution_not_reported_complete(self):
        self.seed(); self.client.partial = True
        with self.allowed():
            self.assertEqual(self.ai.decide(1, "test", True, self.store, lambda _: self.client)["status"], "PARTIAL")

    def test_guard_prevents_concurrent_copy_and_review(self):
        with account_guard(self.tmp.name, "0xabc"):
            with self.assertRaises(OSError):
                with account_guard(self.tmp.name, "0xabc"): pass

    def test_threshold_and_gate(self):
        class Reader:
            def market_context(self, *args): raise AssertionError("Should not fetch above threshold")
        self.client.position["roe"] = -39.99
        p = self.store.profile(1)[1]
        self.assertEqual(self.ai.generate(1, p, self.client, Reader()), [])
        self.assertFalse(self.ai.intervention_gate(p, self.client.position)["allowed"])

    def test_invalid_and_stale_candles_block(self):
        with self.assertRaises(ValueError): analyse([], {}, int(time.time()*1000))

    def test_eight_factors_use_closed_candles(self):
        end = (int(time.time()*1000)//900000)*900000
        candles = [{"t": end-(70-i)*900000, "T": end-(69-i)*900000-1,
                    "c": str(100+i), "h": str(101+i), "l": str(99+i), "v": "10"} for i in range(70)]
        result = analyse(candles, {"funding_bps_hour": .1, "open_interest": 1000}, end)
        self.assertAlmostEqual(result["rsi14"], 100)
        self.assertAlmostEqual(result["volume_ratio20"], 1)
        self.assertEqual(result["candle_close_ms"], end-1)


if __name__ == "__main__": unittest.main()
