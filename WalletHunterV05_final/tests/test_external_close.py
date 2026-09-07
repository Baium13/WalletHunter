"""An exchange stop/manual close must not be undone by the copy target."""
import unittest
from tests import test_engine_safety as fixtures


class ExternalCloseTests(unittest.TestCase):
    setUp=fixtures.EngineSafetyTests.setUp
    run_cycle=fixtures.EngineSafetyTests.run_cycle
    trades=fixtures.EngineSafetyTests.trades

    def test_stop_filled_flat_stays_flat_until_explicit_resume(self):
        source=[fixtures.snapshot(positions=[fixtures.position(notional=1000)])]
        self.run_cycle(source)
        self.client.rows.clear()
        self.client.calls.clear()
        self.run_cycle(source)
        runtime=self.store.profile(1)[1]["runtime"]
        self.assertEqual(self.trades(),[])
        self.assertEqual(runtime["managed"],[])
        self.assertIn("BTC|",runtime["manual_hold_keys"])
        self.assertFalse(self.engine.journal.owned(fixtures.ACCOUNT)["BTC|"]["managed"])
        self.run_cycle(source)
        self.assertEqual(self.trades(),[])

    def test_unknown_close_is_not_reclassified_as_observed_success(self):
        source=[fixtures.snapshot(positions=[fixtures.position(notional=1000)])]
        self.run_cycle(source)
        operation=self.engine.journal.prepare(fixtures.ACCOUNT,"BTC|",{"action":"CLOSE"})
        self.client.rows.clear();self.client.calls.clear()
        self.run_cycle(source)
        self.assertIn("BTC|",self.engine.journal.pending(fixtures.ACCOUNT))
        self.assertEqual(self.trades(),[])


if __name__=="__main__":unittest.main()
