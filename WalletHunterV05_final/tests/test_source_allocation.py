"""Offline source-third accounting and full-engine budget regressions.

ExchangeBoundary is the only exchange: all balances, fills and failures below
are synthetic. Journals and profile state live in disposable test directories.
"""
from copy import deepcopy
import unittest
from unittest.mock import patch

from core.source_allocation import AllocationError, SourceAllocationBook
from core.trading_engine import CopyEngine
from tests import test_engine_safety as fixture


A, B, ACCOUNT = fixture.SOURCE_A, fixture.SOURCE_B, fixture.ACCOUNT
C = "0x" + "e" * 40


def market(row):
    return CopyEngine._runtime_key(CopyEngine._key(row["coin"], row.get("dex")))


def source(wallet, margin, leverage=1, side="LONG"):
    return {"wallet": wallet, "margin": float(margin),
            "signed_notional": float(margin) * leverage * (1 if side == "LONG" else -1),
            "slot_budget": 1000.}


def target(margin, wallet=A, leverage=1, side="LONG", sources=None):
    return {"target_notional": float(margin) * leverage,
            "signed_notional": float(margin) * leverage * (1 if side == "LONG" else -1),
            "target_margin": float(margin), "capital_pct": float(margin) / 3000 * 100,
            "leverage": float(leverage), "side": side, "market_type": "CRYPTO",
            "entry_price": 100., "sources": deepcopy(sources or [source(wallet, margin, leverage, side)])}


def ownership(row, sources=None):
    return {"managed": True, "position": deepcopy(row), "size": row["size"], "side": row["side"],
            "source_targets": deepcopy(sources or [source(A, row["margin_used"], row["leverage"], row["side"])]),
            "attribution": "synthetic_strategy_targets_not_individual_exchange_fills"}


def book(rows=(), *, sources=None, records=None, managed=None, pending=None,
         balance=3000., excluded=(), uncertain=()):
    actual = {market(row): row for row in rows}
    owned = {key: ownership(row) for key, row in actual.items()} if records is None else records
    return SourceAllocationBook(balance, [A] if sources is None else sources, actual, owned,
                                set(owned) if managed is None else managed, pending or {}, excluded, uncertain)


