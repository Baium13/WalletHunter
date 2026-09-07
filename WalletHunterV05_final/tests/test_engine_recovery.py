"""Crash recovery evidence is synthetic; no public/private network is used."""
from contextlib import closing
from copy import deepcopy
import json
import unittest

from core.trading_engine import CopyEngine
from tests import test_engine_safety as fixtures


class RecoveryReader(fixtures.Reader):
    def __init__(self):
        self.history = []
        self.requests = []
        self.fail = False
    def _info(self, payload):
        self.requests.append(deepcopy(payload))
        if self.fail:
            raise RuntimeError("Offline synthetic history failure")
        if payload.get("type") != "userFillsByTime":
            raise AssertionError("Unexpected API request in isolated recovery test")
        return deepcopy(self.history)


class EngineRecoveryTests(unittest.TestCase):
    setUp = fixtures.EngineSafetyTests.setUp
    runtime = fixtures.EngineSafetyTests.runtime
    patch = fixtures.EngineSafetyTests.patch
    run_cycle = fixtures.EngineSafetyTests.run_cycle
    trades = fixtures.EngineSafetyTests.trades

    def open_and_restart(self, *, lose_json=True, market="BTC", dex=""):
        source = [fixtures.snapshot(positions=[fixtures.position(market, 1000, dex=dex)])]
        self.run_cycle(source)
        self.market = CopyEngine._runtime_key(CopyEngine._key(market, dex))
        self.record = self.engine.journal.owned(fixtures.ACCOUNT)[self.market]
        self.assertTrue(self.record["managed"])
        if lose_json:
            self.runtime(managed=[])
        self.client.calls.clear()
        self.reader = RecoveryReader()
        self.engine = CopyEngine(self.reader, self.store, self.settings)
        return source

    def assert_unrecovered(self):
        runtime = self.store.profile(1)[1]["runtime"]
        self.assertEqual(self.client.calls, [])
        self.assertNotIn(self.market, runtime["managed"])
        self.assertIn(self.market, runtime["recovery_required"])

    def update_ledger_record(self, record):
        with closing(self.engine.journal.connect()) as db:
            db.execute("UPDATE ownership SET record=? WHERE account=? AND market=?",
                       (json.dumps(record), fixtures.ACCOUNT, self.market))
            db.commit()

    def test_confirmed_sqlite_before_json_recovers_exact_unchanged_position(self):
        source = self.open_and_restart()
        result = self.run_cycle(source)
        runtime = self.store.profile(1)[1]["runtime"]
        self.assertEqual(result, [])
        self.assertEqual(runtime["managed"], [self.market])
        self.assertEqual(runtime["recovery_required"], [])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.reader.requests[0]["startTime"], self.record["verified_at_ms"])
        self.assertFalse(self.reader.requests[0]["aggregateByTime"])

    def test_recovered_position_follows_verified_source_exit(self):
        self.open_and_restart()
        self.run_cycle([fixtures.snapshot()])
        self.assertEqual([call[0] for call in self.trades()], ["close"])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], [])
        self.assertFalse(self.engine.journal.owned(fixtures.ACCOUNT)[self.market]["managed"])

    def test_matching_position_normal_restart_does_not_submit_duplicate(self):
        source = self.open_and_restart(lose_json=False)
        self.run_cycle(source)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], [self.market])

    def test_history_cap_prevents_adoption_even_if_rows_are_other_markets(self):
        source = self.open_and_restart()
        self.reader.history = [{"coin": "ETH", "time": self.record["verified_at_ms"]+1}] * 2000
        self.run_cycle(source)
        self.assert_unrecovered()

    def test_missing_history_prevents_adoption_and_exit(self):
        self.open_and_restart()
        self.reader.fail = True
        self.run_cycle([fixtures.snapshot()])
        self.assert_unrecovered()

    def test_invalid_history_type_is_not_treated_as_empty(self):
        source = self.open_and_restart()
        self.reader.history = {"error": "rate limit"}
        self.run_cycle(source)
        self.assert_unrecovered()

    def test_unparseable_fill_is_not_ignored_during_recovery(self):
        source = self.open_and_restart()
        self.reader.history = [{"time": self.record["verified_at_ms"]+1}]
        self.run_cycle(source)
        self.assert_unrecovered()

    def test_later_fill_same_market_blocks_identical_manual_reopened_position(self):
        source = self.open_and_restart()
        self.reader.history = [
            {"coin": "BTC", "time": self.record["verified_at_ms"]+1, "dir": "Close Long", "sz": "1"},
            {"coin": "BTC", "time": self.record["verified_at_ms"]+2, "dir": "Open Long", "sz": "1"},
        ]
        self.run_cycle(source)
        self.assert_unrecovered()

    def test_existing_json_ownership_does_not_adopt_identical_manual_reopen(self):
        self.open_and_restart(lose_json=False)
        self.reader.history = [
            {"coin": "BTC", "time": self.record["verified_at_ms"]+1, "dir": "Close Long", "sz": "1"},
            {"coin": "BTC", "time": self.record["verified_at_ms"]+2, "dir": "Open Long", "sz": "1"},
        ]
        # Following a source exit would close this identical replacement unless
        # durable ownership also proves continuous lifetime, not just shape.
        self.run_cycle([fixtures.snapshot()])
        self.assertEqual(self.client.calls, [], "A stale managed flag cannot authorize closing a manually reopened position")
        self.assertIn(self.market, self.store.profile(1)[1]["runtime"]["recovery_required"])

    def test_other_market_fills_do_not_destroy_exact_market_ownership(self):
        source = self.open_and_restart()
        self.reader.history = [{"coin": "ETH", "time": self.record["verified_at_ms"]+1}]
        self.run_cycle(source)
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], [self.market])
        self.assertEqual(self.client.calls, [])

    def test_size_side_or_entry_mismatch_is_never_adopted(self):
        source = self.open_and_restart()
        key = CopyEngine._from_runtime(self.market)
        original = deepcopy(self.client.rows[key])
        for change in ({"size": original["size"]+.01}, {"side": "SHORT"}, {"entry_price": 100.01}):
            with self.subTest(change=change):
                self.client.rows[key] = dict(original, **change)
                self.run_cycle(source)
                self.assert_unrecovered()

    def test_missing_verified_timestamp_cannot_recover_legacy_record(self):
        source = self.open_and_restart()
        record = dict(self.record)
        record.pop("verified_at_ms")
        self.update_ledger_record(record)
        self.run_cycle(source)
        self.assert_unrecovered()

    def test_explicit_manual_hold_survives_recovery(self):
        source = self.open_and_restart()
        self.runtime(manual_hold_keys=[self.market])
        self.run_cycle(source)
        runtime = self.store.profile(1)[1]["runtime"]
        self.assertEqual(runtime["manual_hold_keys"], [self.market])
        self.assertEqual(self.client.calls, [])

    def test_explicit_ai_hold_survives_recovery(self):
        self.open_and_restart()
        self.runtime(ai_hold_keys={self.market: {"proposal": "offline"}})
        self.run_cycle([fixtures.snapshot()])
        runtime = self.store.profile(1)[1]["runtime"]
        self.assertIn(self.market, runtime["ai_hold_keys"])
        self.assertEqual(self.client.calls, [])

    def test_pending_intent_blocks_further_trading_after_shape_recovery(self):
        source = self.open_and_restart()
        self.engine.journal.prepare(fixtures.ACCOUNT, self.market, {"action": "UNKNOWN_NEXT_ORDER"})
        self.run_cycle(source)
        self.assertEqual(self.client.calls, [])
        self.assertIn(self.market, self.store.profile(1)[1]["runtime"]["recovery_required"])

    def test_recovery_under_xyz_alias_preserves_one_market(self):
        source = self.open_and_restart(market="xyz:INTC", dex="xyz")
        self.client.rows[("xyz:INTC", "xyz")]["coin"] = "INTC"
        self.run_cycle(source)
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], ["xyz:INTC|xyz"])
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
