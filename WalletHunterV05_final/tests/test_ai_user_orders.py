from contextlib import closing
from copy import deepcopy
from decimal import Decimal
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from core.ai_user_orders import AiUserOrders
from core.legacy_route_calls import ai_order as legacy_ai_order
from core.order_precision import normalize_perp_size


ADDRESS = "0x" + "1" * 40
NOW = 20000 * 86400000 + 3600000 + 1000


def profile():
    return {"account": {"address": ADDRESS}, "leaders": ["wallet1", "wallet2"],
            "ai_slot_selected": True, "runtime": {"managed": []}}


def learning(coin="BTC", side="LONG", now=NOW):
    return {"status": "RESEARCH_MODEL", "model": {"version": 1}, "collector": {"status": "OK"},
            "predictions": [{"coin": coin, "direction": side, "status": "PENDING", "model_version": 1,
                             "probability_positive_net": .7, "created_ms": now - 500,
                             "decision_ms": now // 3600000 * 3600000 - 1,
                             "entry_ms": now // 3600000 * 3600000 + 900000,
                             "deadline_ms": now // 3600000 * 3600000 + 4499999}]}


class Public:
    address = ADDRESS
    base = "https://api.hyperliquid.xyz"

    def __init__(self):
        self.balance = 3000.
        self.capacity = 3000.
        self.mode_name = "unifiedAccount"
        self.price = 100.
        self.digits = 4
        self.max_leverage = 40
        self.live_positions = []
        self.open_orders = []
        self.calls = []

    def capital_snapshot(self):
        return SimpleNamespace(mode=self.mode_name, sizing_base_usdc=self.balance)

    def available_margin(self, dex):
        return self.capacity

    def positions(self, crypto, stocks):
        self.calls.append("positions")
        return deepcopy(self.live_positions)

    def frontend_open_orders(self, dex):
        return deepcopy(self.open_orders)

    def meta(self):
        return {"universe": [{"name": coin, "szDecimals": self.digits, "maxLeverage": self.max_leverage} for coin in ("BTC", "ETH")]}

    def mid(self, coin, dex):
        return self.price


class Signer:
    def __init__(self, public):
        self.public = public
        self.address = public.address
        self.base = public.base
        self.calls = []
        self.behavior = "filled"

    def submit_user_ioc(self, coin, is_buy, size, limit_price, leverage, cloid, *, expires_ms=None):
        self.calls.append((coin, is_buy, size, limit_price, leverage, cloid))
        if self.behavior == "timeout":
            raise TimeoutError("untrusted network secret")
        if self.behavior == "rejected":
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "rejected"}]}}}
        if self.behavior == "resting":
            self.public.open_orders = [{"coin": coin, "oid": 5}]
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 5}}]}}}
        quantity = normalize_perp_size(size / 2, self.public.digits) if self.behavior == "partial" else size
        position = {"coin": coin, "dex": "", "side": "LONG" if is_buy else "SHORT", "size": quantity,
                    "leverage": leverage, "margin_mode": "cross", "entry_price": limit_price,
                    "margin_used": quantity * limit_price / leverage}
        if self.behavior == "mismatch":
            position["size"] *= 2
        self.public.live_positions.append(position)
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {
            "oid": 5, "totalSz": str(quantity), "avgPx": str(limit_price)}}]}}}


