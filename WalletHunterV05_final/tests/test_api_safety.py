"""Isolated HTTP safety regressions. No .env, production data or network used.

Run with the audit environment containing requirements + httpx. Every server
constructor is redirected before import; real SQLite/JSON persistence lives only
under TemporaryDirectory. Fake exchange responses never submit financial orders.
"""
from contextlib import ExitStack, closing
from copy import deepcopy
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
import uuid

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from core.ai_assistant import AiAssistant as RealAssistant
from core.ai_review import AiReview as RealReview, identity
from core.storage import Storage as RealStorage


TOKEN = "000000000:offline-test-token-not-a-real-bot"
ACCOUNT_A = "0x" + "a" * 40
ACCOUNT_B = "0x" + "b" * 40
LEADERS = ["0x" + char * 40 for char in "1234"]


def init_data(uid=1, *, auth_date=None):
    values = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "offline-query",
        "user": json.dumps({"id": uid, "first_name": f"User {uid}"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def position(coin="BTC", dex="", size=2., side="LONG"):
    return {"coin": coin, "dex": dex, "side": side, "size": size,
            "entry_price": 120., "leverage": 5., "roe": -50.,
            "position_value": size * 100, "margin_used": size * 20,
            "unrealized_pnl": -20., "market_type": "STOCKS" if dex else "CRYPTO"}


class OfflineReader:
    def __init__(self):
        self.rows = {}
        self.failure = False
    def positions(self, address, *args):
        if self.failure:
            raise RuntimeError("Public position read failed")
        return deepcopy(self.rows.get(address, []))
    def balance(self, address):
        return 111 if address == ACCOUNT_A else 222
    def leverage_choices(self):
        return [1, 2, 5, 10, 20, 40]


class OfflineAccount:
    def balance(self): return 300.0
    exchange = object()
    def __init__(self):
        self.rows = [position()]
        self.orders = []
        self.calls = []
        self.price = 100.
        self.failure = False
        self.cancel_failure = False
        self.partial = False
    def positions(self, *args):
        if self.failure:
            raise RuntimeError("Private position read failed")
        return deepcopy(self.rows)
    def frontend_open_orders(self, dex):
        return deepcopy(self.orders)
    def mid(self, *args):
        return self.price
    def round_size(self, coin, size, dex):
        return round(size, 2)
    def size_step(self, *args):
        return .01
    def place_stop_loss(self, coin, side, size, price, dex):
        oid = max((o["oid"] for o in self.orders), default=0) + 1
        self.calls.append(("stop", coin, size, price, dex))
        self.orders.append({"coin": coin, "oid": oid, "reduceOnly": True,
                            "orderType": "Stop Market", "triggerPx": str(price)})
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}
    def cancel_order(self, coin, oid, dex):
        self.calls.append(("cancel", coin, oid, dex))
        if self.cancel_failure:
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "Rejected cancel"}]}}}
        self.orders = [o for o in self.orders if o["oid"] != oid]
        return {"status": "ok", "response": {"data": {"statuses": ["success"]}}}
    def market_close(self, coin, dex, slippage_pct=0.5):
        self.calls.append(("close", coin, dex))
        self.rows = [dict(self.rows[0], size=.5)] if self.partial else []
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "2"}}]}}}
    def market_reduce(self, coin, is_buy, size, dex, slippage):
        self.calls.append(("reduce", coin, size, dex))
        self.rows[0]["size"] -= size
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": str(size)}}]}}}
    def order_error(self, response):
        return ""


class ApiSafetyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.reader = OfflineReader()
        self.accounts = {ACCOUNT_A: OfflineAccount(), ACCOUNT_B: OfflineAccount()}
        self.accounts[ACCOUNT_B].rows = [position("ETH", size=1)]
        self.fake_settings = SimpleNamespace(
            master_key=Fernet.generate_key(), telegram_bot_token=TOKEN,
            hl_mode="TESTNET", auto_trading=False, max_leverage=40,
            max_position_pct=100., max_total_exposure_usd=10000.,
            max_slippage_pct=.5, entry_price_tolerance_pct=.5,
            watch_interval=3,
        )
        fake_module = ModuleType("core.settings")
        fake_module.load = lambda: self.fake_settings
        self.stack.enter_context(patch.dict(sys.modules, {"core.settings": fake_module}))
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden in API tests")))
        self.stack.enter_context(patch("core.storage.Storage", side_effect=lambda root, key: RealStorage(self.tmp, key)))
        self.stack.enter_context(patch("core.ai_assistant.AiAssistant", side_effect=lambda root: RealAssistant(self.tmp)))
        self.stack.enter_context(patch("core.ai_review.AiReview", side_effect=lambda root: RealReview(self.tmp)))
        self.stack.enter_context(patch("core.hyperliquid.HyperliquidReader", side_effect=lambda mode: self.reader))
        source = Path(__file__).resolve().parents[1] / "webapp" / "server.py"
        name = "offline_api_" + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(name, source)
        self.server = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.server
        self.addCleanup(lambda: sys.modules.pop(name, None))
        spec.loader.exec_module(self.server)
        # Static files were mounted from the repository (read-only); subsequent
        # account locks must be created in the temporary root, not real data.
        self.server.ROOT = self.tmp
        self.stack.enter_context(patch.object(self.server, "account_client_for",
            side_effect=lambda profile: self.accounts[profile["account"]["address"]]))
        self.api = self.stack.enter_context(TestClient(self.server.app, raise_server_exceptions=False))
        self.store = self.server.storage
        self.seed_user(1, ACCOUNT_A)
        self.seed_user(2, ACCOUNT_B)

    def seed_user(self, uid, address):
        data, profile = self.store.profile(uid)
        profile.update(account={"id": f"a{uid}", "address": address,
                                "private_key": self.store.encrypt("offline-not-a-key")},
                       leaders=LEADERS[:3], copy_enabled=True)
        profile["runtime"]["journal"] = [{"time": uid, "action": f"private-event-{uid}"}]
        self.store.save(data)
        self.reader.rows[address] = deepcopy(self.accounts[address].rows)

    def request(self, method, path, uid=1, body=None, data=None):
        headers = {"X-Telegram-Init-Data": data if data is not None else init_data(uid)}
        return self.api.request(method, path, headers=headers, json=body)

    def seed_proposal(self, *, expires=None, proposal="offline-review"):
        payload = {"position": identity(self.accounts[ACCOUNT_A].rows[0]), "price": 100.,
                   "action": "REDUCE", "size": .5}
        with closing(self.server.ai_review.connect()) as db:
            db.execute("INSERT INTO proposals(id,user_id,account,market,created,expires,status,payload) VALUES(?,?,?,?,?,?,?,?)",
                       (proposal, "1", ACCOUNT_A, "BTC|", time.time(), expires or time.time()+300,
                        "PENDING", json.dumps(payload)))
            db.commit()
        return f"/api/ai/reviews/{proposal}/decision"

    def test_auth_missing_tampered_expired_and_future_are_rejected(self):
        for data in ["", init_data(1).replace("hash=", "hash=00"), init_data(auth_date=1),
                     init_data(auth_date=int(time.time())+3600)]:
            with self.subTest(data=data[:30]):
                self.assertEqual(self.request("GET", "/api/dashboard", data=data).status_code, 401)

    def test_malformed_auth_date_is_auth_error_not_server_error(self):
        self.assertEqual(self.request("GET", "/api/dashboard", data=init_data(auth_date="invalid")).status_code, 401)

    def test_dashboard_isolates_user_account_events_and_settings(self):
        first = self.request("GET", "/api/dashboard").json()
        second = self.request("GET", "/api/dashboard", uid=2).json()
        self.assertEqual((first["balance"], second["balance"]), (111, 222))
        self.assertEqual(first["events"][0]["action"], "private-event-1")
        self.assertEqual(second["events"][0]["action"], "private-event-2")
        self.assertNotIn("private_key", json.dumps(first))
        self.assertEqual(self.request("PUT", "/api/settings", body={"language": "en"}).status_code, 200)
        self.assertEqual(self.store.profile(2)[1]["language"], "ru")

    def test_manual_action_without_account_is_rejected(self):
        for method, path, body in [
            ("POST", "/api/position/close", {"coin": "BTC"}),
            ("POST", "/api/position/stop-loss", {"coin": "BTC", "price": 80}),
            ("DELETE", "/api/position/stop-loss", {"coin": "BTC"}),
        ]:
            with self.subTest(path=path, method=method):
                self.assertEqual(self.request(method, path, uid=3, body=body).status_code, 400)
        self.assertEqual(self.accounts[ACCOUNT_A].calls, [])

    def test_manual_stop_cancel_roundtrip_is_user_scoped_and_persisted(self):
        result = self.request("POST", "/api/position/stop-loss", body={"coin": "BTC", "price": 80})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("BTC|", self.store.profile(1)[1]["runtime"]["manual_stops"])
        self.assertNotIn("manual_stops", self.store.profile(2)[1]["runtime"])
        result = self.request("DELETE", "/api/position/stop-loss", body={"coin": "BTC"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.accounts[ACCOUNT_A].orders, [])
        self.assertEqual(self.accounts[ACCOUNT_B].calls, [])
        self.assertNotIn("BTC|", self.store.profile(1)[1]["runtime"]["manual_stops"])

    def test_failed_cancel_returns_conflict_and_preserves_stop(self):
        self.request("POST", "/api/position/stop-loss", body={"coin": "BTC", "price": 80})
        self.accounts[ACCOUNT_A].cancel_failure = True
        result = self.request("DELETE", "/api/position/stop-loss", body={"coin": "BTC"})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIn("BTC|", self.store.profile(1)[1]["runtime"]["manual_stops"])
        self.assertEqual(len(self.accounts[ACCOUNT_A].orders), 1)

    def test_manual_close_missing_canonical_context_fails_closed_without_touching_other_account(self):
        result = self.request("POST", "/api/position/close", body={"coin": "BTC"})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(len(self.accounts[ACCOUNT_A].rows), 1)
        self.assertIn("BTC|", self.store.profile(1)[1]["runtime"]["manual_hold_keys"])
        self.assertEqual(self.accounts[ACCOUNT_B].calls, [])
        self.assertEqual(self.accounts[ACCOUNT_A].calls, [])

    def test_manual_read_failure_is_503_and_submits_nothing(self):
        self.accounts[ACCOUNT_A].failure = True
        result = self.request("POST", "/api/position/close", body={"coin": "BTC"})
        self.assertEqual(result.status_code, 503, result.text)
        self.assertEqual(self.accounts[ACCOUNT_A].calls, [])

    def test_account_deletion_requires_confirmation_and_unknown_read_blocks(self):
        result = self.request("DELETE", "/api/account")
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIsNotNone(self.store.profile(1)[1]["account"])
        self.reader.failure = True
        result = self.request("DELETE", "/api/account", body={"confirm_open_positions": True})
        self.assertEqual(result.status_code, 503, result.text)
        self.assertIsNotNone(self.store.profile(1)[1]["account"])
        self.reader.failure = False
        result = self.request("DELETE", "/api/account", body={"confirm_open_positions": True})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIsNone(self.store.profile(1)[1]["account"])
        self.assertIsNotNone(self.store.profile(2)[1]["account"])
        self.assertEqual(self.accounts[ACCOUNT_A].calls, [])

    def test_wallet_delete_then_add_releases_capacity(self):
        self.assertEqual(self.request("POST", "/api/wallet", body={"address": LEADERS[3]}).status_code, 400)
        self.assertEqual(self.request("DELETE", "/api/wallet/2", body={"confirm_open_positions": True}).status_code, 200)
        result = self.request("POST", "/api/wallet", body={"address": LEADERS[3]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.store.profile(1)[1]["leaders"], [LEADERS[0], LEADERS[2], LEADERS[3]])
        self.assertEqual(self.store.profile(2)[1]["leaders"], LEADERS[:3])

    def test_ai_slot_does_not_block_replacement_of_deleted_wallet(self):
        data, profile = self.store.profile(1)
        profile.update(leaders=LEADERS[:2], ai_slot_selected=True)
        self.store.save(data)
        self.assertEqual(self.request("DELETE", "/api/wallet/1", body={"confirm_open_positions": True}).status_code, 200)
        result = self.request("POST", "/api/wallet", body={"address": LEADERS[3]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(self.store.profile(1)[1]["ai_slot_selected"])
        self.assertEqual(self.request("POST", "/api/wallet", body={"address": LEADERS[2]}).status_code, 400)

    def test_wallet_deletion_requires_confirmation_when_positions_are_open(self):
        self.reader.rows[LEADERS[0]] = [position()]
        result = self.request("DELETE", "/api/wallet/1")
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.store.profile(1)[1]["leaders"], LEADERS[:3])
        self.reader.failure = True
        result = self.request("DELETE", "/api/wallet/1", body={"confirm_open_positions": True})
        self.assertEqual(result.status_code, 503, result.text)

    def test_cross_user_ai_confirmation_does_not_consume_owner_proposal(self):
        path = self.seed_proposal()
        self.assertEqual(self.request("POST", path, uid=2, body={"confirm": True}).status_code, 409)
        self.assertEqual(self.server.ai_review.list(1)[0]["status"], "PENDING")
        self.assertEqual(self.accounts[ACCOUNT_A].calls, [])

    def test_ai_decline_and_duplicate_decision_never_trade(self):
        path = self.seed_proposal()
        self.assertEqual(self.request("POST", path, body={"confirm": False}).status_code, 200)
        self.assertEqual(self.request("POST", path, body={"confirm": True}).status_code, 409)
        self.assertEqual(self.accounts[ACCOUNT_A].calls, [])

    def test_ai_unvalidated_probability_gate_blocks_live_confirmation(self):
        path = self.seed_proposal()
        result = self.request("POST", path, body={"confirm": True})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIn("60%", result.text)
        self.assertEqual(self.accounts[ACCOUNT_A].calls, [])

    def test_ai_stale_confirmation_returns_409_without_fake_order(self):
        path = self.seed_proposal()
        self.accounts[ACCOUNT_A].price = 102.
        # Fixture-only: isolate staleness gate independently of missing training.
        with patch.object(self.server.ai_review, "intervention_gate", return_value={"allowed": True}):
            result = self.request("POST", path, body={"confirm": True})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIn("stale", result.text)
        self.assertEqual(self.accounts[ACCOUNT_A].calls, [])

    def test_ai_valid_fixture_confirmation_executes_only_once(self):
        path = self.seed_proposal()
        # This patch exists only inside the isolated fake-exchange test.
        with patch.object(self.server.ai_review, "intervention_gate", return_value={"allowed": True}):
            result = self.request("POST", path, body={"confirm": True})
            duplicate = self.request("POST", path, body={"confirm": True})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.assertEqual(len(self.accounts[ACCOUNT_A].calls), 1)
        self.assertEqual(self.accounts[ACCOUNT_B].calls, [])


if __name__ == "__main__":
    unittest.main()
