import copy
import json
import math
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from core.ai_learning import AiLearning, BAR_MS, HOUR_MS, _features, _outcome


START = 20000 * 86400000


def candles(count, offset=0, coin="BTC"):
    rows = []
    for index in range(offset, offset + count):
        # Deterministic changing regimes; no random source or external data.
        o = 100 + index * .003 + 2 * math.sin(index / 13) + math.sin(index / 3)
        c = o + .3 * math.sin(index / 5)
        rows.append({"t": START + index * BAR_MS, "T": START + (index + 1) * BAR_MS - 1,
                     "o": o, "h": max(o, c) + .5, "l": min(o, c) - .5,
                     "c": c, "v": 100 + index % 17, "s": coin, "i": "15m"})
    return rows


class AiLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.learner = AiLearning(self.temp.name)

    def seed(self, count=800, both=True):
        now = START + count * BAR_MS + 1000
        self.learner.ingest("BTC", candles(count), now)
        if both:
            self.learner.ingest("ETH", candles(count, coin="ETH"), now)
        return now

    def query(self, sql, params=()):
        with closing(sqlite3.connect(self.learner.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]

    def test_empty_summary_has_no_fake_model_or_probability(self):
        result = self.learner.summary()
        self.assertEqual(result["status"], "WAITING_DATA")
        self.assertIsNone(result["model"])
        self.assertIsNone(result["validation"])
        self.assertEqual(result["predictions"], [])
        self.assertFalse(result["real_execution_available"])
        self.assertIsNone(result["forward"]["brier"])

    def test_small_history_keeps_waiting(self):
        result = self.learner.train(self.seed(120))
        self.assertGreater(result["counts"]["examples"], 0)
        self.assertIsNone(result["model"])
        self.assertIsNone(result["validation"])
        self.assertEqual(result["counts"]["forward_predictions"], 0)

    def test_ingest_idempotent_and_conflict_atomic(self):
        rows = candles(10)
        now = rows[-1]["T"] + 1000
        self.assertEqual(self.learner.ingest("BTC", rows, now)["inserted"], 10)
        self.assertEqual(self.learner.ingest("BTC", rows, now)["duplicates"], 10)
        bad = candles(11)
        bad[5]["v"] += 1
        with self.assertRaises(ValueError): self.learner.ingest("BTC", bad, bad[-1]["T"] + 1000)
        self.assertEqual(self.learner.summary()["counts"]["candles"], 10)

    def test_future_gaps_and_malformed_candles_rejected(self):
        source = candles(10)
        bad_batches = [source[:-1] + [candles(1, 11)[0]], [source[0], source[2]]]
        for key, value in [("c", float("nan")), ("c", -1), ("h", 0), ("v", -1),
                           ("o", True), ("i", "1h"), ("s", "ETH"), ("t", START + 1),
                           ("T", source[0]["T"] + 5)]:
            row = copy.deepcopy(source[0]); row[key] = value; bad_batches.append([row])
        for batch in bad_batches:
            with self.assertRaises(ValueError): self.learner.ingest("BTC", batch, source[-1]["T"] + 1)
        with self.assertRaises(ValueError): self.learner.ingest("BTC", source, source[-1]["T"])
        with self.assertRaises(ValueError): self.learner.ingest("xyz:INTC", source, source[-1]["T"] + 1)
        self.assertEqual(self.learner.summary()["counts"]["candles"], 0)

    def test_append_gap_rejected_and_overlap_backfill_allowed(self):
        self.learner.ingest("BTC", candles(10), START + 20 * BAR_MS)
        with self.assertRaises(ValueError):
            self.learner.ingest("BTC", candles(2, 12), START + 20 * BAR_MS)
        self.assertEqual(self.learner.ingest("BTC", candles(5, 8), START + 20 * BAR_MS)["inserted"], 3)

    def test_labels_use_delayed_next_open_and_four_bar_exit(self):
        now = self.seed(120, False)
        self.learner.train(now)
        row = self.query("SELECT * FROM learning_examples ORDER BY decision_ms,direction LIMIT 1")[0]
        i = (row["decision_ms"] + 1 - START) // BAR_MS - 1
        self.assertEqual(row["entry_ms"], START + (i + 2) * BAR_MS)
        self.assertEqual(row["exit_ms"], START + (i + 6) * BAR_MS - 1)
        expected = candles(6, i)
        self.assertEqual(row["entry_price"], expected[2]["o"])
        self.assertEqual(row["exit_price"], expected[5]["c"])
        net, label = _outcome(expected[2]["o"], expected[5]["c"], row["direction"])
        self.assertAlmostEqual(row["net_return"], net)
        self.assertEqual(row["label"], label)

    def test_costs_make_flat_long_and_short_negative(self):
        for side in ("LONG", "SHORT"):
            net, label = _outcome(100, 100, side)
            self.assertLess(net, 0)
            self.assertEqual(label, 0)

    def test_direction_interactions_are_not_two_identical_training_vectors(self):
        self.learner.train(self.seed(120, False))
        rows = self.query("SELECT features FROM learning_examples ORDER BY decision_ms,direction LIMIT 2")
        long, short = (json.loads(row["features"]) for row in rows)
        self.assertEqual(long[:6], [-value for value in short[:6]])
        self.assertEqual(long[6:], short[6:])
        self.assertNotEqual(long, short)

    def test_bootstrap_real_model_metrics_and_shared_time_partition(self):
        result = self.learner.train(self.seed())
        self.assertEqual(result["status"], "RESEARCH_MODEL")
        validation = result["validation"]
        self.assertGreaterEqual(validation["train_groups"], 100)
        self.assertGreaterEqual(validation["groups"], 40)
        self.assertTrue(validation["frozen"])
        self.assertEqual(validation["kind"], "RETROSPECTIVE_BOOTSTRAP")
        self.assertTrue(0 <= validation["brier"] <= 1)
        self.assertTrue(math.isfinite(validation["log_loss"]))
        partitions = self.query("SELECT decision_ms,COUNT(DISTINCT partition) AS n FROM learning_examples GROUP BY decision_ms")
        self.assertTrue(all(row["n"] == 1 for row in partitions))
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM learning_examples WHERE partition IN ('TEST','PURGE') AND trained_version IS NOT NULL")[0]["n"], 0)
        self.assertFalse(result["real_execution_available"])

    def test_label_purge_separates_training_outcomes_from_test_features(self):
        self.learner.train(self.seed())
        last_train = self.query("SELECT MAX(exit_ms) AS last FROM learning_examples WHERE partition='TRAIN'")[0]["last"]
        first_test = self.query("SELECT MIN(decision_ms) AS first FROM learning_examples WHERE partition='TEST'")[0]["first"]
        self.assertLess(last_train, first_test)
        self.assertGreaterEqual(first_test - last_train, BAR_MS)

    def test_scaler_uses_training_rows_only(self):
        self.learner.train(self.seed())
        model = self.query("SELECT * FROM learning_models ORDER BY version LIMIT 1")[0]
        scaler = json.loads(model["scaler"])
        features = [json.loads(row["features"]) for row in self.query("SELECT features FROM learning_examples WHERE trained_version=1")]
        for i in range(8):
            self.assertAlmostEqual(scaler["mean"][i], math.fsum(x[i] for x in features) / len(features))
        self.assertEqual(scaler["basis"], "bootstrap_train_only_frozen")

    def test_features_do_not_change_when_future_price_changes(self):
        now = self.seed(120, False)
        self.learner.train(now)
        before = self.query("SELECT coin,decision_ms,direction,features FROM learning_examples ORDER BY decision_ms,direction")
        self.learner.ingest("BTC", candles(20, 120), START + 140 * BAR_MS + 1000)
        self.learner.train(START + 140 * BAR_MS + 1000)
        after = self.query("SELECT coin,decision_ms,direction,features FROM learning_examples WHERE decision_ms<=? ORDER BY decision_ms,direction", (before[-1]["decision_ms"],))
        self.assertEqual(before, after)

    def test_forward_predictions_created_before_entry_not_backfilled(self):
        now = self.seed()
        result = self.learner.train(now)
        self.assertEqual(len(result["predictions"]), 4)
        for prediction in result["predictions"]:
            self.assertLess(prediction["created_ms"], prediction["entry_ms"])
            self.assertLess(prediction["decision_ms"], prediction["created_ms"])
            self.assertFalse(prediction["probability_calibrated"] if "probability_calibrated" in prediction else False)
        earlier = copy.deepcopy(result["predictions"])
        late = self.learner.train(now + BAR_MS)
        self.assertEqual(late["predictions"], earlier)
        self.assertEqual(late["counts"]["forward_predictions"], 4)

    def test_late_bootstrap_never_creates_historical_forecasts(self):
        now = self.seed()
        result = self.learner.train(now + BAR_MS)
        self.assertIsNotNone(result["model"])
        self.assertEqual(result["counts"]["forward_predictions"], 0)

    def test_forecast_requires_fresh_feature_bar_not_last_second_before_entry(self):
        now = self.seed()
        result = self.learner.train(now + 120_000)
        self.assertIsNotNone(result["model"])
        self.assertEqual(result["counts"]["forward_predictions"], 0)

    def test_mature_then_online_update_frozen_forward_and_validation(self):
        now = self.seed()
        first = self.learner.train(now)
        frozen = self.query("SELECT coin,decision_ms,direction,created_ms,model_version,probability FROM learning_predictions")
        for coin in ("BTC", "ETH"):
            self.learner.ingest(coin, candles(8, 800, coin), START + 808 * BAR_MS + 1000)
        second = self.learner.train(START + 808 * BAR_MS + 1000)
        self.assertGreater(second["model"]["version"], first["model"]["version"])
        self.assertEqual(second["validation"], first["validation"])
        self.assertEqual(second["counts"]["forward_matured"], 4)
        self.assertEqual(second["forward"]["samples"], 4)
        for row in frozen:
            saved = self.query("SELECT coin,decision_ms,direction,created_ms,model_version,probability FROM learning_predictions WHERE coin=? AND decision_ms=? AND direction=?",
                               (row["coin"], row["decision_ms"], row["direction"]))[0]
            self.assertEqual(row, saved)
        first_scaler = self.query("SELECT scaler FROM learning_models WHERE version=1")[0]
        self.assertTrue(all(row == first_scaler for row in self.query("SELECT scaler FROM learning_models")))
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM learning_examples WHERE partition IN ('TEST','PURGE') AND trained_version IS NOT NULL")[0]["n"], 0)

    def test_training_same_matured_history_is_idempotent(self):
        now = self.seed()
        first = self.learner.train(now)
        for _ in range(3):
            second = self.learner.train(now)
            self.assertEqual(first, second)
        self.assertEqual(second["counts"]["model_versions"], 1)

    def test_restart_and_read_only_summary_preserve_everything(self):
        now = self.seed()
        first = self.learner.train(now)
        restarted = AiLearning(self.temp.name)
        self.assertEqual(restarted.summary(now + 10 * BAR_MS), first)
        self.assertEqual(restarted.summary(), first)

    def test_concurrent_training_creates_one_model_and_forecast_set(self):
        now = self.seed()
        def run(_):
            return AiLearning(self.temp.name).train(now)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(run, range(3)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(results[0]["counts"]["model_versions"], 1)

    def test_deterministic_fit_and_no_private_or_order_dependencies(self):
        now = self.seed()
        first = self.learner.train(now)
        with tempfile.TemporaryDirectory() as second_root:
            learner = AiLearning(second_root)
            for coin in ("BTC", "ETH"):
                learner.ingest(coin, candles(800, coin=coin), now)
            second = learner.train(now)
            self.assertEqual(first, second)
        source = Path(__file__).resolve().parents[1].joinpath("core", "ai_learning.py").read_text(encoding="utf-8")
        for forbidden in ("import requests", "from hyperliquid", "private_key", "user_id", "account_address", "market_open("):
            self.assertNotIn(forbidden, source)
        json.dumps(first, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
