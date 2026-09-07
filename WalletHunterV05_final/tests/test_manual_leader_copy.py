import unittest
from core.manual_leader_copy import ManualLeaderConfig,ManualLeaderCopy
from core.foundation.contracts import Scope

class ManualLeaderCopyTests(unittest.TestCase):
    def setUp(self):
        self.scope=Scope(tenant='u1',account='0x'+'a'*40,network='TESTNET')
        self.copy=ManualLeaderCopy(ManualLeaderConfig(scope=self.scope,leader='0x'+'b'*40,alias='trusted',allocation_pct=80,created_ms=1,updated_ms=1))

    def test_proportional_target_and_reserve(self):
        self.assertAlmostEqual(self.copy.target_margin(leader_margin=10000,leader_capital=100000,allocatable_capital=100),8)
        self.assertAlmostEqual(self.copy.target_margin(leader_margin=10000,leader_capital=100000,allocatable_capital=100,fee_reserve_pct=10),7.2)

    def test_full_allocation_is_allocatable_not_zero_reserve(self):
        full=ManualLeaderCopy(self.copy.config.model_copy(update={'allocation_pct':100}))
        self.assertAlmostEqual(full.target_margin(leader_margin=10000,leader_capital=100000,allocatable_capital=100),10)

    def test_start_stop_retains_hold_by_default(self):
        started=self.copy.start(2); self.assertTrue(started.enabled)
        stopped,close=self.copy.stop(3); self.assertFalse(stopped.enabled); self.assertFalse(close)
        stopped,close=self.copy.stop(4,close_positions=True); self.assertTrue(close)

    def test_invalid_evidence_fails_closed(self):
        with self.assertRaises(ValueError): self.copy.target_margin(leader_margin=float('nan'),leader_capital=100,allocatable_capital=100)
        with self.assertRaises(ValueError): self.copy.target_margin(leader_margin=1,leader_capital=0,allocatable_capital=100)
        with self.assertRaises(ValueError): self.copy.target_margin(leader_margin=1,leader_capital=100,allocatable_capital=100,max_margin=float('inf'))

    def test_event_targets_are_proportional_and_deduplicated(self):
        active=self.copy.__class__(self.copy.config.model_copy(update={'enabled':True}))
        open_plan=active.plan_event('evt-1','OPEN',leader_margin=10000,leader_capital=100000,allocatable_capital=100)
        self.assertAlmostEqual(open_plan['target_margin'],8); self.assertAlmostEqual(open_plan['delta_margin'],8)
        add=active.plan_event('evt-2','ADD',leader_margin=20000,leader_capital=100000,allocatable_capital=100,current_margin=8)
        self.assertAlmostEqual(add['delta_margin'],8)
        reduce=active.plan_event('evt-3','REDUCE',leader_margin=5000,leader_capital=100000,allocatable_capital=100,current_margin=16)
        self.assertAlmostEqual(reduce['delta_margin'],-12)
        close=active.plan_event('evt-4','CLOSE',leader_margin=0,leader_capital=100000,allocatable_capital=100,current_margin=4)
        self.assertEqual(close['delta_margin'],-4)
        from core.manual_leader_copy import ManualLeaderEventBook
        book=ManualLeaderEventBook(); self.assertTrue(book.accept('evt-1')); self.assertFalse(book.accept('evt-1'))

    def test_stop_does_not_liquidate(self):
        stopped,_=self.copy.stop(4)
        plan=ManualLeaderCopy(stopped).plan_event('evt','OPEN',leader_margin=1,leader_capital=10,allocatable_capital=100,current_margin=5)
        self.assertEqual(plan['status'],'STOPPED'); self.assertEqual(plan['target_margin'],5)
