"""Isolated API/account-lock races. Inherits no tests and never calls a network."""
from contextlib import closing, contextmanager
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_api_safety as fixtures
from core.ai_review import account_guard


ACCOUNT_C = "0x"+"c"*40


class ApiRaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ApiSafetyTests("test_dashboard_isolates_user_account_events_and_settings")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.server, self.store = self.fixture.server, self.fixture.store
        self.request = self.fixture.request
        # Race tests use synthetic credentials; authority has its own crypto tests.
        authority = patch.object(self.server, "verify_account_control", return_value={"account_type": "SYNTHETIC_TEST"})
        authority.start(); self.addCleanup(authority.stop)
        _, p = self.store.profile(1)
        p["leaders"] = fixtures.LEADERS[:2]
        self.store.update_profile(1, p)

    def account_body(self, address=ACCOUNT_C):
        return {"name": "Offline", "address": address, "private_key": "offline-not-a-key",
                "confirm_open_positions": True}

    def holds(self, status="complete"):
        _, p = self.store.profile(1)
        p["runtime"].update(ai_hold_keys={"BTC|": {"proposal": "one"}, "ETH|": {"proposal": "two"}},
                            manual_hold_keys=["BTC|", "ETH|"], manual_actions={"BTC|": {"status": status}})
        self.store.update_profile(1, p)

    def test_busy_account_blocks_every_scoped_mutation_without_state_changes(self):
        requests = [("PUT", "/api/settings", {"risk_mode": "conservative"}),
                    ("POST", "/api/copy", {"value": False, "confirm_open_positions": True}),
                    ("POST", "/api/wallet/1", {"value": False, "confirm_open_positions": True}),
                    ("POST", "/api/ai/slot", {"value": True}),
                    ("POST", "/api/wallet", {"address": fixtures.LEADERS[3]}),
                    ("DELETE", "/api/wallet/1", {"confirm_open_positions": True}),
                    ("PUT", "/api/account", self.account_body()),
                    ("DELETE", "/api/account", {"confirm_open_positions": True}),
                    ("POST", "/api/emergency", {"close_managed": False})]
        before = deepcopy(self.store.load())
        with account_guard(self.fixture.tmp, fixtures.ACCOUNT_A), patch.object(self.fixture.reader, "positions") as reads:
            for method, path, body in requests:
                with self.subTest(path=path):
                    result = self.request(method, path, body=body)
                    self.assertEqual(result.status_code, 409, result.text)
                    self.assertIn("in progress", result.text)
            reads.assert_not_called()
        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.fixture.accounts[fixtures.ACCOUNT_A].calls, [])

    def test_other_user_settings_are_not_blocked_by_first_account(self):
        with account_guard(self.fixture.tmp, fixtures.ACCOUNT_A):
            result = self.request("PUT", "/api/settings", uid=2, body={"language": "en"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.store.profile(1)[1]["language"], "ru")
        self.assertEqual(self.store.profile(2)[1]["language"], "en")

    def test_unbound_profile_still_has_its_own_mutation_lock(self):
        _, p = self.store.profile(1); p["account"] = None
        self.store.update_profile(1, p)
        with account_guard(self.fixture.tmp, "telegram-profile:1"):
            result = self.request("PUT", "/api/account", body=self.account_body(fixtures.ACCOUNT_A))
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIsNone(self.store.profile(1)[1]["account"])
        self.assertEqual(self.request("PUT", "/api/account", body=self.account_body(fixtures.ACCOUNT_A)).status_code, 200)

    def test_new_target_account_lock_also_protects_rebinding(self):
        with account_guard(self.fixture.tmp, ACCOUNT_C):
            result = self.request("PUT", "/api/account", body=self.account_body())
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.store.profile(1)[1]["account"]["address"], fixtures.ACCOUNT_A)
        # Acquired earlier guards must be released even when a later one fails.
        with account_guard(self.fixture.tmp, fixtures.ACCOUNT_A): pass

    def test_validation_failure_releases_all_guards(self):
        result = self.request("POST", "/api/wallet", body={"address": fixtures.LEADERS[0]})
        self.assertEqual(result.status_code, 400)
        result = self.request("PUT", "/api/settings", body={"language": "en"})
        self.assertEqual(result.status_code, 200, result.text)

    def test_profile_is_reloaded_inside_account_lock(self):
        changed = False
        @contextmanager
        def intervening(root, address):
            nonlocal changed
            with account_guard(root, address):
                if address == fixtures.ACCOUNT_A and not changed:
                    changed = True
                    _, p = self.store.profile(1)
                    p["leaders"] = fixtures.LEADERS[:3]
                    self.store.update_profile(1, p)
                yield
        with patch.object(self.server, "account_guard", intervening):
            result = self.request("POST", "/api/wallet", body={"address": fixtures.LEADERS[3]})
        self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(self.store.profile(1)[1]["leaders"], fixtures.LEADERS[:3])

    def test_changed_account_during_acquire_is_rejected_before_mutation(self):
        changed = False
        @contextmanager
        def intervening(root, address):
            nonlocal changed
            with account_guard(root, address):
                if address == fixtures.ACCOUNT_A and not changed:
                    changed = True
                    _, p = self.store.profile(1); p["account"]["address"] = ACCOUNT_C
                    self.store.update_profile(1, p)
                yield
        with patch.object(self.server, "account_guard", intervening):
            result = self.request("PUT", "/api/settings", body={"language": "en"})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.store.profile(1)[1]["language"], "ru")

    def test_repeated_pause_and_account_delete_are_idempotent_and_user_scoped(self):
        other = deepcopy(self.store.profile(2)[1])
        for _ in range(2):
            self.assertEqual(self.request("POST", "/api/copy", body={"value": False, "confirm_open_positions": True}).status_code, 200)
        self.assertFalse(self.store.profile(1)[1]["copy_enabled"])
        for _ in range(2):
            self.assertEqual(self.request("DELETE", "/api/account", body={"confirm_open_positions": True}).status_code, 200)
        self.assertIsNone(self.store.profile(1)[1]["account"])
        self.assertEqual(self.store.profile(2)[1], other)

    def test_case_only_account_update_preserves_position_ownership(self):
        _, p = self.store.profile(1)
        account_id = p["account"]["id"]
        p["account"]["address"] = "0x"+"A"*40
        p["runtime"]["managed"] = ["BTC|"]
        self.store.update_profile(1, p)
        result = self.request("PUT", "/api/account", body=self.account_body(fixtures.ACCOUNT_A))
        self.assertEqual(result.status_code, 200, result.text)
        p = self.store.profile(1)[1]
        self.assertEqual(p["account"]["id"], account_id)
        self.assertEqual(p["runtime"]["managed"], ["BTC|"])

    def test_resume_manual_unknown_keeps_both_holds_not_partial_resume(self):
        for state in ("unknown", "submitting", "cleanup", "cleanup_required"):
            self.holds(state)
            before = deepcopy(self.store.profile(1)[1]["runtime"])
            result = self.request("POST", "/api/ai/resume-copy", body={"market": "BTC|"})
            self.assertEqual(result.status_code, 409, result.text)
            self.assertEqual(self.store.profile(1)[1]["runtime"], before)

    def test_resume_atomically_clears_both_holds_and_keeps_other_markets(self):
        self.holds()
        for _ in range(2):
            result = self.request("POST", "/api/ai/resume-copy", body={"market": "BTC|"})
            self.assertEqual(result.status_code, 200, result.text)
        rt = self.store.profile(1)[1]["runtime"]
        self.assertEqual(list(rt["ai_hold_keys"]), ["ETH|"])
        self.assertEqual(rt["manual_hold_keys"], ["ETH|"])

    def test_resume_unknown_ai_operation_does_not_clear_manual_hold(self):
        self.holds()
        self.fixture.seed_proposal()
        with closing(self.server.ai_review.connect()) as db:
            db.execute("UPDATE proposals SET status='UNKNOWN'"); db.commit()
        result = self.request("POST", "/api/ai/resume-copy", body={"market": "BTC|"})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIn("BTC|", self.store.profile(1)[1]["runtime"]["manual_hold_keys"])

    def test_resume_unknown_copy_operation_does_not_clear_either_hold(self):
        self.holds()
        self.server.ai_review.execution_journal.prepare(fixtures.ACCOUNT_A, "BTC|", {"test": True})
        result = self.request("POST", "/api/ai/resume-copy", body={"market": "BTC|"})
        self.assertEqual(result.status_code, 409, result.text)
        rt = self.store.profile(1)[1]["runtime"]
        self.assertIn("BTC|", rt["ai_hold_keys"])
        self.assertIn("BTC|", rt["manual_hold_keys"])

    def test_resume_busy_returns409_without_clearing_holds(self):
        self.holds()
        with account_guard(self.fixture.tmp, fixtures.ACCOUNT_A):
            result = self.request("POST", "/api/ai/resume-copy", body={"market": "BTC|"})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIn("BTC|", self.store.profile(1)[1]["runtime"]["ai_hold_keys"])

    def test_emergency_pause_is_persisted_even_if_exchange_step_fails(self):
        with patch.object(self.server.engine, "emergency_stop", AsyncMock(side_effect=RuntimeError("offline failure"))):
            result = self.request("POST", "/api/emergency", body={"close_managed": True})
        self.assertEqual(result.status_code, 500, result.text)
        self.assertFalse(self.store.profile(1)[1]["copy_enabled"])
        self.assertTrue(self.store.profile(2)[1]["copy_enabled"])

    def test_emergency_holds_guard_across_exchange_await(self):
        async def action(*args):
            self.assertFalse(self.store.profile(1)[1]["copy_enabled"])
            with self.assertRaises(OSError):
                with account_guard(self.fixture.tmp, fixtures.ACCOUNT_A): pass
            return [SimpleNamespace(ok=False, error="fake incomplete close")]
        with patch.object(self.server.engine, "emergency_stop", action):
            result = self.request("POST", "/api/emergency", body={"close_managed": True})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["closed"])
        self.assertTrue(result.json()["closed_requested"])


if __name__ == "__main__": unittest.main()
