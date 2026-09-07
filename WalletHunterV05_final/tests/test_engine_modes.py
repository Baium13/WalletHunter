"""Mode boundaries and persistence use temporary storage and a fake exchange."""
import asyncio
import unittest

from core.trading_engine import CopyEngine
from tests import test_engine_safety as fixtures

Reader, ACCOUNT = fixtures.Reader, fixtures.ACCOUNT
position, snapshot = fixtures.position, fixtures.snapshot


class EngineModeTests(unittest.TestCase):
    setUp = fixtures.EngineSafetyTests.setUp
    runtime = fixtures.EngineSafetyTests.runtime
    patch = fixtures.EngineSafetyTests.patch
    run_cycle = fixtures.EngineSafetyTests.run_cycle
    trades = fixtures.EngineSafetyTests.trades
    managed = fixtures.EngineSafetyTests.managed

    def paper(self):
        self.settings.auto_trading = False

    def test_paper_open_never_marks_live_position_managed(self):
        self.runtime(managed=["ETH|"], journal=[{"time": 1, "action": "LIVE_EVENT"}])
        self.client.fail_positions = True  # A paper book must not read live positions.
        self.paper()
        result = self.run_cycle()
        runtime = self.store.profile(1)[1]["runtime"]
        self.assertTrue(any(r.action == "OPEN" and r.paper for r in result))
        self.assertEqual(runtime["managed"], ["ETH|"])
        self.assertEqual(runtime["journal"], [{"time": 1, "action": "LIVE_EVENT"}])
        self.assertEqual(runtime["paper_runtime"]["managed"], ["BTC|"])
        self.assertEqual(self.engine.journal.owned(ACCOUNT), {})
        self.assertEqual(self.client.calls, [])

    def test_paper_position_survives_engine_restart_without_duplicate_open(self):
        self.paper()
        self.run_cycle()
        self.engine = CopyEngine(Reader(), self.store, self.settings)
        result = self.run_cycle()
        self.assertEqual(result, [])
        book = self.store.profile(1)[1]["runtime"]["paper_runtime"]["positions"]
        self.assertAlmostEqual(book["BTC|"]["size"], 1.)
        self.assertEqual(self.client.calls, [])

    def test_paper_close_does_not_remove_live_ownership(self):
        self.runtime(managed=["ETH|"])
        self.paper()
        self.run_cycle()
        result = self.run_cycle([snapshot()])
        self.assertTrue(any(r.action == "FULL_CLOSE" and r.paper for r in result))
        runtime = self.store.profile(1)[1]["runtime"]
        self.assertEqual(runtime["managed"], ["ETH|"])
        self.assertEqual(runtime["paper_runtime"]["managed"], [])
        self.assertEqual(runtime["paper_runtime"]["positions"], {})

    def test_switching_live_does_not_adopt_manual_position_from_paper_flag(self):
        self.paper()
        self.run_cycle()
        self.settings.auto_trading = True
        self.client.seed(position(notional=50))
        result = self.run_cycle()
        self.assertTrue(any(r.action == "MANUAL_POSITION" for r in result))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], [])

    def test_replacing_account_resets_only_paper_book_even_if_id_reused(self):
        self.paper()
        self.run_cycle()
        _, profile = self.store.profile(1)
        changed = dict(profile["account"], address="0x" + "d" * 40)
        self.patch(account=changed)
        result = self.run_cycle()
        self.assertTrue(any(r.action == "OPEN" for r in result))
        book = self.store.profile(1)[1]["runtime"]["paper_runtime"]
        self.assertEqual(book["account"], changed["address"])

    def test_paper_emergency_cannot_clear_live_ownership_or_stops(self):
        self.runtime(managed=["ETH|"], manual_stops={"ETH|": {"oid": 123}})
        self.paper()
        self.run_cycle()
        _, profile = self.store.profile(1)
        asyncio.run(self.engine.emergency_stop(1, profile, self.client, True))
        runtime = self.store.profile(1)[1]["runtime"]
        self.assertEqual(runtime["managed"], ["ETH|"])
        self.assertEqual(runtime["manual_stops"], {"ETH|": {"oid": 123}})
        self.assertEqual(runtime["paper_runtime"]["positions"], {})
        self.assertEqual(self.client.calls, [])

    def test_daily_limit_closes_old_reverse_leg_but_never_opens_new_side(self):
        self.managed(position(notional=100, side="LONG"))
        self.client.pnl = -100
        self.run_cycle([snapshot(positions=[position(notional=1000, side="SHORT")])])
        self.assertEqual([call[0] for call in self.trades()], ["close"])
        self.assertNotIn(("BTC", ""), self.client.rows)
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], [])

    def test_failed_telegram_delivery_is_persisted_for_later_retry(self):
        async def failing(*args):
            raise RuntimeError("Telegram unavailable")
        self.run_cycle(notify=failing)
        runtime = self.store.profile(1)[1]["runtime"]
        self.assertEqual(runtime["managed"], ["BTC|"])
        self.assertTrue(runtime["pending_notifications"])
        self.assertEqual(runtime["pending_notifications"][0]["result"]["action"], "OPEN")
        self.assertTrue(any(row["action"] == "OPEN" for row in runtime["journal"]))
        self.runtime(pending_notifications=[dict(runtime["pending_notifications"][0], next_retry=0)])
        self.engine = CopyEngine(Reader(), self.store, self.settings)
        self.run_cycle()
        self.assertEqual(self.store.profile(1)[1]["runtime"]["pending_notifications"], [])
        self.assertEqual(len(self.trades()), 1)


if __name__ == "__main__":
    unittest.main()
