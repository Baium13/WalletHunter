"""Confirmed AI entries must never become copy-source positions. Fake orders."""
import unittest
from unittest.mock import Mock

from core.trading_engine import CopyEngine
from tests import test_engine_safety as fixtures


class EngineAiUserHoldTests(unittest.TestCase):
    setUp = fixtures.EngineSafetyTests.setUp
    runtime = fixtures.EngineSafetyTests.runtime
    patch = fixtures.EngineSafetyTests.patch
    run_cycle = fixtures.EngineSafetyTests.run_cycle
    managed = fixtures.EngineSafetyTests.managed

    def test_runtime_hold_prevents_add_reverse_exit_and_risk_close(self):
        self.managed(fixtures.position(roe=-90))
        self.runtime(ai_user_order_holds={'BTC|': {'status': 'FILLED'}})
        self.patch(leader_exit_only=False)
        for snapshots in ([fixtures.snapshot()],
                          [fixtures.snapshot(positions=[fixtures.position(notional=2000)])],
                          [fixtures.snapshot(positions=[fixtures.position(side='SHORT', notional=1000)])]):
            self.run_cycle(snapshots)
            self.assertEqual(self.client.calls, [])

    def test_account_wide_sql_hold_works_without_runtime_after_restart(self):
        self.managed(fixtures.position())
        self.engine = CopyEngine(fixtures.Reader(), self.store, self.settings)
        self.engine.ai_user_orders.reserved_markets = Mock(return_value={'BTC|': {'status': 'UNKNOWN'}})
        self.run_cycle([fixtures.snapshot()])
        self.engine.ai_user_orders.reserved_markets.assert_called_once_with(None, fixtures.ACCOUNT)
        self.assertEqual(self.client.calls, [])

    def test_hold_precedes_stale_journal_recovery(self):
        row = fixtures.position()
        self.client.seed(row)
        op = self.engine.journal.prepare(fixtures.ACCOUNT, 'BTC|', {'action': 'old-copy'})
        self.engine.journal.finish(op, {'ok': True}, {'managed': True, 'size': row['size'],
            'side': row['side'], 'position': row, 'source_targets': []})
        self.engine.ai_user_orders.reserved_markets = Mock(return_value={'BTC|': {'status': 'FILLED'}})
        self.run_cycle([fixtures.snapshot()])
        self.assertEqual(self.store.profile(1)[1]['runtime']['managed'], [])
        self.assertEqual(self.client.calls, [])

    def test_ledger_failure_cannot_allow_an_order(self):
        self.engine.ai_user_orders.reserved_markets = Mock(side_effect=RuntimeError('ledger unavailable'))
        with self.assertRaises(RuntimeError):
            self.run_cycle()
        self.assertEqual(self.client.calls, [])

    def test_position_intervention_runtime_hold_blocks_copy_resizing(self):
        self.managed(fixtures.position(roe=-90))
        self.runtime(ai_position_action_holds={'BTC|': {'status': 'FILLED'}})
        self.patch(leader_exit_only=False)
        self.run_cycle([fixtures.snapshot()])
        self.assertEqual(self.client.calls, [])

    def test_position_intervention_sql_hold_survives_runtime_loss(self):
        self.managed(fixtures.position())
        self.engine = CopyEngine(fixtures.Reader(), self.store, self.settings)
        self.engine.ai_position_actions.reserved_markets = Mock(return_value={'BTC|': {'status': 'UNKNOWN'}})
        self.run_cycle([fixtures.snapshot()])
        self.engine.ai_position_actions.reserved_markets.assert_called_once_with(None, fixtures.ACCOUNT)
        self.assertEqual(self.client.calls, [])

    def test_position_intervention_ledger_failure_is_fail_closed(self):
        self.engine.ai_position_actions.reserved_markets = Mock(side_effect=RuntimeError('ledger unavailable'))
        with self.assertRaises(RuntimeError):self.run_cycle()
        self.assertEqual(self.client.calls, [])

    def test_live_hold_does_not_contaminate_paper_simulation(self):
        self.settings.auto_trading = False
        self.runtime(ai_user_order_holds={'BTC|': {'status': 'FILLED'}})
        self.engine.ai_user_orders.reserved_markets = Mock(side_effect=AssertionError('live ledger read in PAPER'))
        result = self.run_cycle()
        self.assertTrue(any(item.action == 'OPEN' and item.paper for item in result))
        self.assertEqual(self.client.calls, [])
