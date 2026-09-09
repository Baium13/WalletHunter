"""Real discovery services, isolated public evidence, never live execution."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from core.intelligence.service import WalletDiscoveryEngine
from core.intelligence.discovery_operations import DiscoveryOperations,high_frequency,supported_account
from test_intelligence import NOW,ADDRESS,PublicFixture
import test_intelligence as fixtures
from test_trade_analyzer import fill


class ContinuousDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.engine=WalletDiscoveryEngine(Path(self.temp.name)/'research.sqlite','TESTNET')
    def seed(self,n,status='DISCOVERED',fresh=False):
        proof=json.dumps({'score':{'qualified':True,'computed_ms':NOW}}) if fresh else None
        with self.engine.store.transaction() as db:
            db.executemany('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)',
                [('TESTNET','0x'+format(i,'040x'),NOW,NOW,status,NOW+100000 if fresh else NOW,0,.8,.8,proof) for i in range(n)])
    def test_10000_wallets_do_not_stop_admission_and_restart(self):
        self.seed(10000)
        trade={'time':NOW,'users':[ADDRESS,'0x'+'b'*40]}
        self.engine.observe([trade],NOW)
        with self.engine.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],10002)
        restarted=WalletDiscoveryEngine(self.engine.store.path,'TESTNET');restarted.observe([trade],NOW)
        with restarted.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],10002)
        self.assertEqual(WalletDiscoveryEngine(self.engine.store.path,'MAINNET').snapshot(NOW)['counts'],{})
    def test_200_verified_candidates_make_new_research_idle_only(self):
        self.seed(200,'QUALIFIED',fresh=True)
        self.engine.observe([{'time':NOW,'users':[ADDRESS,'0x'+'b'*40]}],NOW)
        with patch('core.intelligence.discovery_operations.resources',return_value={'idle':False,'allow_research':True,'reasons':[]}):
            state=self.engine.research_capacity(NOW)
        self.assertEqual(state['potential_count'],200);self.assertEqual(state['discovery_mode'],'IDLE_ONLY')
        self.assertFalse(self.engine.scheduled_candidates(NOW,state))
        self.assertTrue(self.engine.scheduled_candidates(NOW,{**state,'idle':True}))
        self.assertFalse(self.engine.scheduled_candidates(NOW,{**state,'allow_research':False}))
    def test_expired_score_not_counted_even_if_retry_advanced_next_eval(self):
        self.seed(200,'QUALIFIED',fresh=True)
        self.assertEqual(self.engine.research_capacity(NOW+86400000)['potential_count'],0)
    def test_high_frequency_is_distinct_orders_not_partial_fill_fragments(self):
        rows=[dict(fill(i+1,time=NOW-3600000+i*2000),oid=i+1) for i in range(1000)]
        self.assertTrue(high_frequency(rows,NOW,self.engine.operations)['suspected'])
        self.assertFalse(high_frequency([dict(r,oid=1) for r in rows],NOW,self.engine.operations)['suspected'])
        self.assertFalse(high_frequency([dict(r,time=NOW) for r in rows],NOW,self.engine.operations)['suspected'])
    def test_bot_sector_no_deep_history_no_new_admission_after_trade(self):
        self.engine.observe([{'time':NOW,'users':[ADDRESS,'0x'+'b'*40]}],NOW)
        rows=[dict(fill(i+1,time=NOW-3600000+i*2000),oid=i+1) for i in range(1000)];calls=[]
        self.engine.analyze_one(ADDRESS,lambda p:(calls.append(p) or rows),NOW)
        self.assertEqual(len(calls),1)
        self.engine.observe([{'time':NOW+1,'users':[ADDRESS,'0x'+'b'*40]}],NOW+1)
        with self.engine.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM candidates WHERE wallet=?',(ADDRESS,)).fetchone()[0],'HIGH_FREQUENCY')
            self.assertGreater(db.execute('SELECT recheck_ms FROM candidate_segments WHERE wallet=?',(ADDRESS,)).fetchone()[0],NOW+86400000)
        self.engine.promote(NOW)
        self.assertNotIn('ACTIVE',self.engine.snapshot(NOW)['counts'])
    def test_zero_supported_capital_cold_and_can_reenter_on_fresh_recheck(self):
        self.engine.observe([{'time':NOW,'users':[ADDRESS,'0x'+'b'*40]}],NOW)
        fixture=PublicFixture();original=fixture._info
        def empty(p):
            if p['type']=='clearinghouseState':return {'time':NOW,'assetPositions':[],'marginSummary':{'accountValue':'0'}}
            return original(p)
        self.assertIsNone(self.engine.analyze_one(ADDRESS,empty,NOW))
        with self.engine.store.transaction() as db:self.assertEqual(db.execute('SELECT status FROM candidates WHERE wallet=?',(ADDRESS,)).fetchone()[0],'ZERO_SUPPORTED_CAPITAL')
        self.assertIsNotNone(self.engine.analyze_one(ADDRESS,original,NOW))
        with self.engine.store.transaction() as db:self.assertEqual(db.execute('SELECT sector FROM candidate_segments WHERE wallet=?',(ADDRESS,)).fetchone()[0],'NORMAL')
    def test_missing_or_nonfinite_balance_never_zero(self):
        for value in (None,'NaN','Infinity'):
            def bad(p):return {'time':NOW,'assetPositions':[],'marginSummary':{'accountValue':value}}
            with self.assertRaises((ValueError,TypeError)):supported_account(bad,ADDRESS,NOW)
    def test_inactive_sector_requires_empty_current_positions(self):
        self.seed(1)
        address='0x'+'0'*40
        with self.engine.store.transaction() as db:db.execute('UPDATE candidates SET last_seen=?',(NOW-8*86400000,))
        def inactive(p):
            if p['type']=='userFillsByTime':return []
            if p['type']=='spotClearinghouseState':return {'balances':[]}
            return {'time':NOW,'marginSummary':{'accountValue':'10'},'assetPositions':[]}
        self.engine.analyze_one(address,inactive,NOW)
        with self.engine.store.transaction() as db:self.assertEqual(db.execute('SELECT status FROM candidates').fetchone()[0],'INACTIVE')
    def test_new_paper_decision_runs_before_slow_optional_history(self):
        case=fixtures.IntelligenceTests();case.setUp();self.addCleanup(case.doCleanups)
        backend,record=case.paper_backend();order=[]
        # Existing persisted DECISION must be drained first, as production does.
        backend.drain(case.worker);self.assertEqual(backend.exchange.calls,1)
        # New CLOSE is the next real leader event; no synthetic production data.
        clock=NOW+1000;case.reader.fills.append(fill(4000,time=clock,side='A',start='1',size='1',pnl='1'))
        case.reader._info_original=case.reader._info
        def fresh(p):
            x=case.reader._info_original(p)
            if p['type']=='l2Book':x={**x,'time':clock}
            return x
        case.reader._info=fresh
        backend.clock=lambda:clock
        def drain():order.append('PAPER');backend.drain(case.worker)
        with patch.object(case.worker,'analyze_one',side_effect=lambda *a,**kw:order.append('HISTORY')):
            case.worker.cycle(case.reader,[],clock,clock=lambda:clock,on_decision=drain)
        self.assertIn('PAPER',order)
        if 'HISTORY' in order:self.assertLess(order.index('PAPER'),order.index('HISTORY'))
        self.assertEqual(backend.exchange.calls,2)
        with backend.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_outcomes').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM calibration_records').fetchone()[0],1)

if __name__=='__main__':unittest.main()
