import ast
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from core.ai_review import account_guard
from core.profile_mutation import save_profile_guarded, trading_changes, legacy_trading_callback
from core.state_snapshot import StateConflict
from core.storage import Storage


class ProfileMutationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Storage(self.tmp.name, Fernet.generate_key())
        _, p = self.store.profile(1)
        p.update(account={"address": "account-a", "id": "a"}, leaders=["one"], copy_enabled=True)
        p["runtime"].update(chat_message_ids=[1, 2], journal=[{"time": 50, "action": "OLD"}])
        self.store.update_profile(1, p)

    def test_busy_rejects_every_trading_profile_setting(self):
        changes = [{"account": None}, {"leaders": ["two"]}, {"leader_enabled": {"one": False}},
                   {"copy_enabled": False}, {"crypto_enabled": False}, {"stocks_enabled": False},
                   {"risk_mode": "aggressive"}, {"strategy_mode": "experimental"},
                   {"max_leverage": 20}, {"ai_slot_selected": True}]
        before = deepcopy(self.store.load())
        with account_guard(self.tmp.name, "account-a"):
            for change in changes:
                with self.subTest(change=change):
                    _, p = self.store.profile(1); p.update(change)
                    with self.assertRaisesRegex(ValueError, "операция"):
                        save_profile_guarded(self.store, 1, p)
        self.assertEqual(self.store.load(), before)

    def test_chat_clear_and_analytical_updates_do_not_reacquire_trade_guard(self):
        with account_guard(self.tmp.name, "account-a"):
            _, p = self.store.profile(1)
            p["runtime"].update(journal=[], chat_message_ids=[], notification_message_ids=[], journal_cleared_at=100)
            p["leader_models"] = {"one": {"score": 55}}
            p["notifications"] = False
            p["language"] = "en"
            self.assertEqual(trading_changes(p), set())
            save_profile_guarded(self.store, 1, p)
        saved = self.store.profile(1)[1]
        self.assertEqual(saved["runtime"]["journal"], [])
        self.assertEqual(saved["leader_models"]["one"]["score"], 55)
        self.assertFalse(saved["notifications"])

    def test_acknowledged_runtime_changes_do_not_deadlock_chat_clear(self):
        _, p = self.store.profile(1)
        with account_guard(self.tmp.name, "account-a"):
            p["runtime"]["managed"] = ["BTC|"]
            self.store.update_runtime(1, p["runtime"])
            p["runtime"].update(journal=[], journal_cleared_at=100)
            save_profile_guarded(self.store, 1, p)
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], ["BTC|"])

    def test_later_close_is_not_resurrected_by_acknowledged_old_profile(self):
        _, p = self.store.profile(1)
        p["runtime"]["managed"] = ["BTC|"]
        self.store.update_runtime(1, p["runtime"])
        _, fresh = self.store.profile(1)
        fresh["runtime"]["managed"] = []
        self.store.update_runtime(1, fresh["runtime"])
        p["runtime"].update(journal=[], journal_cleared_at=100)
        save_profile_guarded(self.store, 1, p)
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], [])

    def test_busy_runtime_control_change_is_rejected_but_journal_is_not(self):
        _, p = self.store.profile(1)
        p["runtime"]["manual_hold_keys"] = ["BTC|"]
        with account_guard(self.tmp.name, "account-a"), self.assertRaises(ValueError):
            save_profile_guarded(self.store, 1, p)

    def test_old_actual_and_new_account_locks_are_respected(self):
        for locked in ("account-a", "account-b"):
            _, p = self.store.profile(1); p["account"] = {"address": "account-b", "id": "b"}
            with account_guard(self.tmp.name, locked), self.assertRaises(ValueError):
                save_profile_guarded(self.store, 1, p)
        self.assertEqual(self.store.profile(1)[1]["account"]["address"], "account-a")

    def test_unbound_profile_lock_uses_same_namespace_as_api(self):
        self.store.profile(2)
        _, p = self.store.profile(2); p["account"] = {"address": "account-b", "id": "b"}
        with account_guard(self.tmp.name, "telegram-profile:2"), self.assertRaises(ValueError):
            save_profile_guarded(self.store, 2, p)

    def test_stale_conflicting_risk_edit_does_not_replace_fresh_choice(self):
        _, a = self.store.profile(1); _, b = self.store.profile(1)
        a["max_leverage"] = 5; b["max_leverage"] = 20
        save_profile_guarded(self.store, 1, a)
        with self.assertRaises(StateConflict): save_profile_guarded(self.store, 1, b)
        self.assertEqual(self.store.profile(1)[1]["max_leverage"], 5)

    def test_changed_account_requires_reload_before_stale_copy_toggle(self):
        _, old = self.store.profile(1)
        _, current = self.store.profile(1); current["account"] = {"address": "account-b", "id": "b"}
        save_profile_guarded(self.store, 1, current)
        old["copy_enabled"] = False
        with self.assertRaises(StateConflict): save_profile_guarded(self.store, 1, old)
        self.assertTrue(self.store.profile(1)[1]["copy_enabled"])

    def test_guard_reloads_current_state_before_saving(self):
        _, p = self.store.profile(1); p["max_leverage"] = 20
        changed = False
        @contextmanager
        def intervening(root, address):
            nonlocal changed
            with account_guard(root, address):
                if address == "account-a" and not changed:
                    changed = True
                    _, other = self.store.profile(1); other["max_leverage"] = 5
                    self.store.update_profile(1, other)
                yield
        with patch("core.profile_mutation.account_guard", intervening), self.assertRaises(StateConflict):
            save_profile_guarded(self.store, 1, p)
        self.assertEqual(self.store.profile(1)[1]["max_leverage"], 5)

    def test_invalid_owner_or_plain_profile_cannot_bypass_guard(self):
        _, p = self.store.profile(1)
        with self.assertRaises(StateConflict): save_profile_guarded(self.store, 1, dict(p))
        with self.assertRaises(StateConflict): save_profile_guarded(self.store, 2, p)

    def test_legacy_trading_keys_redirect_but_chat_and_ai_decisions_stay_available(self):
        for key in ("account", "addleader", "delleader:1", "toggleleader:1", "copy", "crypto", "stocks",
                    "risk", "risk:standard", "strategy", "strategy:swing", "leverage", "leverage:20",
                    "emergency", "estop:hold", "estop:close"):
            self.assertTrue(legacy_trading_callback(key), key)
        for key in ("menu", "clear_journal", "about", "leaders", "notify", "journal", "stats",
                    "analyse", "analyse:1", "analyse:custom", "report:1", "reports", "air:yes:123", "air:no:123"):
            self.assertFalse(legacy_trading_callback(key), key)

    def test_callback_redirect_precedes_obsolete_exchange_branches(self):
        source = Path(__file__).resolve().parents[1]/"desktop"/"main.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        callback = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "callback")
        redirect = next(n for n in callback.body if isinstance(n, ast.If) and isinstance(n.test, ast.Call)
                        and isinstance(n.test.func, ast.Name) and n.test.func.id == "legacy_trading_callback")
        emergency = next(n for n in callback.body if isinstance(n, ast.If) and "estop:" in ast.unparse(n.test))
        self.assertLess(redirect.lineno, emergency.lineno)
        self.assertTrue(any(isinstance(n, ast.Return) for n in redirect.body))
        self.assertIn("alert=True", ast.unparse(redirect))


if __name__ == "__main__": unittest.main()
