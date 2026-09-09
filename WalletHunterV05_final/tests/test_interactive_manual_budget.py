import tempfile
import unittest
import contextvars
from pathlib import Path
from unittest.mock import patch
from core.hl_budget import Budget,BudgetUnavailable
from core.interactive_budget import manual_review_budget,admission


class InteractiveBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.b=Budget(Path(self.tmp.name)/'budget.sqlite')
        patcher=patch('core.hl_budget.configured',return_value=self.b)
        patcher.start();self.addCleanup(patcher.stop)

    def test_temporary_total_1000_with_240_safety_reserve(self):
        with manual_review_budget():
            for _ in range(38):self.b.begin('userAbstraction',{},'manual',2)
            with self.assertRaises(BudgetUnavailable):self.b.begin('userAbstraction',{},'manual',2)
            # Distinct contexts simulate other processes: background pauses,
            # while safety remains eligible for all 240 reserved units.
            other=contextvars.Context()
            with self.assertRaises(BudgetUnavailable):other.run(self.b.begin,'orderStatus',{},'discovery',3)
            for _ in range(120):other.run(self.b.begin,'orderStatus',{},'reconciliation',0)
            with self.assertRaises(BudgetUnavailable):other.run(self.b.begin,'orderStatus',{},'reconciliation',0)

    def test_expiry_exception_and_contention_restore_background(self):
        with self.assertRaisesRegex(ValueError,'test'):
            with manual_review_budget():
                with self.assertRaises(BudgetUnavailable):
                    contextvars.Context().run(lambda:manual_review_budget().__enter__())
                raise ValueError('test')
        self.b.begin('orderStatus',{},'background',3)
        with self.b.db() as db:
            row=db.execute('SELECT expires,safety_until FROM interactive_budget').fetchone()
            self.assertIsNone(admission(db,row[1]+1,0))

    def test_raised_usage_does_not_block_safety_on_lease_release(self):
        with manual_review_budget():
            for _ in range(38):self.b.begin('userAbstraction',{},'manual',2)
            for _ in range(50):self.b.begin('orderStatus',{},'safety',0)
        # 860 used: ordinary hard840 would block; transient safety ceiling1000
        # remains until these requests age out of the minute.
        self.b.begin('orderStatus',{},'safety',0)
        with self.assertRaises(BudgetUnavailable):self.b.begin('orderStatus',{},'background',3)

    def test_start_uses_same_interactive_ceiling_after_legacy_600_is_exhausted(self):
        with manual_review_budget():
            for _ in range(36):self.b.begin('userAbstraction',{},'preview',2)
            self.b.begin('orderStatus',{},'preview',2)  # 722, above observed 721.
        with self.assertRaises(BudgetUnavailable):self.b.begin('orderStatus',{},'legacy_start',2)
        with manual_review_budget():
            self.b.begin('orderStatus',{},'manual_start.current_evidence',2)
        self.b.begin('orderStatus',{},'reconciliation',0)

if __name__=='__main__':unittest.main()