class SourceAllocationTests(unittest.TestCase):
    def assert_account(self, allocation, wallet=A, committed=0., reserved=0., available=1000., limit=1000.):
        row = allocation.accounts[wallet]
        self.assertAlmostEqual(row.allocation_limit, limit)
        self.assertAlmostEqual(row.committed_margin, committed)
        self.assertAlmostEqual(row.reserved_margin, reserved)
        self.assertAlmostEqual(row.available_source_budget, available)

    def test_fixed_thirds_never_donate_empty_or_paused_source_share(self):
        for wallets in ([A], [A, B], [A, B, C]):
            with self.subTest(configured=len(wallets)):
                allocation = book(sources=wallets)
                for wallet in wallets:
                    self.assert_account(allocation, wallet)
        allocation = book([fixture.position(notional=800)], sources=[A, B])
        self.assert_account(allocation, A, committed=800, available=200)
        self.assert_account(allocation, B)

    def test_held_700_leaves_300_and_caps_only_new_market(self):
        allocation = book([fixture.position(notional=700)])
        self.assert_account(allocation, committed=700, available=300)
        self.assertAlmostEqual(allocation.cap("ETH|", target(1000))["target_margin"], 300)

    def test_unchanged_position_does_not_subtract_its_margin_twice(self):
        row = fixture.position(notional=700)
        self.assertEqual(book([row]).cap("BTC|", target(700), row), target(700))

    def test_existing_market_receives_only_its_own_current_credit(self):
        btc, eth = fixture.position(notional=700), fixture.position("ETH", 200)
        capped = book([btc, eth]).cap("BTC|", target(900), btc)
        self.assertAlmostEqual(capped["target_notional"], 800)

    def test_partial_shared_fill_normalizes_actual_margin_by_source_weights(self):
        row = fixture.position(notional=350)
        records = {"BTC|": ownership(row, [source(A, 700), source(B, 300)])}
        allocation = book([row], sources=[A, B], records=records)
        self.assert_account(allocation, A, committed=245, available=755)
        self.assert_account(allocation, B, committed=105, available=895)

    def test_shared_target_cap_preserves_contribution_ratio(self):
        held = fixture.position(notional=800)
        allocation = book([held], sources=[A, B])
        requested = target(1000, sources=[source(A, 500), source(B, 500)])
        capped = allocation.cap("ETH|", requested)
        self.assertAlmostEqual(capped["target_margin"], 400)
        self.assertEqual([s["margin"] for s in capped["sources"]], [200., 200.])

    def test_mixed_or_duplicate_source_weights_fail_closed(self):
        row = fixture.position(notional=350)
        for weights in ([source(A, 700), source(B, 300, side="SHORT")],
                        [source(A, 200), source(A, 150)]):
            with self.subTest(weights=weights):
                allocation = book([row], sources=[A, B], records={"BTC|": ownership(row, weights)})
                self.assertTrue(allocation.errors)
                with self.assertRaises(AllocationError): allocation.cap("ETH|", target(100))

    def test_removed_source_retains_commitment_and_blocks_new_risk(self):
        allocation = book([fixture.position(notional=700)], sources=[B])
        self.assert_account(allocation, A, committed=700, available=0, limit=0)
        with self.assertRaises(AllocationError): allocation.cap("ETH|", target(1000, wallet=B))

    def test_invalid_numeric_balance_and_position_values_never_become_zero(self):
        for value in (float("nan"), float("inf"), -1., True, "3000"):
            with self.subTest(balance=repr(value)):
                with self.assertRaises(AllocationError): book(balance=value)
        for field in ("margin_used", "size", "position_value", "leverage"):
            for value in (float("nan"), float("inf"), -1., True):
                with self.subTest(field=field, value=repr(value)):
                    row = fixture.position(notional=700)
                    record = ownership(row)
                    row[field] = value
                    allocation = book([row], records={"BTC|": record})
                    self.assertTrue(allocation.errors)
                    with self.assertRaises(AllocationError): allocation.cap("ETH|", target(100))

    def test_actual_margin_uses_conservative_max_and_only_missing_field_falls_back(self):
        for supplied, expected in ((800., 800.), (600., 700.), (None, 700.)):
            with self.subTest(supplied=supplied):
                row = fixture.position(notional=700)
                record = ownership(row)
                if supplied is None: row.pop("margin_used")
                else: row["margin_used"] = supplied
                allocation = book([row], records={"BTC|": record})
                self.assert_account(allocation, committed=expected, available=1000 - expected)

    def test_unknown_managed_blocks_but_valid_manual_position_is_not_guessed(self):
        row = fixture.position(notional=700)
        unknown = book([row], records={}, managed={"BTC|"})
        self.assertTrue(unknown.errors)
        with self.assertRaises(AllocationError): unknown.cap("ETH|", target(100))
        manual = book([row], records={}, managed=set())
        self.assertFalse(manual.errors)
        self.assert_account(manual)
        self.assertEqual(manual.cap("ETH|", target(1000)), target(1000))

    def test_pending_new_open_reserves_full_target_without_double_charging_observed_fill(self):
        for observed, expected in ((0, 900), (600, 900), (1200, 1200)):
            with self.subTest(observed=observed):
                rows = [fixture.position(notional=observed)] if observed else []
                allocation = book(rows, records={}, managed=set(),
                    pending={"BTC|": {"action": "RECONCILE", "before": None, "target": target(900)}})
                self.assertFalse(allocation.errors)
                self.assert_account(allocation, reserved=expected, available=max(0, 1000 - expected))

    def test_pending_increase_reserves_only_increment(self):
        row = fixture.position(notional=700)
        allocation = book([row], pending={"BTC|": {"action": "RECONCILE", "before": row, "target": target(900)}})
        self.assert_account(allocation, committed=700, reserved=200, available=100)

    def test_pending_partial_reduction_retains_before_budget_until_confirmation(self):
        before, current = fixture.position(notional=700), fixture.position(notional=500)
        allocation = book([current], pending={"BTC|": {"action": "RECONCILE", "before": before, "target": target(200)}})
        self.assert_account(allocation, committed=500, reserved=200, available=300)

    def test_pending_close_is_not_released_capital(self):
        row = fixture.position(notional=700)
        allocation = book([row], pending={"BTC|": {"action": "CLOSE", "before": row}})
        self.assert_account(allocation, committed=700, available=300)
        self.assertAlmostEqual(allocation.cap("ETH|", target(1000))["target_notional"], 300)

    def test_malformed_pending_and_unknown_source_history_fail_closed(self):
        for intent in ({}, {"action": "RECONCILE", "before": None, "target": {}},
                       {"action": "CLOSE", "before": fixture.position(notional=700)}):
            with self.subTest(intent=intent):
                allocation = book(pending={"BTC|": intent})
                self.assertTrue(allocation.errors)
                with self.assertRaises(AllocationError): allocation.cap("ETH|", target(100))

    def test_leverage_cap_cannot_turn_same_size_into_forced_partial_close(self):
        btc, eth = fixture.position(notional=800), fixture.position("ETH", 1000, leverage=10)
        with self.assertRaises(AllocationError): book([btc, eth]).cap("ETH|", target(1000, leverage=1), eth)

    def test_requested_small_reduction_cannot_become_large_collateral_driven_sale(self):
        row = fixture.position(notional=1000, leverage=10)
        with self.assertRaises(AllocationError):
            book([row], balance=300).cap("BTC|", target(900, leverage=1), row)

    def test_pending_market_and_uncertain_evidence_cannot_be_reused(self):
        allocation = book(pending={"BTC|": {"action": "RECONCILE", "before": None, "target": target(900)}})
        with self.assertRaises(AllocationError): allocation.cap("BTC|", target(950))
        with self.assertRaises(AllocationError): book(uncertain={"BTC|"}).cap("ETH|", target(100))

    def test_explicit_independent_ai_exclusion_does_not_claim_source_funds(self):
        row = fixture.position(notional=700)
        allocation = book([row], records={}, managed={"BTC|"}, excluded={"BTC|"})
        self.assertFalse(allocation.errors)
        self.assert_account(allocation)
        with self.assertRaises(AllocationError): allocation.cap("BTC|", target(800))

    def test_cap_is_stateless_inputs_and_provenance_are_unchanged(self):
        row = fixture.position(notional=700)
        actual, owned, spec = {"BTC|": row}, {"BTC|": ownership(row)}, target(1000)
        original = deepcopy((actual, owned, spec))
        allocation = SourceAllocationBook(3000., [A], actual, owned, {"BTC|"}, {})
        first = allocation.cap("ETH|", spec)
        self.assertEqual(first, allocation.cap("ETH|", spec))
        self.assertEqual((actual, owned, spec), original)
        self.assert_account(allocation, committed=700, available=300)
        self.assert_account(book())

    def test_crypto_and_xyz_have_separate_exact_market_accounting(self):
        rows = [fixture.position("BTC", 200), fixture.position("xyz:INTC", 500, dex="xyz")]
        allocation = book(rows)
        self.assertFalse(allocation.errors)
        self.assert_account(allocation, committed=700, available=300)
        self.assertEqual(market(rows[1]), "xyz:INTC|xyz")

    def test_fully_committed_and_overcommitted_source_has_no_new_capacity_or_forced_sale(self):
        for held in (1000., 1200.):
            with self.subTest(committed=held):
                row = fixture.position(notional=held)
                before = deepcopy(row)
                allocation = book([row])
                self.assertFalse(allocation.errors)
                self.assert_account(allocation, committed=held, available=0)
                with self.assertRaises(AllocationError): allocation.cap("ETH|", target(100))
                # Existing-market credit preserves an unchanged position even
                # after legitimate overcommit; it cannot finance another market.
                self.assertEqual(allocation.cap("BTC|", target(held), row), target(held))
                self.assertEqual(row, before)

    def test_invalid_source_margin_and_mismatching_saved_size_fail_closed(self):
        row = fixture.position(notional=700)
        for value in (float("nan"), float("inf"), -1., 0., 701.):
            with self.subTest(source_margin=repr(value)):
                record = ownership(row)
                record["source_targets"][0]["margin"] = value
                allocation = book([row], records={"BTC|": record})
                self.assertTrue(allocation.errors)
                with self.assertRaises(AllocationError): allocation.cap("ETH|", target(100))
        record = ownership(row)
        record["size"] = row["size"] + 1.
        allocation = book([row], records={"BTC|": record})
        self.assertTrue(allocation.errors)
        with self.assertRaises(AllocationError): allocation.cap("ETH|", target(100))

    def test_isolated_extra_margin_is_retained_on_unchanged_target_and_new_market(self):
        row = fixture.position(notional=1000, leverage=10)
        row.update(margin_used=700., margin_mode="isolated")
        record = ownership(row, [source(A, 100, leverage=10)])
        allocation = book([row], records={"BTC|": record})
        self.assert_account(allocation, committed=700, available=300)
        unchanged = target(100, leverage=10)
        self.assertEqual(allocation.cap("BTC|", unchanged, row), unchanged)
        self.assertAlmostEqual(allocation.cap("ETH|", target(1000))["target_margin"], 300.)
        self.assertAlmostEqual(row["margin_used"], 700.)