class AiUserOrderTests(unittest.TestCase):
    def test_verified_manual_close_releases_only_own_reservation_and_survives_restart(self):
        proposal = self.proposal()
        self.decide(proposal)
        self.public.live_positions.clear()
        self.public.verify_ai_closed = lambda payload, result: {"status": "CLOSED_RECONCILED", "trade_ids": [5, 6]}
        self.service.reconcile_closed("139", self.profile, self.public, self.persist, NOW+2000)
        self.assertEqual(self.service.reserved_markets("139", ADDRESS), {})
        self.assertEqual(self.profile["runtime"]["ai_user_order_holds"], {})
        restored = AiUserOrders(self.temp.name)
        self.assertEqual(restored._get("139", ADDRESS, proposal["id"])["status"], "RELEASED")
        self.assertEqual(len(self.signer.calls), 1)

    def test_flat_without_closure_proof_or_wrong_network_never_releases(self):
        proposal = self.proposal()
        self.decide(proposal)
        self.public.live_positions.clear()
        self.service.reconcile_closed("139", self.profile, self.public, self.persist, NOW+2000)
        self.assertIn("BTC|", self.service.reserved_markets("139", ADDRESS))
        self.public.verify_ai_closed = lambda *args: {"status": "CLOSED_RECONCILED"}
        self.public.base = "https://api.hyperliquid-testnet.xyz"
        self.service.reconcile_closed("139", self.profile, self.public, self.persist, NOW+3000)
        self.assertIn("BTC|", self.service.reserved_markets("139", ADDRESS))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = AiUserOrders(self.temp.name, monotonic=lambda: 0,
                                    legacy_test_executor=legacy_ai_order)
        self.profile = profile()
        self.public = Public()
        self.signer = Signer(self.public)
        self.factories = 0
        self.persisted = []

    def factory(self):
        self.factories += 1
        self.assertTrue(self.profile["runtime"]["ai_user_order_holds"])
        self.assertTrue(self.persisted)
        return self.signer

    def persist(self):
        self.persisted.append(deepcopy(self.profile["runtime"]))

    def prepare(self, model=None):
        return self.service.prepare("139", self.profile, self.public, None, model or learning(), NOW)

    def proposal(self, model=None):
        result = self.prepare(model)
        self.assertEqual(len(result["pending"]), 1, result)
        return result["pending"][0]

    def decide(self, proposal, confirm=True, now=NOW+1000):
        return self.service.decide("139", proposal["id"], confirm, self.profile, self.public,
                                   self.factory, self.persist, now)

    def test_prepare_is_read_only_frozen_exact_order_form(self):
        p = self.proposal()
        data = p["payload"]
        self.assertEqual(self.factories, 0)
        self.assertEqual(self.signer.calls, [])
        self.assertEqual(self.profile["runtime"], {"managed": []})
        self.assertEqual(data["share_usdc"], 1000)
        self.assertEqual(data["entry_pct_of_share"], 10)
        self.assertEqual(data["leverage"], 40)
        self.assertEqual(data["margin_mode"], "cross")
        self.assertEqual(data["network"], "MAINNET")
        self.assertLessEqual(data["maximum_expected_margin_usdc"], 100)
        self.assertEqual(Decimal(data["size_text"]), Decimal(str(data["size"])))
        self.assertEqual(p["expires_ms"], NOW+90000)

    def test_missing_slot_and_too_many_sources_block_without_signing(self):
        self.profile["ai_slot_selected"] = False
        self.assertEqual(self.prepare()["reason"], "ai_slot_unavailable")
        self.profile["ai_slot_selected"] = True
        self.profile["leaders"].append("wallet3")
        self.assertEqual(self.prepare()["reason"], "too_many_wallets")
        self.assertEqual(self.factories, 0)

    def test_low_budget_never_forces_minimum_order(self):
        self.public.balance = 3
        result = self.prepare()
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["reason"], "minimum_notional")

    def test_ninety_deposit_uses_ten_percent_of_its_third_not_total(self):
        self.public.balance = 90
        data = self.proposal()["payload"]
        self.assertEqual(data["share_usdc"], 30)
        self.assertEqual(data["margin_cap_usdc"], 3)
        self.assertLessEqual(data["maximum_expected_margin_usdc"], 3)
        self.assertGreater(data["notional_usdc"], 10)
        self.assertEqual(data["leverage"], 40)

    def test_exchange_and_user_leverage_caps_still_apply(self):
        self.public.max_leverage = 25
        self.assertEqual(self.proposal()["payload"]["leverage"], 25)

    def test_position_intervention_hold_cannot_be_reopened_as_ai_entry(self):
        self.profile["runtime"]["ai_position_action_holds"] = {"BTC|": {"status": "UNKNOWN"}}
        self.assertEqual(self.prepare()["reason"], "instrument_owned_or_held")
        self.assertEqual(self.factories, 0)

    def test_missing_weak_future_stale_or_matured_signal_never_invented(self):
        for field, value in [("probability_positive_net", .59), ("probability_positive_net", float("nan")),
                             ("created_ms", NOW+1), ("created_ms", NOW-120001), ("status", "MATURED"),
                             ("coin", "xyz:INTC"), ("deadline_ms", NOW)]:
            candidate = learning(); candidate["predictions"][0][field] = value
            self.assertEqual(self.prepare(candidate)["pending"], [])
        self.assertEqual(self.prepare({"status": "WAITING_DATA"})["reason"], "model_unavailable")

    def test_existing_position_order_or_journal_pending_blocks(self):
        self.public.live_positions = [{"coin": "BTC", "dex": "", "size": 1}]
        self.assertEqual(self.prepare()["reason"], "position_conflict")
        self.public.live_positions = []
        self.public.open_orders = [{"coin": "BTC", "oid": 9}]
        self.assertEqual(self.prepare()["reason"], "open_order_conflict")
        self.public.open_orders = []
        self.service.journal.prepare(ADDRESS, "ETH|", {"action": "TEST"})
        self.assertEqual(self.prepare()["reason"], "execution_pending")

    def test_decline_no_public_reads_or_signer_and_no_same_signal_reoffer(self):
        p = self.proposal()
        result = self.service.decide("139", p["id"], False, {}, None, None, None, NOW+1000)
        self.assertEqual(result["status"], "DECLINED")
        self.assertEqual(self.factories, 0)
        self.assertEqual(self.prepare()["pending"], [])
        self.assertEqual(self.prepare()["reason"], "signal_already_handled")

    def test_expired_confirm_never_signs(self):
        result = self.decide(self.proposal(), now=NOW+90000)
        self.assertEqual(result["status"], "EXPIRED")
        self.assertEqual(self.factories, 0)

    def test_filled_once_verified_and_never_ordinary_copy_owned(self):
        p = self.proposal()
        result = self.decide(p)
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(len(self.signer.calls), 1)
        self.assertEqual(self.service.journal.owned(ADDRESS), {})
        self.assertEqual(self.profile["runtime"]["managed"], [])
        self.assertIn("BTC|", self.profile["runtime"]["ai_user_order_holds"])
        self.assertEqual(self.profile["runtime"]["ai_user_order_positions"]["BTC|"]["status"], "FILLED")
        self.assertEqual(self.decide(p), result)
        self.assertEqual(len(self.signer.calls), 1)

    def test_short_order_side_and_exact_ioc_payload(self):
        p = self.proposal(learning(side="SHORT"))
        result = self.decide(p)
        self.assertEqual(result["status"], "FILLED")
        coin, is_buy, size, price, leverage, cloid = self.signer.calls[0]
        self.assertFalse(is_buy)
        self.assertEqual((size, price, leverage, cloid), tuple(p["payload"][key] for key in ("size", "limit_price", "leverage", "cloid")))

    def test_partial_fill_explicit_and_remainder_never_retried(self):
        self.signer.behavior = "partial"
        p = self.proposal()
        result = self.decide(p)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["result"]["filled_size"], normalize_perp_size(p["payload"]["size"]/2, self.public.digits))
        self.decide(p)
        self.assertEqual(len(self.signer.calls), 1)

    def test_timeout_persists_unknown_and_account_wide_hold_after_restart(self):
        self.signer.behavior = "timeout"
        p = self.proposal()
        result = self.decide(p)
        self.assertEqual(result["status"], "UNKNOWN")
        self.service = AiUserOrders(self.temp.name, legacy_test_executor=legacy_ai_order)
        self.assertIn("BTC|", self.service.reserved_markets(None, ADDRESS))
        self.assertIn("BTC|", self.service.journal.pending(ADDRESS))
        self.assertEqual(self.decide(p)["status"], "UNKNOWN")
        self.assertEqual(len(self.signer.calls), 1)
        self.assertNotIn("secret", str(result))

    def test_unexpected_resting_or_position_mismatch_remains_unknown(self):
        for behavior in ("resting", "mismatch"):
            with tempfile.TemporaryDirectory() as root:
                service = AiUserOrders(root, legacy_test_executor=legacy_ai_order); public = Public(); signer = Signer(public); signer.behavior = behavior
                prof = profile()
                p = service.prepare("139", prof, public, None, learning(), NOW)["pending"][0]
                result = service.decide("139", p["id"], True, prof, public, lambda: signer, lambda: None, NOW+1000)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertIn("BTC|", service.reserved_markets(None, ADDRESS))

    def test_definite_rejection_verified_flat_releases_hold(self):
        self.signer.behavior = "rejected"
        result = self.decide(self.proposal())
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.profile["runtime"]["ai_user_order_holds"], {})
        self.assertEqual(self.service.journal.pending(ADDRESS), set())

    def test_failed_pre_submit_persistence_never_constructs_signer(self):
        p = self.proposal()
        def failed(): raise IOError("storage failure")
        result = self.service.decide("139", p["id"], True, self.profile, self.public, self.factory, failed, NOW+1000)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.factories, 0)
        self.assertIn("BTC|", self.service.reserved_markets(None, ADDRESS))

    def test_fill_survives_post_submit_json_persistence_failure(self):
        p = self.proposal()
        def persist():
            if self.persisted: raise IOError("second persistence failed")
            self.persist()
        result = self.service.decide("139", p["id"], True, self.profile, self.public, self.factory, persist, NOW+1000)
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(AiUserOrders(self.temp.name).reserved_markets(None, ADDRESS)["BTC|"]["status"], "FILLED")

    def test_fresh_price_balance_capacity_metadata_account_or_slot_changes_invalidate(self):
        for change in ("price", "balance", "capacity", "digits", "mode", "slot", "network", "new_position"):
            with tempfile.TemporaryDirectory() as root:
                service = AiUserOrders(root, legacy_test_executor=legacy_ai_order); public = Public(); prof = profile(); signer = Signer(public)
                p = service.prepare("139", prof, public, None, learning(), NOW)["pending"][0]
                if change == "price": public.price *= 1.01
                if change == "balance": public.balance = 2000
                if change == "capacity": public.capacity = 0
                if change == "digits": public.digits = 3
                if change == "mode": public.mode_name = "portfolioMargin"
                if change == "slot": prof["leaders"].pop()
                if change == "network": public.base = "https://api.hyperliquid-testnet.xyz"
                if change == "new_position": public.live_positions = [{"coin": "BTC", "size": 1}]
                result = service.decide("139", p["id"], True, prof, public, lambda: signer, lambda: None, NOW+1000)
                self.assertEqual(result["status"], "INVALIDATED", change)
                self.assertEqual(signer.calls, [])

    def test_foreign_uid_cannot_confirm_or_decline_or_read(self):
        p = self.proposal()
        self.assertEqual(self.service.summary("other", self.profile, NOW)["pending"], [])
        for confirm in (True, False):
            with self.assertRaisesRegex(ValueError, "proposal_not_found"):
                self.service.decide("other", p["id"], confirm, self.profile, self.public, self.factory, self.persist, NOW+1)
        self.assertEqual(self.factories, 0)

    def test_payload_corruption_fails_closed(self):
        p = self.proposal()
        with closing(sqlite3.connect(self.service.path)) as db:
            db.execute("UPDATE user_order_proposals SET payload=replace(payload,'\"leverage\":40','\"leverage\":41') WHERE id=?", (p["id"],))
            db.commit()
        with self.assertRaisesRegex(ValueError, "immutable_proposal_corrupt"):
            self.decide(p)
        self.assertEqual(self.factories, 0)

    def test_literal_boolean_required_and_pending_reused_unchanged(self):
        p = self.proposal()
        self.public.price = 900
        self.assertEqual(self.prepare()["pending"][0], p)
        for value in ("true", 1, None):
            with self.assertRaises(ValueError): self.decide(p, value)

    def test_summary_marks_expired_and_slot_invalidated_without_order(self):
        self.proposal()
        self.profile["ai_slot_selected"] = False
        result = self.service.summary("139", self.profile, NOW+1)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["history"][0]["status"], "INVALIDATED")
        self.assertEqual(self.factories, 0)

    def test_other_ai_position_reserved_margin_cannot_exceed_third(self):
        self.decide(self.proposal())
        self.public.live_positions[0]["margin_used"] = 999
        model = learning(coin="ETH", now=NOW+1000)
        result = self.service.prepare("139", self.profile, self.public, None, model, NOW+1000)
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["reason"], "ai_slot_budget_exhausted")

    def test_testnet_labeled_and_no_real_dependency_in_fake_tests(self):
        self.public.base = "https://api.hyperliquid-testnet.xyz"
        self.assertEqual(self.proposal()["payload"]["network"], "TESTNET")
        self.assertEqual(self.signer.calls, [])

    def test_real_runtime_holds_and_crypto_disable_are_respected(self):
        for name, value in [("manual_hold_keys", ["BTC|"]), ("ai_hold_keys", {"BTC|": {}}),
                            ("recovery_required", ["BTC|"]), ("paused_source_markets", {"wallet": ["BTC|"]}),
                            ("manual_actions", {"BTC|": {"status": "unknown"}})]:
            self.profile["runtime"] = {name: value}
            self.assertEqual(self.prepare()["reason"], "instrument_owned_or_held", name)
        self.profile["runtime"] = {}
        self.profile["crypto_enabled"] = False
        self.assertEqual(self.prepare()["reason"], "crypto_disabled")

    def test_profile_leverage_ceiling_is_part_of_frozen_configuration(self):
        self.profile["max_leverage"] = 5
        p = self.proposal()
        self.assertEqual(p["payload"]["leverage"], 5)
        self.profile["max_leverage"] = 10
        self.assertEqual(self.decide(p)["status"], "INVALIDATED")
        self.assertEqual(self.factories, 0)

    def test_monotonic_expiry_after_slow_reads_or_factory_never_submits(self):
        for stage in ("reads", "factory"):
            with tempfile.TemporaryDirectory() as root:
                clock = [0.]
                service = AiUserOrders(root, monotonic=lambda: clock[0], legacy_test_executor=legacy_ai_order)
                public, prof = Public(), profile()
                signer = Signer(public)
                p = service.prepare("139", prof, public, None, learning(), NOW)["pending"][0]
                if stage == "reads":
                    original = public.mid
                    def slow(*args):
                        clock[0] = 2.
                        return original(*args)
                    public.mid = slow
                    factory = lambda: signer
                else:
                    def factory():
                        clock[0] = 2.
                        return signer
                result = service.decide("139", p["id"], True, prof, public, factory, lambda: None, NOW+89000)
                self.assertEqual(signer.calls, [])
                self.assertEqual(result["status"], "INVALIDATED" if stage == "reads" else "UNKNOWN")

    def test_signing_account_mismatch_stays_unknown_without_sending(self):
        p = self.proposal()
        self.signer.address = "0x" + "2"*40
        result = self.decide(p)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.signer.calls, [])

    def test_cloid_proof_required_when_available(self):
        p = self.proposal()
        self.public.query_order_by_cloid = lambda _: {"status": "unknownOid"}
        self.assertEqual(self.decide(p)["status"], "UNKNOWN")

    def test_cloid_exact_order_and_ioc_limit_are_verified(self):
        p = self.proposal()
        data = p["payload"]
        self.public.query_order_by_cloid = lambda cloid: {"status": "order", "order": {"status": "filled", "order": {
            "oid": 5, "coin": "BTC", "cloid": cloid, "side": "B", "origSz": str(data["size"]), "limitPx": str(data["limit_price"])}}}
        self.assertEqual(self.decide(p)["status"], "FILLED")

    def test_fill_beyond_limit_is_not_claimed_successful(self):
        p = self.proposal()
        original = self.signer.submit_user_ioc
        def outside(coin, buy, size, limit, lev, cloid, **kwargs):
            return original(coin, buy, size, limit*1.1, lev, cloid, **kwargs)
        self.signer.submit_user_ioc = outside
        self.assertEqual(self.decide(p)["status"], "UNKNOWN")

    def test_expired_form_does_not_block_new_telegram_binding_forever(self):
        self.proposal()
        later = NOW + 3600000
        result = self.service.prepare("new-user", self.profile, self.public, None, learning(now=later), later)
        self.assertEqual(len(result["pending"]), 1)
        old = self.service.summary("139", self.profile, later)
        self.assertEqual(old["history"][0]["status"], "EXPIRED")

    def test_missing_canonical_context_fails_closed_without_legacy_or_signer(self):
        proposal = self.proposal()
        strict = AiUserOrders(self.temp.name)
        result = strict.decide("139", proposal["id"], True, self.profile, self.public,
                               self.factory, self.persist, NOW + 1000)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.factories, 0)
        self.assertEqual(self.signer.calls, [])


if __name__ == "__main__":
    unittest.main()
