"""P1-01: deterministic full-engine ownership-proof TTL regressions.

All exchange calls use the existing synthetic boundary; JSON/SQLite live in a
TemporaryDirectory. Only core.trading_engine's time reference is replaced, so
asyncio's own clock and the process-wide time module keep their real behavior.
"""
import asyncio
from contextlib import closing
from copy import deepcopy
from dataclasses import FrozenInstanceError
import time as real_time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import core.trading_engine as engine_module
from tests import test_engine_recovery as recovery
from tests import test_engine_safety as fixtures


READER_MAIN = "https://reader-main.invalid/info"
READER_TEST = "https://reader-test.invalid/info"
CLIENT_MAIN = "https://signer-main.invalid"
CLIENT_TEST = "https://signer-test.invalid"
SECOND_ACCOUNT = "0x" + "d" * 40


class OwnershipHistoryCacheTests(unittest.TestCase):
    # Reuse helper methods, not the existing TestCase class: discovery must not
    # inherit and run the recovery/safety suites a second time.
    runtime = fixtures.EngineSafetyTests.runtime
    patch = fixtures.EngineSafetyTests.patch
    run_cycle = fixtures.EngineSafetyTests.run_cycle
    trades = fixtures.EngineSafetyTests.trades
    open_and_restart = recovery.EngineRecoveryTests.open_and_restart

    def setUp(self):
        fixtures.EngineSafetyTests.setUp(self)
        self.monotonic_now = 1000.0
        clock = SimpleNamespace(
            time=real_time.time,
            monotonic=lambda: self.monotonic_now,
        )
        clock_patch = patch.object(engine_module, "time", clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def begin_recovery(self, *, market="BTC", dex=""):
        self.source = self.open_and_restart(market=market, dex=dex)
        self.reader.base_url = READER_MAIN
        self.client.base = CLIENT_MAIN
        self.original_rows = deepcopy(self.client.rows)
        self.assertEqual(self.engine.ownership_checks, {})
        return self.source

    def cache_key(self, *, uid=1, address=fixtures.ACCOUNT):
        return (
            str(uid), address.lower(), self.reader.base_url, self.client.base,
            self.market, self.record["verified_at_ms"],
        )

    def proof(self, *, uid=1, address=fixtures.ACCOUNT):
        return self.engine.ownership_checks[self.cache_key(uid=uid, address=address)]

    def establish_proof(self, *, market="BTC", dex=""):
        self.begin_recovery(market=market, dex=dex)
        self.run_cycle(self.source)
        self.assertEqual(len(self.reader.requests), 1)
        self.assert_ready()
        return self.proof()

    def assert_no_execution(self, *, address=fixtures.ACCOUNT):
        self.assertEqual(self.client.calls, [], "History verification must not mutate the exchange")
        self.assertEqual(self.client.rows, self.original_rows)
        self.assertEqual(self.engine.journal.owned(address)[self.market], self.record,
                         "Cache refresh must not rewrite durable source provenance")

    def assert_ready(self, *, uid=1, address=fixtures.ACCOUNT):
        runtime = self.store.profile(uid)[1]["runtime"]
        self.assertIn(self.market, runtime["managed"])
        self.assertNotIn(self.market, runtime["recovery_required"])
        self.assert_no_execution(address=address)

    def assert_held(self, *, uid=1, address=fixtures.ACCOUNT):
        runtime = self.store.profile(uid)[1]["runtime"]
        self.assertIn(self.market, runtime["recovery_required"])
        self.assert_no_execution(address=address)

    def later_fill(self, *, coin="BTC", dex=""):
        return {
            "coin": coin, "dex": dex,
            "time": self.record["verified_at_ms"] + 1,
            "dir": "Close Long", "sz": "1",
        }

    def run_as(self, uid, snapshots):
        _, profile = self.store.profile(uid)

        async def notify(result, account):
            self.notifications.append(result)

        return asyncio.run(self.engine.sync_profile(uid, profile, self.client, snapshots, notify))

    def assert_invalid_refresh_then_retry(self, invalid):
        original = self.establish_proof()
        self.reader.history = invalid
        for stamp, count in ((1060.0, 2), (1061.0, 3)):
            self.monotonic_now = stamp
            self.run_cycle([fixtures.snapshot()])
            self.assertEqual(len(self.reader.requests), count)
            self.assertIs(self.proof(), original, "A failed refresh must not renew stale evidence")
            self.assertEqual(original.fetched_at, 1000.0)
            self.assert_held()
        self.reader.history = []
        self.monotonic_now = 1062.0
        self.run_cycle(self.source)
        self.assertEqual(len(self.reader.requests), 4)
        self.assertEqual(self.proof().fetched_at, 1062.0)
        self.assert_ready()

    def test_first_fetch_stores_immutable_proof_without_changing_provenance(self):
        self.begin_recovery()
        self.run_cycle(self.source)
        self.assertEqual(self.reader.requests, [{
            "type": "userFillsByTime", "user": fixtures.ACCOUNT,
            "startTime": self.record["verified_at_ms"], "aggregateByTime": False,
        }])
        self.assertEqual(set(self.engine.ownership_checks), {self.cache_key()})
        evidence = self.proof()
        self.assertIs(evidence.no_later_fills, True)
        self.assertEqual(evidence.fetched_at, 1000.0)
        with self.assertRaises(FrozenInstanceError):
            evidence.fetched_at = 1001.0
        self.assert_ready()

    def test_zero_monotonic_timestamp_is_valid_and_same_time_reuses_proof(self):
        self.monotonic_now = 0.0
        self.begin_recovery()
        self.run_cycle(self.source)
        original = self.proof()
        self.assertEqual(original.fetched_at, 0.0)
        self.assertEqual(len(self.reader.requests), 1)
        self.run_cycle(self.source)
        self.assertEqual(len(self.reader.requests), 1)
        self.assertIs(self.proof(), original, "A valid zero timestamp must not become a cache miss")
        self.assert_ready()

    def test_new_verified_snapshot_watermark_requires_its_own_history_read(self):
        original = self.establish_proof()
        old_key = self.cache_key()
        newer_record = deepcopy(self.record)
        newer_record["verified_at_ms"] += 1
        recovery.EngineRecoveryTests.update_ledger_record(self, newer_record)
        self.record = newer_record
        self.monotonic_now = 1001.0
        self.run_cycle(self.source)
        self.assertEqual(len(self.reader.requests), 2)
        self.assertEqual(self.reader.requests[-1]["startTime"], newer_record["verified_at_ms"])
        self.assertEqual(set(self.engine.ownership_checks), {old_key, self.cache_key()})
        self.assertIs(self.engine.ownership_checks[old_key], original)
        self.assertEqual(self.proof().fetched_at, 1001.0)
        self.assert_ready()

    def test_cache_hit_before_sixty_seconds_and_refresh_at_exact_expiry(self):
        original = self.establish_proof()
        self.monotonic_now = 1059.999
        self.run_cycle(self.source)
        self.assertEqual(len(self.reader.requests), 1)
        self.assertIs(self.proof(), original)
        self.assertEqual(original.fetched_at, 1000.0)
        self.monotonic_now = 1060.0
        self.run_cycle(self.source)
        self.assertEqual(len(self.reader.requests), 2)
        self.assertIsNot(self.proof(), original)
        self.assertEqual(self.proof().fetched_at, 1060.0)
        self.assert_ready()

    def test_frequent_cycles_never_slide_last_real_fetch_time(self):
        original = self.establish_proof()
        for offset in (1, 10, 20, 30, 40, 50, 59):
            with self.subTest(offset=offset):
                self.monotonic_now = 1000.0 + offset
                self.run_cycle(self.source)
                self.assertEqual(len(self.reader.requests), 1)
                self.assertIs(self.proof(), original)
                self.assertEqual(self.proof().fetched_at, 1000.0)
        self.monotonic_now = 1060.0
        self.run_cycle(self.source)
        refreshed = self.proof()
        self.assertEqual(len(self.reader.requests), 2)
        for stamp in (1061.0, 1080.0, 1100.0, 1119.0):
            self.monotonic_now = stamp
            self.run_cycle(self.source)
            self.assertEqual(len(self.reader.requests), 2)
            self.assertIs(self.proof(), refreshed)
        self.monotonic_now = 1120.0
        self.run_cycle(self.source)
        self.assertEqual(len(self.reader.requests), 3)
        self.assertEqual(self.proof().fetched_at, 1120.0)
        self.assert_ready()

    def test_external_fill_after_cached_snapshot_blocks_source_exit_on_refresh(self):
        original = self.establish_proof()
        self.reader.history = [self.later_fill()]
        self.monotonic_now = 1060.0
        self.run_cycle([fixtures.snapshot()])
        self.assertEqual(len(self.reader.requests), 2)
        self.assertIs(self.proof(), original)
        self.assert_held()

    def test_failed_refresh_cannot_renew_proof_and_next_cycle_can_recover(self):
        original = self.establish_proof()
        self.reader.fail = True
        for stamp, count in ((1060.0, 2), (1061.0, 3)):
            self.monotonic_now = stamp
            self.run_cycle([fixtures.snapshot()])
            self.assertEqual(len(self.reader.requests), count)
            self.assertIs(self.proof(), original)
            self.assertEqual(self.proof().fetched_at, 1000.0)
            self.assert_held()
        self.reader.fail = False
        self.monotonic_now = 1062.0
        self.run_cycle(self.source)
        self.assertEqual(len(self.reader.requests), 4)
        self.assertEqual(self.proof().fetched_at, 1062.0)
        self.assert_ready()

    def test_malformed_history_container_cannot_renew_or_authorize_exit(self):
        self.assert_invalid_refresh_then_retry({"error": "offline invalid history"})

    def test_truncated_history_cannot_renew_even_with_only_other_markets(self):
        # The existing cap is deliberately unchanged by P1-01.
        self.assert_invalid_refresh_then_retry([{"coin": "ETH", "time": 1}] * 2000)

    def test_unparseable_fill_cannot_renew_or_authorize_exit(self):
        self.assert_invalid_refresh_then_retry([{"time": 1}])

    def test_same_engine_same_address_rebound_to_another_user_requires_new_proof(self):
        original = self.establish_proof()
        old_key = self.cache_key()
        _, old_profile = self.store.profile(1)
        account = deepcopy(old_profile["account"])
        # Respect the storage's uniqueness guard: transfer, never duplicate, the
        # synthetic account. Rebinding must not inherit the first user's proof.
        self.patch(account=None, copy_enabled=False)
        _, other = self.store.profile(2)
        other.update(account=account, leaders=[fixtures.SOURCE_A], copy_enabled=True,
                     max_leverage=40, risk_mode="standard", strategy_mode="swing",
                     leader_exit_only=True)
        self.store.update_profile(2, other)
        self.reader.history = [self.later_fill()]
        self.monotonic_now = 1001.0
        self.run_as(2, [fixtures.snapshot()])
        self.assertEqual(len(self.reader.requests), 2)
        self.assertIs(self.engine.ownership_checks[old_key], original)
        self.assertNotIn(self.cache_key(uid=2), self.engine.ownership_checks)
        self.assert_held(uid=2)

    def test_same_engine_same_user_different_address_requires_new_proof(self):
        original = self.establish_proof()
        old_key = self.cache_key()
        with closing(self.engine.journal.connect()) as db:
            db.execute("INSERT INTO ownership(account,market,updated,operation_id,record) "
                       "SELECT ?,market,updated,operation_id,record FROM ownership "
                       "WHERE account=? AND market=?", (SECOND_ACCOUNT, fixtures.ACCOUNT, self.market))
            db.commit()
        _, profile = self.store.profile(1)
        account = deepcopy(profile["account"])
        account.update(id="offline-second", address=SECOND_ACCOUNT)
        self.patch(account=account)
        self.reader.history = [self.later_fill()]
        self.monotonic_now = 1001.0
        self.run_cycle([fixtures.snapshot()])
        self.assertEqual([row["user"] for row in self.reader.requests], [fixtures.ACCOUNT, SECOND_ACCOUNT])
        self.assertIs(self.engine.ownership_checks[old_key], original)
        self.assertNotIn(self.cache_key(address=SECOND_ACCOUNT), self.engine.ownership_checks)
        self.assert_held(address=SECOND_ACCOUNT)

    def test_reader_network_change_cannot_reuse_other_network_proof(self):
        original = self.establish_proof()
        old_key = self.cache_key()
        self.reader.base_url = READER_TEST
        self.reader.history = [self.later_fill()]
        self.monotonic_now = 1001.0
        self.run_cycle([fixtures.snapshot()])
        self.assertEqual(len(self.reader.requests), 2)
        self.assertIs(self.engine.ownership_checks[old_key], original)
        self.assertNotIn(self.cache_key(), self.engine.ownership_checks)
        self.assert_held()

    def test_signing_client_network_change_cannot_reuse_other_network_proof(self):
        original = self.establish_proof()
        old_key = self.cache_key()
        self.client.base = CLIENT_TEST
        self.reader.history = [self.later_fill()]
        self.monotonic_now = 1001.0
        self.run_cycle([fixtures.snapshot()])
        self.assertEqual(len(self.reader.requests), 2)
        self.assertIs(self.engine.ownership_checks[old_key], original)
        self.assertNotIn(self.cache_key(), self.engine.ownership_checks)
        self.assert_held()

    def test_successful_slow_fetch_uses_request_start_not_response_time(self):
        self.begin_recovery()
        original_info = self.reader._info

        def slow_read(payload):
            result = original_info(payload)
            self.monotonic_now += 12.0
            return result

        with patch.object(self.reader, "_info", side_effect=slow_read):
            self.run_cycle(self.source)
            self.assertEqual(self.monotonic_now, 1012.0)
            self.assertEqual(self.proof().fetched_at, 1000.0)
            self.monotonic_now = 1059.0
            self.run_cycle(self.source)
            self.assertEqual(len(self.reader.requests), 1)
            self.monotonic_now = 1060.0
            self.run_cycle(self.source)
        self.assertEqual(len(self.reader.requests), 2)
        self.assertEqual(self.proof().fetched_at, 1060.0)
        self.assert_ready()

    def test_fetch_expiring_before_response_holds_and_retries_without_caching(self):
        self.begin_recovery()
        original_info = self.reader._info

        def expired_read(payload):
            result = original_info(payload)
            self.monotonic_now += 60.0
            return result

        with patch.object(self.reader, "_info", side_effect=expired_read):
            self.run_cycle([fixtures.snapshot()])
        self.assertEqual(self.monotonic_now, 1060.0)
        self.assertEqual(len(self.reader.requests), 1)
        self.assertEqual(self.engine.ownership_checks, {})
        self.assert_held()
        self.monotonic_now = 1061.0
        self.run_cycle(self.source)
        self.assertEqual(len(self.reader.requests), 2)
        self.assertEqual(self.proof().fetched_at, 1061.0)
        self.assert_ready()

    def test_xyz_alias_later_fill_blocks_exit_without_losing_source_provenance(self):
        self.begin_recovery(market="xyz:INTC", dex="xyz")
        self.client.rows[("xyz:INTC", "xyz")]["coin"] = "INTC"
        self.original_rows = deepcopy(self.client.rows)
        self.reader.history = [self.later_fill(coin="BTC")]
        self.run_cycle(self.source)
        original = self.proof()
        self.assertEqual(self.market, "xyz:INTC|xyz")
        self.assert_ready()
        self.reader.history = [self.later_fill(coin="INTC", dex="xyz")]
        self.monotonic_now = 1060.0
        self.run_cycle([fixtures.snapshot()])
        self.assertEqual(len(self.reader.requests), 2)
        self.assertIs(self.proof(), original)
        self.assert_held()

    def test_negative_monotonic_age_requires_fresh_evidence_not_cache_hit(self):
        original = self.establish_proof()
        self.reader.history = [self.later_fill()]
        self.monotonic_now = 999.0
        self.run_cycle([fixtures.snapshot()])
        self.assertEqual(len(self.reader.requests), 2)
        self.assertIs(self.proof(), original)
        self.assert_held()


if __name__ == "__main__":
    unittest.main()
