"""Operational changes: isolated stores and no exchange transport."""
import tempfile
import unittest
from pathlib import Path
from core.intelligence.service import WalletDiscoveryEngine
from core.intelligence.models import IntelligencePolicy

NOW=1800000000000

class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine=WalletDiscoveryEngine(Path(self.temp.name)/'research.sqlite','TESTNET',IntelligencePolicy(registry_limit=10))
        self.wallets=['0x'+format(i,'040x') for i in range(12)]
        with self.engine.store.transaction() as db:
            for i,address in enumerate(self.wallets[:10]):
                db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)',('TESTNET',address,NOW-86400000,NOW-3600000,'ACTIVE' if i==0 else 'PROBATION',NOW-10000-i,0,None,None,None))
    def test_archive_preserves_protected_history_and_identity(self):
        self.engine.position_owned_leaders={self.wallets[1]}
        self.assertEqual(self.engine.retire_candidates(NOW),2)
        with self.engine.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],10)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidates WHERE status='ARCHIVED'").fetchone()[0],2)
            for address in self.wallets[:2]:
                self.assertNotEqual(db.execute('SELECT status FROM candidates WHERE wallet=?',(address,)).fetchone()[0],'ARCHIVED')
        self.assertEqual(self.engine.retire_candidates(NOW),0)
    def test_fresh_reentry_no_duplicate_and_no_old_event_replay(self):
        self.engine.retire_candidates(NOW)
        with self.engine.store.transaction() as db:
            address=db.execute("SELECT wallet FROM candidates WHERE status='ARCHIVED' LIMIT 1").fetchone()[0]
        t=NOW+1800001
        self.engine.observe([{'time':t,'users':[address,self.wallets[0]]}],t)
        with self.engine.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],10)
            self.assertEqual(db.execute('SELECT status FROM candidates WHERE wallet=?',(address,)).fetchone()[0],'DISCOVERED')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candidate_retirements').fetchone()[0],2)
    def test_fair_oldest_slot_survives_restart(self):
        for _ in range(3):self.engine.scheduled_candidates(NOW)
        restored=WalletDiscoveryEngine(self.engine.store.path,'TESTNET',self.engine.policy)
        self.assertEqual(restored.scheduled_candidates(NOW)[0]['wallet'],self.wallets[9])
        q=restored.candidate_queue(NOW)
        self.assertEqual(q['size'],10)
        self.assertGreaterEqual(q['p95_ms'],q['p50_ms'])

if __name__=='__main__':unittest.main()
