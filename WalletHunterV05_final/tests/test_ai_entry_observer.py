"""Entry scheduling and persistent notification dedup, with fake public reads."""
import ast
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.ai_entry_observer import AiEntryObserver, entry_notice
from core.ai_review import account_guard
from core.ai_user_orders import AiUserOrders
from tests.test_ai_user_orders import Public, learning, profile as sample_profile, NOW, ADDRESS


def form(pid="form_1"):
    return {"id": pid, "status": "PENDING", "created_ms": NOW-1000, "expires_ms": NOW+90000,
            "payload": {"action": "OPEN", "coin": "BTC", "direction": "LONG", "size": 5.,
                        "balance_usdc": 999123., "leverage": 40}}


class MemoryStorage:
    def __init__(self):
        base = sample_profile()
        base.update(ai_trader_enabled=True, notifications=True)
        self.profiles = {1: base}
        self.reads = []
        self.on_read = None

    def profile(self, uid):
        self.reads.append(uid)
        if self.on_read: self.on_read(len(self.reads))
        return {}, deepcopy(self.profiles[int(uid)])

    def update_runtime(self, *args): raise AssertionError("Observer must not mutate trading state")
    def save(self, *args): raise AssertionError("Observer must not mutate profiles")


class EntryObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        clock = patch("core.ai_entry_observer.time.time", return_value=NOW/1000)
        self.clock = clock.start(); self.addCleanup(clock.stop)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden"))
        network.start(); self.addCleanup(network.stop)
        self.storage = MemoryStorage()
        self.orders = Mock()
        self.orders.summary.return_value = {"pending": [form()]}
        self.orders.prepare.return_value = {"pending": [form()]}
        self.public = Mock(return_value=object())
        self.reader = object()
        self.learning = Mock()
        self.learning.summary.return_value = learning()
        self.observer = self.new()

    def new(self):
        return AiEntryObserver(self.temp.name, self.storage, self.orders, self.public, self.reader, self.learning)

    def test_background_prepares_exact_public_inputs_without_trading_or_profile_changes(self):
        before = deepcopy(self.storage.profiles)
        result = self.observer.prepare(1)
        self.assertEqual(result["pending"][0]["id"], "form_1")
        self.learning.summary.assert_called_once_with(now_ms=NOW)
        args = self.orders.prepare.call_args.args
        self.assertEqual(args[0], 1)
        self.assertIs(args[2], self.public.return_value)
        self.assertIs(args[3], self.reader)
        self.assertEqual(args[4], self.learning.summary.return_value)
        self.assertEqual(args[5], NOW)
        self.assertEqual(self.storage.profiles, before)
        self.orders.decide.assert_not_called()
        self.orders.release.assert_not_called()

    def test_all_enabling_gates_are_required_before_public_or_learning_access(self):
        for change in ({"account": None}, {"ai_slot_selected": False}, {"ai_trader_enabled": False},
                       {"ai_trader_enabled": "true"}, {"leaders": [1, 2, 3]}, {"leaders": None},
                       {"crypto_enabled": False}, {"account": {"address": "invalid"}}):
            with self.subTest(change=change):
                initial = deepcopy(self.storage.profiles[1]); self.storage.profiles[1].update(change)
                self.assertIsNone(self.observer.prepare(1))
                self.storage.profiles[1] = initial
        self.public.assert_not_called(); self.learning.summary.assert_not_called()
        self.orders.prepare.assert_not_called()

    def test_copying_pause_and_notification_mute_do_not_stop_learning_preparation(self):
        self.storage.profiles[1].update(copy_enabled=False, notifications=False)
        self.observer.prepare(1)
        self.orders.prepare.assert_called_once()
        self.assertEqual(self.observer.notices(1)[0], [])
        self.assertFalse(self.observer.claim_notice(1, "form_1"))
        self.orders.summary.assert_not_called()

    def test_profile_then_account_guards_block_concurrent_actions(self):
        for target in ("telegram-profile:1", ADDRESS):
            with account_guard(self.temp.name, target):
                with self.assertRaises(OSError): self.observer.prepare(1)
        self.public.assert_not_called(); self.orders.prepare.assert_not_called()

    def test_account_change_during_guard_reload_does_not_prepare_or_notify(self):
        def change_on_second_read(count):
            if count == 2: self.storage.profiles[1]["account"] = {"address": "0x"+"2"*40}
        self.storage.on_read = change_on_second_read
        self.assertIsNone(self.observer.prepare(1))
        self.public.assert_not_called()

    def test_disabled_during_reload_does_not_prepare(self):
        def disable(count):
            if count == 2: self.storage.profiles[1]["ai_trader_enabled"] = False
        self.storage.on_read = disable
        self.assertIsNone(self.observer.prepare(1))
        self.orders.prepare.assert_not_called()

    def test_notices_are_readonly_copies_and_do_not_claim_until_requested(self):
        rows, current = self.observer.notices(1)
        self.assertEqual(current["account"]["address"], ADDRESS)
        rows[0]["payload"]["size"] = 999
        self.assertEqual(self.observer.notices(1)[0][0]["payload"]["size"], 5.)
        self.public.assert_not_called(); self.learning.summary.assert_not_called()
        self.orders.prepare.assert_not_called(); self.orders.decide.assert_not_called()

    def test_claim_is_atomic_at_most_once_and_survives_restart(self):
        self.assertTrue(self.observer.claim_notice(1, "form_1"))
        self.assertFalse(self.observer.claim_notice(1, "form_1"))
        reopened = self.new()
        self.assertFalse(reopened.claim_notice(1, "form_1"))
        self.assertEqual(reopened.notices(1)[0], [])
        # A failed Telegram send intentionally does not reset this claim.

    def test_claims_are_user_and_account_scoped(self):
        self.assertTrue(self.observer.claim_notice(1, "form_1"))
        self.storage.profiles[2] = deepcopy(self.storage.profiles[1])
        self.storage.profiles[2]["account"] = {"address": "0x"+"2"*40}
        self.assertTrue(self.observer.claim_notice(2, "form_1"))
        self.storage.profiles[1]["account"] = {"address": "0x"+"3"*40}
        self.assertTrue(self.observer.claim_notice(1, "form_1"))

    def test_unknown_expired_changed_or_disabled_form_cannot_be_claimed(self):
        self.assertFalse(self.observer.claim_notice(1, "missing"))
        self.assertFalse(self.observer.claim_notice(1, None))
        self.orders.summary.return_value["pending"][0]["status"] = "DECLINED"
        self.assertFalse(self.observer.claim_notice(1, "form_1"))
        self.orders.summary.return_value = {"pending": [form()]}
        self.clock.return_value = (NOW+90000)/1000
        self.assertFalse(self.observer.claim_notice(1, "form_1"))
        self.clock.return_value = NOW/1000
        self.storage.profiles[1]["ai_slot_selected"] = False
        self.assertFalse(self.observer.claim_notice(1, "form_1"))

    def test_only_fresh_open_btc_eth_forms_are_notified(self):
        invalid = []
        for change in ({"status": "UNKNOWN"}, {"created_ms": NOW+1}, {"expires_ms": NOW},
                       {"expires_ms": True}, {"id": "bad\n"}, {"payload": {"action": "REDUCE", "coin": "ETH"}}):
            row = form(); row.update(change); invalid.append(row)
        self.orders.summary.return_value = {"pending": [None, *invalid, form("valid")]}
        self.assertEqual([row["id"] for row in self.observer.notices(1)[0]], ["valid"])

    def test_learning_or_preparation_failure_is_not_hidden_as_success(self):
        self.learning.summary.side_effect = RuntimeError("Model unavailable")
        with self.assertRaises(RuntimeError): self.observer.prepare(1)
        self.orders.prepare.assert_not_called()
        self.learning.summary.side_effect = None
        self.orders.prepare.side_effect = ValueError("No market data")
        with self.assertRaises(ValueError): self.observer.prepare(1)
        self.assertEqual(self.observer.notices(1)[0][0]["id"], "form_1")

    def test_real_order_preparation_with_signal_never_forces_below_minimum(self):
        service = AiUserOrders(self.temp.name, monotonic=lambda: 0)
        public = Public(); public.balance = 1.; public.capacity = 1.
        observer = AiEntryObserver(self.temp.name, self.storage, service, lambda _: public, None, self.learning)
        before = deepcopy(self.storage.profiles)
        result = observer.prepare(1)
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["reason"], "minimum_notional")
        self.assertEqual(self.storage.profiles, before)
        self.assertEqual(public.live_positions, [])
        self.assertEqual(observer.notices(1)[0], [])

    def test_real_valid_signal_creates_persistent_form_but_no_position_or_trade_hold(self):
        service = AiUserOrders(self.temp.name, monotonic=lambda: 0)
        public = Public()
        observer = AiEntryObserver(self.temp.name, self.storage, service, lambda _: public, None, self.learning)
        result = observer.prepare(1)
        self.assertEqual(len(result["pending"]), 1)
        row = result["pending"][0]
        self.assertTrue(observer.claim_notice(1, row["id"]))
        self.assertEqual(public.live_positions, [])
        self.assertEqual(service.reserved_markets(None, ADDRESS), {})
        self.assertEqual(service.journal.pending(ADDRESS), set())

    def test_notice_is_bilingual_without_private_balance_quantity_or_probability_claim(self):
        for english in (True, False):
            text = entry_notice(form(), english)
            self.assertIn("BTC", text)
            self.assertNotIn("999123", text)
            self.assertNotIn("60%", text)
            if english: self.assertFalse(any("\u0400" <= c <= "\u04ff" for c in text))

    def test_observer_source_has_no_execution_or_key_access(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/"core/ai_entry_observer.py").read_text(encoding="utf-8"))
        attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse({"decide", "submit_user_ioc", "market_open", "set_leverage", "decrypt",
                          "update_runtime", "set_enabled", "save"} & attrs)


if __name__ == "__main__":
    unittest.main()
