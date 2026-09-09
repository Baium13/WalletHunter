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
        self.assertEqual(self.engine.retire_candidates(NOW),0)
        self.assertEqual(self.engine.retire_candidates(NOW+7*86400000),8)
        with self.engine.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],10)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidates WHERE status='ARCHIVED'").fetchone()[0],8)
            for address in self.wallets[:2]:
                self.assertNotEqual(db.execute('SELECT status FROM candidates WHERE wallet=?',(address,)).fetchone()[0],'ARCHIVED')
        self.assertEqual(self.engine.retire_candidates(NOW),0)
    def test_fresh_reentry_no_duplicate_and_no_old_event_replay(self):
        archived_at=NOW+7*86400000
        self.engine.retire_candidates(archived_at)
        with self.engine.store.transaction() as db:
            address=db.execute("SELECT wallet FROM candidates WHERE status='ARCHIVED' LIMIT 1").fetchone()[0]
        t=archived_at+1800001
        self.engine.observe([{'time':t,'users':[address,self.wallets[0]]}],t)
        with self.engine.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],10)
            self.assertEqual(db.execute('SELECT status FROM candidates WHERE wallet=?',(address,)).fetchone()[0],'DISCOVERED')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candidate_retirements').fetchone()[0],9)
    def test_fair_oldest_slot_survives_restart(self):
        for _ in range(3):self.engine.scheduled_candidates(NOW)
        restored=WalletDiscoveryEngine(self.engine.store.path,'TESTNET',self.engine.policy)
        self.assertEqual(restored.scheduled_candidates(NOW)[0]['wallet'],self.wallets[9])
        q=restored.candidate_queue(NOW)
        self.assertEqual(q['size'],10)
        self.assertGreaterEqual(q['p95_ms'],q['p50_ms'])

    def test_research_hot_archive_keeps_original_replay_rows(self):
        with self.engine.store.transaction() as db:
            db.executemany('INSERT INTO intelligence_records VALUES(?,?,?,?,?)',
                ((str(i),'TESTNET','WALLET_DISCOVERED',NOW-8*86400000,'{}') for i in range(100000)))
            self.engine._record(db,'LEADER_ANALYZED',{'test':'new'},NOW)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intelligence_records').fetchone()[0],100001)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM research_archive').fetchone()[0],20000)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM research_hot').fetchone()[0],80001)
            self.assertEqual(db.execute("SELECT body FROM intelligence_records WHERE id='1'").fetchone()[0],'{}')

class HistoryTests(unittest.TestCase):
    def test_incremental_tail_does_not_refresh_by_cache(self):
        from core.intelligence.history import IncrementalHistory
        from core.foundation.store import Store
        from test_trade_analyzer import fill
        with tempfile.TemporaryDirectory() as tmp:
            h=IncrementalHistory(Store(Path(tmp)/'h.sqlite'),'TESTNET');calls=[]
            rows=[fill(1,time=1000),fill(2,time=100000)]
            def info(p):
                calls.append(p)
                return [r for r in rows if p['startTime']<=r['time']<=p['endTime']]
            self.assertEqual(len(h.fetch(info,'wallet',0,100000,2,'deep')),2)
            rows.append(fill(3,time=110000))
            self.assertEqual(len(h.fetch(info,'wallet',0,110000,2,'deep')),3)
            self.assertEqual(calls[-1]['startTime'],40000)
            self.assertEqual(calls[-1]['endTime'],110000)
    def test_resume_partial_pages_and_network_isolation(self):
        from core.intelligence.history import IncrementalHistory
        from core.foundation.store import Store
        from core.fill_history import HistoryIncomplete
        from test_trade_analyzer import fill
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'h.sqlite');h=IncrementalHistory(store,'TESTNET');calls=[]
            rows=[fill(i+1,time=i) for i in range(2000)]
            def info(p):
                calls.append((p['startTime'],p['endTime']))
                return [r for r in rows if p['startTime']<=r['time']<=p['endTime']][:2000]
            with self.assertRaises(HistoryIncomplete):h.fetch(info,'wallet',0,4000,2,'deep')
            first=list(calls)
            got=IncrementalHistory(store,'TESTNET').fetch(info,'wallet',0,4000,8,'deep')
            self.assertEqual(len(got),2000)
            self.assertFalse(set(first)&set(calls[len(first):]))
            self.assertEqual(IncrementalHistory(store,'MAINNET').fetch(lambda p:[],'wallet',0,4000,2,'deep'),[])

class StreamTests(unittest.TestCase):
    def test_continuous_reader_does_not_wait_for_next_cycle(self):
        import json,queue,threading
        from unittest.mock import patch
        import websocket
        from core.intelligence.service import PublicTrades
        class Socket:
            def __init__(self):
                self.items=queue.Queue();self.consumed=threading.Event();self.count=0
                for i in range(30):self.items.put(json.dumps({'channel':'trades','data':[{'time':NOW+i,'px':'1','sz':'1','tid':i,'users':['a','b']}]}))
            def send(self,value):pass
            def settimeout(self,value):pass
            def close(self):pass
            def recv(self):
                try:result=self.items.get(timeout=.02)
                except queue.Empty:raise websocket.WebSocketTimeoutException()
                self.count+=1
                if self.count==30:self.consumed.set()
                return result
        sock=Socket();stream=PublicTrades('TESTNET')
        with patch('websocket.create_connection',return_value=sock),patch('core.hl_budget.configured',return_value=None):
            first=stream.poll()
            self.assertTrue(sock.consumed.wait(1))
            stream.close()
            self.assertEqual(len(first)+len(stream.buffer.take()),30)
            self.assertEqual(stream.messages_received,30)

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

class OperationalAlertTests(unittest.TestCase):
    def test_critical_budget_only_persistent_and_restart_dedup(self):
        from core.product_events import ProductEvents
        from core.foundation.contracts import Scope
        with tempfile.TemporaryDirectory() as tmp:
            now=[NOW];path=Path(tmp)/'p.sqlite';scope=Scope(tenant='1',account='0x'+'a'*40,network='TESTNET')
            p=ProductEvents(path,lambda:now[0])
            def signal(state):p.budget_alert(scope,{'state':state,'checked_ms':now[0]})
            signal('ELEVATED');signal('RATE_LIMITED')
            with p.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM product_outbox').fetchone()[0],0)
            now[0]+=120001;signal('RATE_LIMITED')
            p=ProductEvents(path,lambda:now[0]);signal('RATE_LIMITED')
            with p.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM product_outbox').fetchone()[0],1)
    def test_cache_bounded_without_timestamp_refresh(self):
        from core.bounded_cache import BoundedCache
        c=BoundedCache(2);c['a']=(1,'old');c['b']=(2,'value')
        self.assertEqual(c.get('a')[0],1)
        c['c']=(3,'new');self.assertNotIn('a',c);self.assertEqual(len(c),2)
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