class SourceAllocationEngineTests(unittest.TestCase):
    # Compose fixture setup/helpers only; inheriting its TestCase would silently
    # rediscover every unrelated safety case in this targeted regression module.
    patch = fixture.EngineSafetyTests.patch
    runtime = fixture.EngineSafetyTests.runtime
    run_cycle = fixture.EngineSafetyTests.run_cycle
    trades = fixture.EngineSafetyTests.trades

    def setUp(self):
        fixture.EngineSafetyTests.setUp(self)
        self.client.cash = 3000.

    def seed_owned(self, row, sources=None):
        self.client.seed(row)
        self.client.leverages[CopyEngine._key(row["coin"], row.get("dex"))] = row["leverage"]
        _, profile = self.store.profile(1)
        self.runtime(managed=sorted(set(profile["runtime"].get("managed", [])) | {market(row)}))
        op = self.engine.journal.prepare(ACCOUNT, market(row), {
            "action": "OFFLINE_SOURCE_FIXTURE", "network": self.client.network})
        self.engine.journal.finish(op, {"ok": True}, ownership(row, sources))

    def margin(self):
        return sum(max(row["margin_used"], row["position_value"] / row["leverage"]) for row in self.client.rows.values())

    def restart(self):
        self.engine = CopyEngine(fixture.Reader(), self.store, self.settings)

    def test_held_position_consumes_source_capacity_for_new_market(self):
        self.seed_owned(fixture.position(notional=700))
        self.runtime(manual_hold_keys=["BTC|"])
        self.run_cycle([fixture.snapshot(positions=[fixture.position("ETH", 900)])])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["position_value"], 700)
        self.assertAlmostEqual(self.client.rows[("ETH", "")]["position_value"], 300)
        self.assertAlmostEqual(self.margin(), 1000)

    def test_unchanged_position_does_not_force_reduction_or_leverage_call(self):
        self.seed_owned(fixture.position(notional=700))
        self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=700)])])
        self.assertEqual(self.client.calls, [])

    def test_paused_wallet_does_not_use_or_donate_other_wallet_capacity(self):
        self.seed_owned(fixture.position(notional=700))
        self.patch(leaders=[A, B], leader_enabled={A: False, B: True})
        self.run_cycle([fixture.snapshot(A), fixture.snapshot(B, [fixture.position("ETH", 1000)])])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["position_value"], 700)
        self.assertAlmostEqual(self.client.rows[("ETH", "")]["position_value"], 1000)

    def test_removed_source_does_not_release_held_capital_to_replacement(self):
        self.seed_owned(fixture.position(notional=700))
        self.patch(leaders=[B])
        self.run_cycle([fixture.snapshot(B, [fixture.position("ETH", 1000)])])
        self.assertEqual(self.client.calls, [])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["position_value"], 700)

    def test_unknown_managed_origin_blocks_new_market(self):
        self.client.seed(fixture.position(notional=700))
        # Hold the source-unknown exposure; ordinary absent-leader exits are a
        # separate lifecycle policy, not the P1.2 allocation contract.
        self.runtime(managed=["BTC|"], manual_hold_keys=["BTC|"])
        self.run_cycle([fixture.snapshot(positions=[fixture.position("ETH", 1000)])])
        self.assertEqual(self.client.calls, [])

    def test_manual_external_position_is_not_arbitrarily_assigned_to_source(self):
        self.client.seed(fixture.position(notional=700))
        self.run_cycle([fixture.snapshot(positions=[fixture.position("ETH", 1000)])])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["position_value"], 700)
        self.assertAlmostEqual(self.client.rows[("ETH", "")]["position_value"], 1000)
        self.assertNotIn("BTC|", self.engine.journal.owned(ACCOUNT))

    def test_failed_reduction_does_not_release_capacity_for_next_market(self):
        self.seed_owned(fixture.position(notional=800))
        with patch.object(self.client, "submit_copy_ioc", side_effect=RuntimeError("Synthetic rejected reduction")):
            self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=200), fixture.position("ETH", 800)])])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["position_value"], 800)
        self.assertLessEqual(self.client.rows.get(("ETH", ""), {}).get("margin_used", 0), 200.00001)
        self.assertIn("BTC|", self.engine.journal.pending(ACCOUNT))
        self.assertLessEqual(self.margin(), 1000.00001)

    def test_confirmed_partial_reduction_releases_only_observed_amount(self):
        self.seed_owned(fixture.position(notional=800))
        submit = self.client.submit_copy_ioc
        def partial(coin, buy, size, limit, reduce_only, cloid, dex='', *, expires_ms):
            self.client.fill_fraction = .5 if reduce_only else 1.
            return submit(coin, buy, size, limit, reduce_only, cloid, dex, expires_ms=expires_ms)
        with patch.object(self.client, "submit_copy_ioc", side_effect=partial):
            self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=200), fixture.position("ETH", 800)])])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["position_value"], 500)
        self.assertAlmostEqual(self.client.rows[("ETH", "")]["position_value"], 500)
        self.assertAlmostEqual(self.margin(), 1000)

    def test_confirmed_reduction_can_release_budget_in_same_cycle(self):
        self.seed_owned(fixture.position(notional=800))
        self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=200), fixture.position("ETH", 800)])])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["position_value"], 200)
        self.assertAlmostEqual(self.client.rows[("ETH", "")]["position_value"], 800)
        self.assertAlmostEqual(self.margin(), 1000)

    def test_growth_before_reduction_waits_until_next_confirmed_snapshot(self):
        self.seed_owned(fixture.position(notional=800))
        desired = [fixture.snapshot(positions=[fixture.position("ETH", 800), fixture.position(notional=200)])]
        self.run_cycle(desired)
        self.assertAlmostEqual(self.client.rows[("ETH", "")]["position_value"], 200)
        self.assertLessEqual(self.margin(), 1000.00001)
        self.run_cycle(desired)
        self.assertAlmostEqual(self.client.rows[("ETH", "")]["position_value"], 800)
        self.assertAlmostEqual(self.margin(), 1000)

    def test_unknown_open_reservation_survives_engine_restart_and_no_retry(self):
        self.client.fail_after_submit = True
        self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=700), fixture.position("ETH", 300)])])
        self.assertIn("BTC|", self.engine.journal.pending(ACCOUNT))
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["position_value"], 700)
        self.restart()
        self.client.calls.clear()
        self.run_cycle([fixture.snapshot(positions=[fixture.position("ETH", 1000)])])
        self.assertFalse(any(call[1] == ("BTC", "") for call in self.trades()))
        self.assertLessEqual(self.client.rows.get(("ETH", ""), {}).get("margin_used", 0), 300.00001)
        self.assertLessEqual(self.margin(), 1000.00001)

    def test_fresh_price_cannot_turn_unknown_provenance_reduction_into_add(self):
        self.client.seed(fixture.position(notional=100))
        self.runtime(managed=["BTC|"])
        self.client.prices[("BTC", "")] = 50.
        self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=90)])])
        self.assertEqual(self.client.calls, [])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["size"], 1.)

    def test_unknown_source_ai_intervention_hold_cannot_free_copy_budget(self):
        self.client.seed(fixture.position(notional=700))
        self.runtime(managed=["BTC|"], ai_position_action_holds={"BTC|": {"status": "FILLED"}})
        self.run_cycle([fixture.snapshot(positions=[fixture.position("ETH", 1000)])])
        self.assertEqual(self.client.calls, [])

    def test_fresh_owned_read_failure_never_overwrites_old_provenance_or_executes(self):
        self.seed_owned(fixture.position(notional=700))
        original = self.engine.journal.owned(ACCOUNT)
        with patch.object(self.engine.journal, "owned", side_effect=[deepcopy(original), RuntimeError("Synthetic journal read failure")]):
            self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=600)])])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.engine.journal.owned(ACCOUNT), original)
        self.assertFalse(self.engine.journal.pending(ACCOUNT))

    def test_leverage_only_snapshot_preserves_actual_extra_collateral_for_next_market(self):
        self.seed_owned(fixture.position("xyz:INTC", 200, dex="xyz"))
        self.seed_owned(fixture.position(notional=1000, leverage=10))
        self.runtime(manual_hold_keys=["xyz:INTC|xyz"])
        change = self.client.set_leverage
        def extra_margin(coin, leverage, dex=""):
            result = change(coin, leverage, dex)
            if coin == "BTC": self.client.rows[("BTC", "")]["margin_used"] = 650.
            return result
        with patch.object(self.client, "set_leverage", side_effect=extra_margin):
            self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=1000, leverage=2), fixture.position("ETH", 500)])])
        self.assertEqual(self.client.rows[("BTC", "")]["leverage"], 2)
        self.assertAlmostEqual(self.client.rows[("ETH", "")]["position_value"], 150)
        self.assertAlmostEqual(self.margin(), 1000)
        record = self.engine.journal.owned(ACCOUNT)["BTC|"]
        self.assertAlmostEqual(record["position"]["margin_used"], 650)

    def test_leverage_ack_without_matching_snapshot_remains_unknown(self):
        self.seed_owned(fixture.position(notional=1000, leverage=10))
        original = self.engine.journal.owned(ACCOUNT)
        def acknowledge_only(coin, leverage, dex=""):
            self.client.calls.append(("leverage", (coin, dex), leverage))
            return {"ok": True}
        with patch.object(self.client, "set_leverage", side_effect=acknowledge_only):
            self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=1000, leverage=2)])])
        self.assertEqual(self.trades(), [])
        self.assertIn("BTC|", self.engine.journal.pending(ACCOUNT))
        self.assertEqual(self.engine.journal.owned(ACCOUNT), original)
        self.assertEqual(self.client.rows[("BTC", "")]["leverage"], 10)

    def test_invalid_post_leverage_margin_remains_unknown_and_blocks_reuse(self):
        self.seed_owned(fixture.position(notional=1000, leverage=10))
        original = self.engine.journal.owned(ACCOUNT)
        change = self.client.set_leverage
        def invalid_margin(coin, leverage, dex=""):
            result = change(coin, leverage, dex)
            self.client.rows[("BTC", "")]["margin_used"] = float("nan")
            return result
        with patch.object(self.client, "set_leverage", side_effect=invalid_margin):
            self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=1000, leverage=2), fixture.position("ETH", 500)])])
        self.assertIn("BTC|", self.engine.journal.pending(ACCOUNT))
        self.assertEqual(self.engine.journal.owned(ACCOUNT)["BTC|"], original["BTC|"])
        self.assertNotIn(("ETH", ""), self.client.rows)
        self.assertEqual(self.trades(), [])

    def test_reduction_with_lower_leverage_sells_only_requested_size_at_old_leverage(self):
        self.seed_owned(fixture.position("ETH", 900))
        self.seed_owned(fixture.position(notional=1000, leverage=10))
        self.runtime(manual_hold_keys=["ETH|"])
        # The third has only 100 for BTC after the held ETH commitment. The
        # requested 10% size reduction must not turn into a 90% liquidation.
        self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=900, leverage=1)])])
        self.assertEqual(self.client.rows[("BTC", "")]["leverage"], 10)
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["position_value"], 900)
        self.assertEqual(self.client.calls, [("reduce", ("BTC", ""), 1.)])

    def test_unfilled_other_source_reservation_reduces_account_available_margin(self):
        self.client.cash = 300.
        self.patch(leaders=[A, B])
        pending_target = target(80)
        pending_target["sources"][0]["slot_budget"] = 100.
        self.engine.journal.prepare(ACCOUNT, "BTC|", {"action": "RECONCILE", "before": None, "target": pending_target})
        with patch.object(self.client, "available_margin", return_value=100.):
            self.run_cycle([fixture.snapshot(A), fixture.snapshot(B, [fixture.position("ETH", 300)])])
        self.assertEqual(self.client.calls, [])
        self.assertNotIn(("ETH", ""), self.client.rows)
        self.assertIn("BTC|", self.engine.journal.pending(ACCOUNT))

    def test_paper_position_source_targets_and_budget_survive_restart(self):
        self.settings.auto_trading = False
        self.run_cycle([fixture.snapshot(positions=[fixture.position(notional=700)])])
        _, profile = self.store.profile(1)
        saved = profile["runtime"]["paper_runtime"]["positions"]["BTC|"]
        self.assertEqual(saved["source_targets"][0]["wallet"], A)
        profile["runtime"]["paper_runtime"]["manual_hold_keys"] = ["BTC|"]
        self.store.update_runtime(1, profile["runtime"])
        self.restart()
        self.run_cycle([fixture.snapshot(positions=[fixture.position("ETH", 1000)])])
        _, profile = self.store.profile(1)
        positions = profile["runtime"]["paper_runtime"]["positions"]
        self.assertAlmostEqual(positions["BTC|"]["position_value"], 700)
        self.assertAlmostEqual(positions["ETH|"]["position_value"], 300)
        self.assertEqual(positions["BTC|"]["source_targets"], saved["source_targets"])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.engine.journal.owned(ACCOUNT), {})

    def test_live_held_budget_and_provenance_survive_new_storage_and_engine(self):
        self.seed_owned(fixture.position(notional=700))
        self.runtime(manual_hold_keys=["BTC|"])
        original = self.engine.journal.owned(ACCOUNT)["BTC|"]
        self.store = fixture.Storage(self.directory.name, self.key)
        self.restart()
        self.run_cycle([fixture.snapshot(positions=[fixture.position("ETH", 1000)])])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["position_value"], 700.)
        self.assertAlmostEqual(self.client.rows[("ETH", "")]["position_value"], 300.)
        self.assertAlmostEqual(self.margin(), 1000.)
        self.assertEqual(self.engine.journal.owned(ACCOUNT)["BTC|"], original)
        self.assertFalse(any(call[1] == ("BTC", "") for call in self.client.calls))

    def test_source_owned_ai_intervention_hold_keeps_its_committed_budget(self):
        self.seed_owned(fixture.position(notional=700))
        original = self.engine.journal.owned(ACCOUNT)["BTC|"]
        self.runtime(ai_position_action_holds={"BTC|": {"status": "FILLED"}})
        self.run_cycle([fixture.snapshot(positions=[fixture.position("ETH", 1000)])])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["position_value"], 700.)
        self.assertAlmostEqual(self.client.rows[("ETH", "")]["position_value"], 300.)
        self.assertEqual(self.engine.journal.owned(ACCOUNT)["BTC|"], original)
        self.assertAlmostEqual(self.margin(), 1000.)

    def test_pending_intents_are_strictly_scoped_to_requested_account(self):
        other_account = "0x" + "d" * 40
        first = {"action": "RECONCILE", "before": None, "target": target(700)}
        second = {"action": "RECONCILE", "before": None, "target": target(900, wallet=B)}
        self.engine.journal.prepare(ACCOUNT, "BTC|", first)
        operation = self.engine.journal.prepare(other_account, "BTC|", second)
        self.engine.journal.finish(operation, {"ok": False, "error": "Synthetic uncertain order"})
        self.assertEqual(self.engine.journal.pending_intents(ACCOUNT.upper()), {"BTC|": first})
        self.assertEqual(self.engine.journal.pending_intents(other_account), {"BTC|": second})
        self.assertEqual(self.engine.journal.pending_intents(C), {})


if __name__ == "__main__":
    unittest.main()
