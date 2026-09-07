import copy
import math
import unittest

from core.ai_candidates import build_candidates


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.position = {"coin": "BTC", "dex": "", "side": "LONG", "size": 1,
                         "entry_price": 100, "mark_price": 95, "leverage": 10,
                         "margin_used": 10, "margin_mode": "isolated", "liquidation_price": 91}
        self.lifecycle = {"basis": "original_pre_intervention", "risk_capital_usdc": 10,
                          "realized_pnl_usdc": -2, "paid_costs_usdc": .1,
                          "source_wallet": "one", "evidence": "TEST verified lifecycle"}
        self.source = {"verified": True, "owner_count": 1, "wallet": "one", "slot_budget_usdc": 90,
                       "free_budget_usdc": 20, "additional_spent_usdc": 0, "injections_count": 0}
        self.costs = {"entry_fee_bps": 10, "exit_fee_bps": 10, "slippage_bps": 10,
                      "label": "Explicit test assumptions, not actual execution costs"}

    def build(self, position=None, lifecycle=None, source=None, assumptions=None, **kwargs):
        return build_candidates(position or self.position, lifecycle or self.lifecycle, source or self.source,
                                assumptions or self.costs, sz_decimals=kwargs.pop("sz_decimals", 3), **kwargs)

    def find(self, action, rows=None, available=True):
        return [r for r in rows or self.build() if r["action"] == action and r["available_for_research"] is available]

    def test_all_candidates_are_nonexecuting_and_not_probabilities(self):
        for c in self.build():
            self.assertFalse(c["executable"])
            self.assertIsNone(c["probability"])
            self.assertIn("calibrated_probability_unavailable", c["permission_blockers"])

    def test_hold_preserves_size_entry_liquidation_and_all_losses(self):
        c = self.find("HOLD")[0]
        self.assertEqual(c["post_position"]["size"], 1)
        self.assertEqual(c["post_position"]["entry_price"], 100)
        self.assertEqual(c["post_position"]["liquidation_price"], 91)
        self.assertEqual(c["fixed_basis_mark_pnl_usdc"], -7.1)
        self.assertEqual(c["fixed_basis_roe_pct"], -71)
        self.assertEqual(c["reserved_source_budget_usdc"], 0)

    def test_long_average_weighted_entry_and_costs_not_double_counted(self):
        c = self.find("AVERAGE")[-1]
        q, fill = c["quantity"], c["assumed_fill_price"]
        self.assertGreater(fill, self.position["mark_price"])
        self.assertAlmostEqual(c["post_position"]["entry_price"], (100+q*fill)/(1+q))
        self.assertEqual(c["post_lifecycle"]["realized_pnl_usdc"], -2)
        self.assertAlmostEqual(c["post_lifecycle"]["paid_costs_usdc"], .1+c["modelled_fee_usdc"])
        old = self.find("HOLD")[0]["fixed_basis_mark_pnl_usdc"]
        self.assertAlmostEqual(c["fixed_basis_mark_pnl_usdc"], old-c["modelled_fee_usdc"]-c["modelled_slippage_usdc"])
        self.assertLessEqual(c["reserved_source_budget_usdc"], 20)
        self.assertEqual(c["post_lifecycle"]["risk_capital_usdc"], 10)

    def test_short_average_has_adverse_fill_and_correct_weighted_entry(self):
        p = dict(self.position, side="SHORT", mark_price=105, liquidation_price=109)
        rows = self.build(position=p)
        c = self.find("AVERAGE", rows)[-1]
        q, fill = c["quantity"], c["assumed_fill_price"]
        self.assertLess(fill, 105)
        self.assertAlmostEqual(c["post_position"]["entry_price"], (100+q*fill)/(1+q))
        old = self.find("HOLD", rows)[0]["fixed_basis_mark_pnl_usdc"]
        self.assertAlmostEqual(c["fixed_basis_mark_pnl_usdc"], old-c["modelled_fee_usdc"]-c["modelled_slippage_usdc"])

    def test_reduce_keeps_prior_realized_loss_and_charges_adverse_fill_once(self):
        c = self.find("REDUCE")[0]
        self.assertEqual(c["quantity"], .25)
        self.assertEqual(c["post_position"]["size"], .75)
        self.assertAlmostEqual(c["post_lifecycle"]["realized_pnl_usdc"], -2+.25*(c["assumed_fill_price"]-100))
        old = self.find("HOLD")[0]["fixed_basis_mark_pnl_usdc"]
        self.assertAlmostEqual(c["fixed_basis_mark_pnl_usdc"], old-c["modelled_fee_usdc"]-c["modelled_slippage_usdc"])
        self.assertEqual(c["post_lifecycle"]["risk_capital_usdc"], 10)

    def test_short_reduce_buys_at_adverse_higher_price(self):
        rows = self.build(position=dict(self.position, side="SHORT", mark_price=105, liquidation_price=109))
        c = self.find("REDUCE", rows)[0]
        self.assertGreater(c["assumed_fill_price"], 105)
        self.assertAlmostEqual(c["post_lifecycle"]["realized_pnl_usdc"], -2-.25*(c["assumed_fill_price"]-100))

    def test_margin_addition_does_not_manufacture_positive_roe(self):
        rows = self.build()
        hold = self.find("HOLD", rows)[0]
        for c in self.find("ADD_MARGIN", rows):
            self.assertGreater(c["post_position"]["margin_used"], 10)
            self.assertEqual(c["fixed_basis_roe_pct"], hold["fixed_basis_roe_pct"])
            self.assertEqual(c["fixed_basis_mark_pnl_usdc"], hold["fixed_basis_mark_pnl_usdc"])
            self.assertEqual(c["post_position"]["size"], 1)

    def test_lower_leverage_uses_source_cash_but_does_not_change_pnl(self):
        rows = self.build(lower_leverages=(5,))
        c = self.find("LOWER_LEVERAGE", rows)[0]
        self.assertEqual(c["post_position"]["leverage"], 5)
        self.assertEqual(c["modelled_margin_change_usdc"], 9)
        self.assertEqual(c["reserved_source_budget_usdc"], 9)
        self.assertEqual(c["fixed_basis_roe_pct"], self.find("HOLD", rows)[0]["fixed_basis_roe_pct"])

    def test_cross_margin_does_not_offer_per_position_collateral_adjustments(self):
        rows = self.build(position=dict(self.position, margin_mode="cross"))
        self.assertEqual(self.find("LOWER_LEVERAGE", rows), [])
        self.assertEqual(self.find("ADD_MARGIN", rows), [])
        self.assertTrue(self.find("AVERAGE", rows))
        for c in self.find("ADD_MARGIN", rows, available=False):
            self.assertEqual(c["reason"], "isolated_margin_required_for_source_accounting")

    def test_all_post_action_liquidations_are_unknown_not_estimated_by_leverage(self):
        for c in self.build():
            if c["action"] != "HOLD":
                self.assertIsNone(c["post_position"]["liquidation_price"])
                self.assertFalse(c["new_liquidation_verified"])
                self.assertIn("post_action_liquidation_unknown", c["permission_blockers"])

    def test_all_cash_actions_use_remaining_cumulative_fifty_percent_cap(self):
        source = dict(self.source, additional_spent_usdc=42, free_budget_usdc=20)
        for c in self.build(source=source):
            self.assertLessEqual(c["reserved_source_budget_usdc"], 3)
            self.assertLessEqual(c["post_source_ledger"]["additional_spent_usdc"], 45)
            self.assertGreaterEqual(c["remaining_extra_cap_usdc"], 0)

    def test_own_slot_free_funds_bind_before_other_accounts_money(self):
        rows = self.build(source=dict(self.source, free_budget_usdc=2))
        for c in rows:
            self.assertLessEqual(c["reserved_source_budget_usdc"], 2)
            self.assertGreaterEqual(c["remaining_source_free_usdc"], 0)

    def test_four_capital_injections_block_averaging_margin_and_leverage_spend(self):
        rows = self.build(source=dict(self.source, injections_count=4))
        self.assertFalse(any(c["available_for_research"] and c["reserved_source_budget_usdc"] > 0 for c in rows))
        self.assertTrue(self.find("REDUCE", rows))
        self.assertTrue(self.find("HOLD", rows))

    def test_fourth_injection_is_available_but_next_counter_is_four(self):
        rows = self.build(source=dict(self.source, injections_count=3))
        c = self.find("AVERAGE", rows)[0]
        self.assertEqual(c["next_injections_count"], 4)
        self.assertEqual(c["post_source_ledger"]["injections_count"], 4)

    def test_reducing_does_not_recycle_unverified_released_margin(self):
        c = self.find("REDUCE")[0]
        self.assertLess(c["modelled_margin_change_usdc"], 0)
        self.assertEqual(c["remaining_source_free_usdc"], 20)
        self.assertEqual(c["post_source_ledger"]["additional_spent_usdc"], 0)

    def test_exact_ten_dollar_average_is_allowed_but_below_ten_is_not(self):
        p = dict(self.position, mark_price=100)
        costs = dict(self.costs, entry_fee_bps=0, exit_fee_bps=0, slippage_bps=0)
        source = dict(self.source, free_budget_usdc=1)
        row = self.find("AVERAGE", self.build(position=p, source=source, assumptions=costs, average_fractions=(1,)))[0]
        self.assertEqual(row["quantity"], .1)
        self.assertEqual(row["order_notional_usdc"], 10)
        below = self.build(position=p, source=dict(source, free_budget_usdc=.999), assumptions=costs, average_fractions=(1,))
        self.assertEqual(self.find("AVERAGE", below), [])

    def test_min_lot_and_tiny_deposit_never_round_up_to_exchange_minimum(self):
        p = dict(self.position, size=.000001, entry_price=100000, mark_price=95000,
                 margin_used=.01, liquidation_price=91000)
        source = dict(self.source, slot_budget_usdc=.001, free_budget_usdc=.0001)
        rows = self.build(position=p, source=source, sz_decimals=6)
        self.assertEqual(self.find("REDUCE", rows), [])
        self.assertEqual(self.find("AVERAGE", rows), [])

    def test_size_precision_is_truncated_not_rounded_to_overspend(self):
        for c in self.find("AVERAGE"):
            self.assertAlmostEqual(c["quantity"]*1000, round(c["quantity"]*1000))
            self.assertLessEqual(c["reserved_source_budget_usdc"], 20)

    def test_large_values_remain_finite_and_within_budget(self):
        p = dict(self.position, size=100000000, margin_used=1000000000)
        source = dict(self.source, slot_budget_usdc=1e10, free_budget_usdc=1e8)
        rows = self.build(position=p, source=source)
        for c in rows:
            self.assertTrue(math.isfinite(c["fixed_basis_mark_pnl_usdc"]))
            self.assertLessEqual(c["reserved_source_budget_usdc"], 1e8)

    def test_bad_ownership_and_missing_basis_rejected(self):
        for change in ({"verified": False}, {"owner_count": 2}, {"wallet": "other"}):
            with self.subTest(change=change), self.assertRaises(ValueError): self.build(source=dict(self.source, **change))
        with self.assertRaises(ValueError): self.build(lifecycle=dict(self.lifecycle, risk_capital_usdc=0))
        with self.assertRaises(ValueError): self.build(lifecycle=dict(self.lifecycle, evidence=""))

    def test_invalid_numeric_fields_and_policy_expansion_rejected(self):
        for value in (float("nan"), float("inf"), -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError): self.build(source=dict(self.source, free_budget_usdc=value))
        with self.assertRaises(ValueError): self.build(max_extra_fraction=.51)
        with self.assertRaises(ValueError): self.build(max_injections=5)
        with self.assertRaises(ValueError): self.build(average_fractions=(1.1,))
        with self.assertRaises(ValueError): self.build(lower_leverages=(1.5,))

    def test_input_dictionaries_are_not_mutated(self):
        originals = copy.deepcopy((self.position, self.lifecycle, self.source, self.costs))
        self.build()
        self.assertEqual((self.position, self.lifecycle, self.source, self.costs), originals)

    def test_identical_requested_variants_do_not_duplicate_cards(self):
        rows = self.build(average_fractions=(1, 1), margin_fractions=(1, 1), lower_leverages=(5, 5))
        self.assertEqual(len({c["id"] for c in rows}), len(rows))

    def test_changed_position_does_not_reuse_old_candidate_identifiers(self):
        old = self.find("AVERAGE")[0]["id"]
        changed = self.find("AVERAGE", self.build(position=dict(self.position, mark_price=96)))[0]["id"]
        self.assertNotEqual(old, changed)

    def test_required_leverage_collateral_rounds_up_then_checks_budget(self):
        p = dict(self.position, mark_price=95.000001)
        rows = self.build(position=p, lower_leverages=(5,))
        c = self.find("LOWER_LEVERAGE", rows)[0]
        self.assertEqual(c["reserved_source_budget_usdc"], 9.000001)
        rows = self.build(position=p, source=dict(self.source, free_budget_usdc=9), lower_leverages=(5,))
        self.assertEqual(self.find("LOWER_LEVERAGE", rows), [])


if __name__ == "__main__": unittest.main()
