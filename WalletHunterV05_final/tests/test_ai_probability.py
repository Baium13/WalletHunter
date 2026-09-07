"""Pure synthetic evidence: no network, storage, live orders or gate bypass."""
from dataclasses import replace
from copy import deepcopy
import unittest

from core.ai_probability import DAY_MS, ProbabilityPolicy, evaluate_probability, wilson_interval


POLICY = ProbabilityPolicy("AVERAGE", "extra_25pct", "roe_-50_to_-60", "fixed-24h-v1", "costs-v1",
                           registered_ms=80*DAY_MS, split_ms=100*DAY_MS,
                           registration_evidence="synthetic-preregistration-fixture")
NOW = 150*DAY_MS


def row(index, *, test=False, success=True, net=None):
    signal = (100 if test else 10)*DAY_MS + index*60_000
    net = (5. if success else -5.) if net is None else net
    return {"id": f"{'test' if test else 'train'}-{index}",
            "account": f"account-{'test' if test else 'train'}-{index}", "market": "BTC|",
            "action": POLICY.action, "action_variant": POLICY.action_variant,
            "risk_bucket": POLICY.risk_bucket, "scenario_id": POLICY.scenario_id,
            "cost_model_id": POLICY.cost_model_id, "evidence_id": f"evidence-{'test' if test else 'train'}-{index}",
            "calibration_eligible": True, "costs_included": True,
            "risk_basis": "original_pre_intervention", "signal_ms": signal,
            "features_asof_ms": signal-1000, "created_ms": signal-1000,
            "deadline_ms": signal+DAY_MS, "observed_until_ms": signal+DAY_MS,
            "labelled_ms": signal+DAY_MS+1000,
            "target_roe_pct": 3., "loss_roe_pct": -120.,
            "status": "TARGET" if success else "HORIZON", "success": success,
            "gross_pnl_usdc": net+.2, "net_pnl_usdc": net, "risk_capital_usdc": 100.,
            "costs": {"fees_usdc": .1, "slippage_usdc": .05, "funding_usdc": .05}}


def records(train_wins=40, test_wins=40):
    return ([row(i, success=i<train_wins) for i in range(50)] +
            [row(i, test=True, success=i<test_wins) for i in range(50)])


