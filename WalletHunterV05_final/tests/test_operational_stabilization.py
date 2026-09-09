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

class StreamTests(unittest.TestCase):
    def test_bounded_priority_dedup_and_order(self):
        import threading
        from core.intelligence.stream_buffer import TradeBuffer
        b=TradeBuffer(3);stop=threading.Event();b.protected=frozenset({'owned'})
        for i in range(10):b.put({'tid':i,'time':i,'users':['noise']},stop)
        b.put({'tid':20,'time':20,'users':['owned']},stop)
        b.put({'tid':20,'time':20,'users':['owned']},stop)
        self.assertEqual(b.metrics()['queue_depth'],3)
        self.assertEqual(b.metrics()['dedup'],1)
        self.assertEqual(b.metrics()['dropped_critical'],0)
        self.assertEqual(b.take(1)[0]['tid'],20)
        self.assertEqual([r['time'] for r in b.take()],[8,9])
    def test_critical_backpressure_no_loss(self):
        import threading,time
        from core.intelligence.stream_buffer import TradeBuffer
        b=TradeBuffer(1);b.protected=frozenset({'owned'});stop=threading.Event()
        b.put({'tid':1,'users':['owned']},stop)
        thread=threading.Thread(target=lambda:b.put({'tid':2,'users':['owned']},stop))
        thread.start();time.sleep(.02)
        self.assertTrue(thread.is_alive());self.assertEqual(b.take()[0]['tid'],1)
        thread.join(1);self.assertFalse(thread.is_alive());self.assertEqual(b.take()[0]['tid'],2)
        self.assertEqual(b.metrics()['dropped_critical'],0)

if __name__=='__main__':unittest.main()
