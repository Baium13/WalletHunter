import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace

from core.ai_outcomes import AiOutcomes, OutcomeCosts, OutcomePolicy, evaluate_snapshot


class CounterfactualTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AiOutcomes(self.tmp.name)
        # User-specified +3%/-120% and requested 24h are explicit inputs, not
        # module defaults. Small bars let tests cover a full path inexpensively.
        self.policy = OutcomePolicy(3, -120, 24*3600000, 60000, 120000)
        self.costs = OutcomeCosts(0, 0, 0, 0)
        self.snapshot = {
            "owner_id": "1", "source_review_id": "review-1", "source_wallet": "verified-source-id",
            "market": "xyz:INTC|xyz", "action": "HOLD", "side": "LONG",
            "signal_ms": 0, "features_asof_ms": 0, "quote_asof_ms": 0,
            "features": {"rsi14": 30, "model_version": "rules-test"},
            "risk_basis": "original_pre_intervention", "risk_capital_usdc": 10,
            "size": 1, "entry_price": 100, "reference_price": 95,
            "realized_pnl_usdc": 0, "paid_costs_usdc": 0, "liquidation_price": 91,
        }

    def bar(self, start=0, o=95, h=96, l=94, c=95, duration=60000):
        return {"t": start, "T": start+duration-1, "o": o, "h": h, "l": l, "c": c}

    def run_path(self, bars, now=60000, snapshot=None, policy=None, costs=None):
        return evaluate_snapshot(snapshot or self.snapshot, policy or self.policy,
                                 costs or self.costs, bars, now)

    def test_explicit_policy_has_no_live_permission_or_probability(self):
        with self.assertRaises(TypeError): OutcomePolicy()
        self.store.create("a", self.snapshot, self.policy, self.costs)
        result = self.store.summary()
        self.assertIsNone(result["probability"])
        self.assertFalse(result["ready_for_live_trading"])

    def test_target_is_fixed_original_risk_capital_not_price_percent(self):
        r = self.run_path([self.bar(h=101, c=100)])
        self.assertEqual(r["status"], "TARGET")
        self.assertAlmostEqual(r["net_pnl_usdc"], .3)
        self.assertAlmostEqual(r["roe_pct"], 3)
        self.assertAlmostEqual(r["exit_price"], 100.3)

    def test_exact_float_target_boundary_is_not_missed(self):
        r = self.run_path([self.bar(h=100.3, c=100)])
        self.assertEqual(r["status"], "TARGET")
        self.assertAlmostEqual(r["roe_pct"], 3)
        self.assertEqual(r["event_time_bounds_ms"], [0, 60000])

    def test_liquidation_precedes_user_minus120_loss_threshold(self):
        r = self.run_path([self.bar(l=87, c=90)])
        self.assertEqual(r["status"], "LIQUIDATION")
        self.assertEqual(r["roe_pct"], -90)
        self.assertFalse(r["success"])

    def test_loss_before_more_distant_liquidation(self):
        s = dict(self.snapshot, liquidation_price=80)
        r = self.run_path([self.bar(l=85, c=90)], snapshot=s)
        self.assertEqual(r["status"], "LOSS")
        self.assertAlmostEqual(r["roe_pct"], -120)
        self.assertAlmostEqual(r["exit_price"], 88)

    def test_short_direction_target_and_liquidation(self):
        s = dict(self.snapshot, side="SHORT", reference_price=105, liquidation_price=109)
        target = self.run_path([self.bar(o=105, h=106, l=99, c=100)], snapshot=s)
        self.assertEqual(target["status"], "TARGET")
        self.assertAlmostEqual(target["exit_price"], 99.7)
        loss = self.run_path([self.bar(o=105, h=110, l=104, c=108)], snapshot=s)
        self.assertEqual(loss["status"], "LIQUIDATION")

    def test_same_candle_target_and_liquidation_never_assumed_win(self):
        r = self.run_path([self.bar(h=101, l=90)])
        self.assertEqual(r["status"], "AMBIGUOUS")
        self.assertEqual(r["conservative_terminal"], "LIQUIDATION")
        self.assertIsNone(r["success"])
        self.assertIsNone(r["net_pnl_usdc"])
        self.assertFalse(r["calibration_eligible"])

    def test_same_candle_target_and_loss_without_liquidation(self):
        s = dict(self.snapshot, liquidation_price=None)
        r = self.run_path([self.bar(h=101, l=87)], snapshot=s)
        self.assertEqual(r["status"], "AMBIGUOUS")
        self.assertEqual(r["conservative_terminal"], "LOSS")

    def test_missing_liquidation_is_not_eligible_research_sample(self):
        r = self.run_path([self.bar(h=101)], snapshot=dict(self.snapshot, liquidation_price=None))
        self.assertEqual(r["status"], "TARGET")
        self.assertFalse(r["calibration_eligible"])

    def test_opening_adverse_gap_uses_worse_price_not_barrier(self):
        r = self.run_path([self.bar(o=85, h=87, l=83, c=84)])
        self.assertEqual(r["status"], "LIQUIDATION")
        self.assertEqual(r["exit_price"], 85)
        self.assertEqual(r["roe_pct"], -150)

    def test_opening_favourable_gap_does_not_invent_extra_profit(self):
        r = self.run_path([self.bar(o=105, h=110, l=104, c=108)])
        self.assertEqual(r["status"], "TARGET")
        self.assertAlmostEqual(r["roe_pct"], 3)

    def test_fees_slippage_and_paid_costs_change_target(self):
        costs = OutcomeCosts(.1, 10, 10, 0)
        s = dict(self.snapshot, paid_costs_usdc=.2)
        r = self.run_path([self.bar(h=102, c=101)], costs=costs, snapshot=s)
        self.assertEqual(r["status"], "TARGET")
        self.assertAlmostEqual(r["exit_price"], 100.6/.998)
        self.assertAlmostEqual(r["net_pnl_usdc"], .3)
        self.assertGreater(r["exit_cost_usdc"], .2)

    def test_funding_estimate_is_signed_and_explicit(self):
        policy = replace(self.policy, horizon_ms=60000)
        s = dict(self.snapshot, reference_price=100, liquidation_price=90)
        bar = self.bar(o=100, h=100, l=100, c=100)
        paid = self.run_path([bar], snapshot=s, policy=policy, costs=OutcomeCosts(0, 0, 0, 6))
        credit = self.run_path([bar], snapshot=s, policy=policy, costs=OutcomeCosts(0, 0, 0, -6))
        self.assertAlmostEqual(paid["funding_estimate_usdc"], .1)
        self.assertAlmostEqual(paid["net_pnl_usdc"], -.1)
        self.assertAlmostEqual(credit["net_pnl_usdc"], .1)

    def test_intrabar_funding_timing_uncertainty_not_a_win(self):
        s = dict(self.snapshot, reference_price=100, liquidation_price=90)
        r = self.run_path([self.bar(o=100, h=100.35, l=100, c=100.1)],
                          snapshot=s, costs=OutcomeCosts(0, 0, 0, 6))
        self.assertEqual(r["status"], "AMBIGUOUS")
        self.assertEqual(r["reason"], "unknown_intrabar_funding_timing")

    def test_realized_loss_cannot_be_erased_by_partial_reduction(self):
        # Half already sold at -5, remaining half at entry 100. Fixed original
        # risk capital remains $10: reaching +3% needs remaining price 105.6.
        s = dict(self.snapshot, size=.5, realized_pnl_usdc=-2.5)
        r = self.run_path([self.bar(h=101, c=100)], snapshot=s)
        self.assertEqual(r["status"], "OPEN")
        r = self.run_path([self.bar(h=106, c=105)], snapshot=s)
        self.assertEqual(r["status"], "TARGET")
        self.assertAlmostEqual(r["exit_price"], 105.6)

    def test_post_action_margin_is_rejected_as_roe_basis(self):
        s = dict(self.snapshot, risk_basis="post_action_margin")
        with self.assertRaisesRegex(ValueError, "original_pre_intervention"):
            self.run_path([], snapshot=s)

    def test_full_horizon_without_recovery_is_not_target_success(self):
        policy = replace(self.policy, horizon_ms=120000)
        r = self.run_path([self.bar(), self.bar(60000)], now=120000, policy=policy)
        self.assertEqual(r["status"], "HORIZON")
        self.assertEqual(r["roe_pct"], -50)
        self.assertFalse(r["success"])

    def test_missing_bars_before_later_target_is_data_gap(self):
        r = self.run_path([self.bar(), self.bar(120000, h=110)], now=180000)
        self.assertEqual(r["status"], "DATA_GAP")
        self.assertFalse(r["terminal"])
        self.assertIsNone(r["success"])

    def test_lack_of_horizon_coverage_can_be_backfilled(self):
        policy = replace(self.policy, horizon_ms=120000)
        r = self.run_path([self.bar()], now=120000, policy=policy)
        self.assertEqual(r["reason"], "missing_coverage_at_horizon")
        self.assertFalse(r["terminal"])

    def test_stale_data_is_not_clean_open(self):
        r = self.run_path([self.bar()], now=600001)
        self.assertEqual(r["status"], "STALE")

    def test_unclosed_and_beyond_horizon_candles_are_not_used(self):
        pending = self.run_path([self.bar(h=110)], now=59999)
        self.assertEqual(pending["status"], "OPEN")
        future = self.run_path([self.bar(), self.bar(60000, h=110)], now=120000,
                               policy=replace(self.policy, horizon_ms=60000))
        self.assertEqual(future["status"], "HORIZON")

    def test_candle_with_presignal_high_is_never_used(self):
        s = dict(self.snapshot, signal_ms=30000, features_asof_ms=30000, quote_asof_ms=30000)
        r = self.run_path([self.bar(h=110), self.bar(60000)], now=120000, snapshot=s)
        self.assertEqual(r["reason"], "initial_partial_candle_unobservable")
        self.assertFalse(r["terminal"])

    def test_forward_partial_first_bar_and_final_bar_allowed(self):
        s = dict(self.snapshot, signal_ms=30000, features_asof_ms=30000, quote_asof_ms=30000)
        policy = replace(self.policy, horizon_ms=120000)
        r = self.run_path([self.bar(30000, duration=30000), self.bar(60000),
                           self.bar(120000, duration=30000)], now=150000, snapshot=s, policy=policy)
        self.assertEqual(r["status"], "HORIZON")

    def test_invalid_numeric_future_and_stale_features_rejected(self):
        for change in ({"size": float("nan")}, {"size": float("inf")}, {"size": 0},
                       {"features_asof_ms": 1}, {"quote_asof_ms": 1},
                       {"risk_capital_usdc": 0}, {"paid_costs_usdc": -1},
                       {"liquidation_price": 96}, {"side": "INVALID"}):
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                self.run_path([], snapshot=dict(self.snapshot, **change))
        stale = dict(self.snapshot, signal_ms=999999)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.run_path([], snapshot=stale, now=999999)

    def test_invalid_ohlc_and_costs_rejected(self):
        with self.assertRaises(ValueError): self.run_path([self.bar(l=96)])
        for costs in (OutcomeCosts(-1, 0, 0, 0), OutcomeCosts(0, -1, 0, 0),
                      OutcomeCosts(0, 0, 10000, 0), OutcomeCosts(0, 0, 0, float("nan"))):
            with self.subTest(costs=costs), self.assertRaises(ValueError): self.run_path([], costs=costs)

    def test_duplicates_are_idempotent_but_conflicts_are_not_accepted(self):
        r = self.run_path([self.bar(), self.bar()])
        self.assertEqual(r["status"], "OPEN")
        r = self.run_path([self.bar(), self.bar(h=101)])
        self.assertEqual(r["reason"], "conflicting_duplicate_candles")

    def test_immutable_snapshot_and_idempotent_creation(self):
        snap = copy.deepcopy(self.snapshot)
        self.store.create("a", snap, self.policy, self.costs)
        snap["features"]["rsi14"] = 99
        stored = self.store.get("1", "a")
        self.assertEqual(stored["snapshot"]["features"]["rsi14"], 30)
        self.store.create("a", self.snapshot, self.policy, self.costs)
        with self.assertRaisesRegex(ValueError, "different immutable"):
            self.store.create("a", snap, self.policy, self.costs)
        with closing(self.store.connect()) as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute("UPDATE scenarios SET snapshot='{}'")

    def test_pending_and_terminal_outcomes_survive_restart(self):
        self.store.create("a", self.snapshot, self.policy, self.costs)
        pending = self.store.evaluate("1", "a", [self.bar()], 60000)
        self.assertEqual(AiOutcomes(self.tmp.name).get("1", "a")["result"], pending)
        final = AiOutcomes(self.tmp.name).evaluate("1", "a", [self.bar(), self.bar(60000, h=101)], 120000)
        self.assertEqual(final["status"], "TARGET")
        # Newly supplied contradictory bars must not rewrite a frozen outcome.
        after = AiOutcomes(self.tmp.name).evaluate("1", "a", [self.bar(l=85)], 180000)
        self.assertEqual(after, final)
        self.assertEqual(self.store.summary("1")["status_counts"], {"TARGET": 1})
        with closing(self.store.connect()) as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute("DELETE FROM evaluations")

    def test_arithmetic_overflow_fails_explicitly_not_as_a_win(self):
        s = dict(self.snapshot, size=1e308, entry_price=1e308, reference_price=1e308,
                 liquidation_price=None)
        with self.assertRaisesRegex(ValueError, "calculated"):
            self.run_path([self.bar()], snapshot=s)

    def test_older_worker_cannot_regress_current_evaluation(self):
        self.store.create("a", self.snapshot, self.policy, self.costs)
        newer = self.store.evaluate("1", "a", [self.bar(), self.bar(60000)], 120000)
        older = self.store.evaluate("1", "a", [], 60000)
        self.assertEqual(older, newer)

    def test_cross_account_lookup_rejected_and_global_summary_anonymous(self):
        self.store.create("a", self.snapshot, self.policy, self.costs)
        with self.assertRaises(ValueError): self.store.get("2", "a")
        with self.assertRaises(ValueError): self.store.evaluate("2", "a", [], 0)
        self.assertEqual(self.store.summary("2")["scenarios"], 0)
        self.assertNotIn("owner_id", str(self.store.summary()))
        self.assertNotIn("verified-source-id", str(self.store.summary()))

    def test_pending_polling_is_scoped_and_excludes_terminal_outcomes(self):
        self.store.create("a", self.snapshot, self.policy, self.costs)
        self.assertEqual(len(AiOutcomes(self.tmp.name).pending("1")), 1)
        self.assertEqual(self.store.pending("2"), [])
        self.store.evaluate("1", "a", [self.bar(h=101)], 60000)
        self.assertEqual(self.store.pending("1"), [])

    def test_minimum_and_large_sizes_keep_same_roe_when_risk_scaled(self):
        for size in (1e-8, 1e8):
            s = dict(self.snapshot, size=size, risk_capital_usdc=10*size)
            result = self.run_path([self.bar(h=101)], snapshot=s)
            self.assertEqual(result["status"], "TARGET")
            self.assertAlmostEqual(result["roe_pct"], 3, places=5)


if __name__ == "__main__":
    unittest.main()