class ProbabilityTests(unittest.TestCase):
    def evaluate(self, rows, policy=POLICY):
        return evaluate_probability(rows, policy, now_ms=NOW)

    def test_missing_data_has_no_invented_probability(self):
        result = self.evaluate([])
        self.assertIsNone(result["probability"])
        self.assertFalse(result["eligible"])
        self.assertFalse(result["ready_for_live_trading"])

    def test_strong_matured_evidence_passes_only_research_gate(self):
        result = self.evaluate(records())
        self.assertTrue(result["eligible"], result)
        self.assertFalse(result["ready_for_live_trading"])
        self.assertEqual((result["train_samples"], result["test_samples"]), (50, 50))
        self.assertAlmostEqual(result["probability"], .8)
        self.assertGreater(result["lower_bound"], .6)
        self.assertAlmostEqual(result["training_baseline_probability"], .8)
        self.assertAlmostEqual(result["heldout_brier"], .16)

    def test_raw_60_percent_is_not_a_confident_60_percent_lower_bound(self):
        result = self.evaluate(records(30, 30))
        self.assertAlmostEqual(result["probability"], .6)
        self.assertLess(result["lower_bound"], .6)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "heldout_lower_bound_below_required_probability")

    def test_training_holdout_drift_blocks_readiness(self):
        result = self.evaluate(records(50, 40))
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "training_holdout_rate_drift")

    def test_high_win_rate_with_negative_net_expectancy_does_not_pass(self):
        values = records()
        for r in values[90:]:
            r.update(net_pnl_usdc=-100., gross_pnl_usdc=-99.8)
        result = self.evaluate(values)
        self.assertGreater(result["lower_bound"], .6)
        self.assertLess(result["mean_net_roe_pct"], 0)
        self.assertFalse(result["eligible"])

    def test_hold_never_validates_averaging_or_reduction(self):
        values = records()
        for r in values: r["action"] = "HOLD"
        for action in ("AVERAGE", "REDUCE"):
            result = self.evaluate(values, replace(POLICY, action=action))
            self.assertIsNone(result["probability"])
            self.assertFalse(result["eligible"])
            self.assertEqual(result["reason"], "no_matching_action_risk_samples")

    def test_variants_risk_buckets_and_cost_models_cannot_be_pooled(self):
        for field in ("action_variant", "risk_bucket", "scenario_id", "cost_model_id"):
            values = records()
            for r in values: r[field] = "different"
            with self.subTest(field=field):
                self.assertFalse(self.evaluate(values)["eligible"])

    def test_future_and_incomplete_forward_labels_are_excluded(self):
        for change in ({"labelled_ms": NOW+1}, {"observed_until_ms": 100*DAY_MS},
                       {"features_asof_ms": NOW}, {"created_ms": NOW}, {"calibration_eligible": False}):
            values = records()
            for r in values[50:]: r.update(change)
            with self.subTest(change=change):
                result = self.evaluate(values)
                self.assertEqual(result["test_samples"], 0)
                self.assertIsNone(result["probability"])

    def test_early_target_requires_full_forward_horizon_before_admission(self):
        values = records(50, 50)
        for r in values[50:]:
            r.update(observed_until_ms=r["signal_ms"]+3_600_000,
                     labelled_ms=r["signal_ms"]+3_600_001)
        result = self.evaluate(values)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["test_samples"], 0)

    def test_full_24_hour_train_label_purge_is_enforced(self):
        values = records()
        for r in values[:50]:
            end = POLICY.split_ms-DAY_MS+1
            r.update(signal_ms=end-DAY_MS, features_asof_ms=end-DAY_MS,
                     created_ms=end-DAY_MS, deadline_ms=end,
                     observed_until_ms=end, labelled_ms=end)
        result = self.evaluate(values)
        self.assertEqual(result["train_samples"], 0)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["rejected"]["24h_purge_or_unavailable_training_label"], 50)

    def test_full_24_hour_purge_exact_boundary_is_accepted(self):
        values = records()
        for r in values[:50]:
            end = POLICY.split_ms-DAY_MS
            r.update(signal_ms=end-DAY_MS, features_asof_ms=end-DAY_MS,
                     created_ms=end-DAY_MS, deadline_ms=end,
                     observed_until_ms=end, labelled_ms=end)
        self.assertTrue(self.evaluate(values)["eligible"])

    def test_overlapping_market_account_groups_cannot_cross_split(self):
        values = records()
        for i, r in enumerate(values[50:]): r["account"] = values[i]["account"]
        result = self.evaluate(values)
        self.assertEqual(result["test_samples"], 0)
        self.assertEqual(result["rejected"]["train_test_group_leakage"], 50)

    def test_many_same_group_outcomes_do_not_manufacture_sample_count(self):
        values = records()
        for r in values[50:]: r["account"] = "one-account"
        result = self.evaluate(values)
        self.assertEqual(result["test_samples"], 1)
        self.assertFalse(result["eligible"])

    def test_duplicate_ids_are_deduplicated_and_conflicting_labels_block(self):
        values = records()
        result = self.evaluate(values+deepcopy(values))
        self.assertEqual(result["test_samples"], 50)
        changed = deepcopy(values[-1])
        changed.update(success=True, status="TARGET", net_pnl_usdc=5, gross_pnl_usdc=5.2)
        result = self.evaluate(values+[changed])
        self.assertEqual(result["reason"], "conflicting_duplicate_evidence")
        self.assertFalse(result["eligible"])

    def test_missing_or_inconsistent_costs_and_bad_success_labels_are_excluded(self):
        for change in ({"costs_included": False}, {"net_pnl_usdc": 1000},
                       {"status": "AMBIGUOUS"}, {"net_pnl_usdc": float("nan")},
                       {"risk_basis": "post_action_margin"}):
            values = records()
            for r in values[50:]: r.update(change)
            with self.subTest(change=change):
                self.assertFalse(self.evaluate(values)["eligible"])

    def test_insufficient_groups_never_emit_precise_odds(self):
        result = self.evaluate(records()[:-1])
        self.assertEqual(result["test_samples"], 49)
        self.assertIsNone(result["probability"])

    def test_policy_cannot_disable_purge_or_registration_or_uncertainty(self):
        for change in ({"purge_ms": DAY_MS-1}, {"horizon_ms": DAY_MS-1},
                       {"registered_ms": POLICY.split_ms}, {"min_test_groups": 1},
                       {"min_probability": .5}, {"confidence": .9}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.evaluate(records(), replace(POLICY, **change))

    def test_wilson_bounds_and_sample_validation(self):
        lower, upper = wilson_interval(0, 50)
        self.assertAlmostEqual(lower, 0)
        self.assertLess(upper, .1)
        lower, upper = wilson_interval(50, 50)
        self.assertGreater(lower, .9)
        self.assertAlmostEqual(upper, 1)
        for args in ((1, 0), (51, 50), (-1, 50), (1.1, 50)):
            with self.assertRaises(ValueError): wilson_interval(*args)

    def test_evaluator_does_not_mutate_source_records(self):
        values = records()
        before = deepcopy(values)
        self.evaluate(values)
        self.assertEqual(values, before)


if __name__ == "__main__":
    unittest.main()
