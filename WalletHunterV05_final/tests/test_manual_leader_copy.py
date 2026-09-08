import unittest
import time
import tempfile
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

    def test_real_open_uses_manual_allocation_and_canonical_gateway(self):
        # This is the real copy gateway boundary used by a Manual Leader event;
        # only the exchange client is the deterministic fixture.
        import test_engine_safety as fixture
        from core.manual_leader_copy import execute_manual_leader
        case = fixture.EngineSafetyTests(methodName='runTest')
        case.setUp()
        try:
            operation = case.engine.journal.prepare(fixture.ACCOUNT, 'BTC|', {
                'action': 'MANUAL_LEADER_OPEN', 'network': 'TESTNET',
                'strategy': 'MANUAL_LEADER_COPY', 'leader': fixture.SOURCE_A})
            receipt = execute_manual_leader(
                engine=case.engine,
                account={'_tenant': '1', 'address': fixture.ACCOUNT, '_sources': ()},
                client=case.client, operation=operation, event_id='manual-open-1', action='OPEN',
                leader_margin=10000., leader_capital=100000., allocatable_capital=100.,
                current_margin=0., spec={'leader': fixture.SOURCE_A, 'alias': 'A',
                    'allocation_pct': 80., 'coin': 'BTC', 'side': 'LONG', 'leverage': 5,
                    'market_type': 'CRYPTO', 'entry_price': 100.})
            self.assertEqual(receipt.status, 'FILLED')
            self.assertEqual([call[0] for call in case.client.calls], ['leverage', 'open'])
            owned = case.engine.journal.owned(fixture.ACCOUNT)['BTC|']
            self.assertEqual(owned['source_targets'][0]['wallet'], fixture.SOURCE_A)
            self.assertAlmostEqual(owned['source_targets'][0]['margin'], 8.)
            self.assertFalse(case.engine.journal.pending(fixture.ACCOUNT))
        finally:
            case.doCleanups()

    def test_event_identity_survives_restart(self):
        from core.manual_leader_copy import ManualLeaderEventBook
        with tempfile.TemporaryDirectory() as root:
            path = root + '/events.sqlite3'
            first = ManualLeaderEventBook(path)
            self.assertTrue(first.accept('leader-event-1'))
            second = ManualLeaderEventBook(path)
            self.assertFalse(second.accept('leader-event-1'))

    def test_event_identity_isolated_by_scope(self):
        from core.manual_leader_copy import ManualLeaderEventBook
        book = ManualLeaderEventBook()
        self.assertTrue(book.accept('same-exchange-id', scope=self.scope))
        other = self.scope.model_copy(update={'tenant': 'u2'})
        self.assertTrue(book.accept('same-exchange-id', scope=other))

    def test_older_leader_event_cannot_reopen_newer_state(self):
        from core.manual_leader_copy import ManualLeaderEventBook
        book = ManualLeaderEventBook()
        self.assertTrue(book.accept('close-new', scope=self.scope, event_ms=200))
        self.assertFalse(book.accept('open-old', scope=self.scope, event_ms=100))

    def test_copy_actions_are_proportional_and_provenanced(self):
        import test_engine_safety as fixture
        from core.manual_leader_copy import execute_manual_leader
        case = fixture.EngineSafetyTests(methodName='runTest')
        case.setUp()
        account = {'_tenant': '1', 'address': fixture.ACCOUNT, '_sources': ()}
        common = {'leader': fixture.SOURCE_A, 'alias': 'A', 'allocation_pct': 80.,
                  'coin': 'BTC', 'side': 'LONG', 'leverage': 5, 'market_type': 'CRYPTO'}
        try:
            def execute(event, action, leader_margin, current_margin, before=None, **extra):
                op = case.engine.journal.prepare(fixture.ACCOUNT, 'BTC|', {
                    'action': 'MANUAL_LEADER_' + action, 'network': 'TESTNET',
                    'strategy': 'MANUAL_LEADER_COPY', 'leader': fixture.SOURCE_A})
                return execute_manual_leader(engine=case.engine, account=account,
                    client=case.client, operation=op, event_id=event, action=action,
                    leader_margin=leader_margin, leader_capital=100000.,
                    allocatable_capital=100., current_margin=current_margin,
                    spec=dict(common, **extra), before_position=before)

            opened = execute('open-1', 'OPEN', 10000., 0.)
            self.assertEqual(opened.status, 'FILLED')
            position = case.client.rows[('BTC', '')]
            added = execute('add-1', 'ADD', 20000., 8., position)
            self.assertEqual(added.status, 'FILLED')
            self.assertAlmostEqual(case.client.rows[('BTC', '')]['size'], .8)
            reduced_from = case.client.rows[('BTC', '')]
            reduced = execute('reduce-1', 'REDUCE', 10000., 16., reduced_from)
            self.assertEqual(reduced.status, 'FILLED')
            self.assertAlmostEqual(case.client.rows[('BTC', '')]['size'], .4)
            closed_from = case.client.rows[('BTC', '')]
            closed = execute('close-1', 'CLOSE', 0., 8., closed_from)
            self.assertEqual(closed.status, 'FILLED')
            self.assertNotIn(('BTC', ''), case.client.rows)
            self.assertFalse(case.engine.journal.pending(fixture.ACCOUNT))
            self.assertFalse(case.engine.journal.owned(fixture.ACCOUNT)['BTC|']['managed'])
        finally:
            case.doCleanups()

    def test_reverse_unknown_close_never_opens_opposite_side(self):
        import test_engine_safety as fixture
        from core.manual_leader_copy import execute_manual_leader
        from unittest.mock import patch
        case = fixture.EngineSafetyTests(methodName='runTest')
        case.setUp()
        account = {'_tenant': '1', 'address': fixture.ACCOUNT, '_sources': ()}
        try:
            op = case.engine.journal.prepare(fixture.ACCOUNT, 'BTC|', {
                'action': 'MANUAL_LEADER_OPEN', 'network': 'TESTNET',
                'strategy': 'MANUAL_LEADER_COPY', 'leader': fixture.SOURCE_A})
            execute_manual_leader(engine=case.engine, account=account, client=case.client,
                operation=op, event_id='rev-open', action='OPEN', leader_margin=10000.,
                leader_capital=100000., allocatable_capital=100., current_margin=0.,
                spec={'leader': fixture.SOURCE_A, 'allocation_pct': 80., 'coin': 'BTC',
                      'side': 'LONG', 'leverage': 5, 'market_type': 'CRYPTO'})
            position = case.client.rows[('BTC', '')]
            reverse_op = case.engine.journal.prepare(fixture.ACCOUNT, 'BTC|', {
                'action': 'MANUAL_LEADER_REVERSE', 'network': 'TESTNET',
                'strategy': 'MANUAL_LEADER_COPY', 'leader': fixture.SOURCE_A})
            with patch.object(case.client, 'submit_copy_ioc', side_effect=TimeoutError('unknown')) as submit:
                result = execute_manual_leader(engine=case.engine, account=account,
                    client=case.client, operation=reverse_op, event_id='rev-1', action='REVERSE',
                    leader_margin=10000., leader_capital=100000., allocatable_capital=100.,
                    current_margin=8., before_position=position,
                    spec={'leader': fixture.SOURCE_A, 'allocation_pct': 80., 'coin': 'BTC',
                          'side': 'SHORT', 'leverage': 5, 'market_type': 'CRYPTO'})
            self.assertEqual(result.status, 'UNKNOWN')
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(case.client.rows[('BTC', '')]['side'], 'LONG')
            self.assertIn('BTC|', case.engine.journal.pending(fixture.ACCOUNT))
        finally:
            case.doCleanups()

    def test_stopped_policy_holds_without_exchange_mutation(self):
        import test_engine_safety as fixture
        from core.manual_leader_copy import execute_manual_leader
        case = fixture.EngineSafetyTests(methodName='runTest')
        case.setUp()
        try:
            result = execute_manual_leader(engine=case.engine,
                account={'_tenant': '1', 'address': fixture.ACCOUNT, '_sources': ()},
                client=case.client, operation=None, event_id='stopped-1', action='OPEN',
                leader_margin=10000., leader_capital=100000., allocatable_capital=100.,
                current_margin=0., spec={'leader': fixture.SOURCE_A, 'allocation_pct': 80.,
                    'coin': 'BTC', 'side': 'LONG', 'leverage': 5,
                    'market_type': 'CRYPTO', 'enabled': False})
            self.assertEqual(result['status'], 'STOPPED')
            self.assertEqual(case.client.calls, [])
        finally:
            case.doCleanups()

    def test_service_persists_start_stop_and_rejects_leader_switch_adoption(self):
        import test_engine_safety as fixture
        from core.manual_leader_copy import ManualLeaderCopyService
        case = fixture.EngineSafetyTests(methodName='runTest')
        case.setUp()
        account = {'_tenant': '1', 'address': fixture.ACCOUNT}
        try:
            service = ManualLeaderCopyService(case.engine)
            configured = service.configure(account, case.client, fixture.SOURCE_A, 80., now=10)
            self.assertFalse(configured.enabled)
            self.assertTrue(service.start(account, case.client, now=11).enabled)
            restarted = ManualLeaderCopyService(case.engine)
            self.assertTrue(restarted.config(account, case.client).enabled)
            switched = restarted.configure(account, case.client, fixture.SOURCE_B, 60., now=12)
            self.assertFalse(switched.enabled)
            stopped, close = restarted.stop(account, case.client, now=13)
            self.assertFalse(stopped.enabled)
            self.assertFalse(close)
        finally:
            case.doCleanups()

    def test_partial_manual_fill_is_retained_for_query_only_recovery(self):
        import test_engine_safety as fixture
        from core.manual_leader_copy import execute_manual_leader, recover_pending_manual_leader
        from unittest.mock import patch
        case = fixture.EngineSafetyTests(methodName='runTest')
        case.setUp()
        account = {'_tenant': '1', 'address': fixture.ACCOUNT, '_sources': ()}
        try:
            case.client.fill_fraction = .5
            result = execute_manual_leader(engine=case.engine, account=account,
                client=case.client, operation=None, event_id='partial-1', action='OPEN',
                leader_margin=10000., leader_capital=100000., allocatable_capital=100.,
                current_margin=0., spec={'leader': fixture.SOURCE_A, 'allocation_pct': 80.,
                    'coin': 'BTC', 'side': 'LONG', 'leverage': 5, 'market_type': 'CRYPTO'})
            self.assertEqual(result.status, 'PARTIAL')
            self.assertIn('BTC|', case.engine.journal.pending(fixture.ACCOUNT))
            calls = len(case.client.calls)
            recovered = recover_pending_manual_leader(case.engine, account, case.client)
            self.assertEqual(case.client.calls.__len__(), calls)
            self.assertEqual(recovered[0]['status'], 'PARTIAL')
            self.assertIn('BTC|', case.engine.journal.pending(fixture.ACCOUNT))
            with patch.object(case.client, 'submit_copy_ioc') as submit:
                duplicate = execute_manual_leader(engine=case.engine, account=account,
                    client=case.client, operation=None, event_id='partial-1', action='OPEN',
                    leader_margin=10000., leader_capital=100000., allocatable_capital=100.,
                    current_margin=0., spec={'leader': fixture.SOURCE_A, 'allocation_pct': 80.,
                        'coin': 'BTC', 'side': 'LONG', 'leverage': 5, 'market_type': 'CRYPTO'})
            self.assertEqual(duplicate['status'], 'DUPLICATE')
            submit.assert_not_called()
        finally:
            case.doCleanups()

    def test_unknown_manual_execution_recovers_without_resubmission(self):
        import test_engine_safety as fixture
        from core.manual_leader_copy import execute_manual_leader, recover_pending_manual_leader
        from unittest.mock import patch
        case = fixture.EngineSafetyTests(methodName='runTest')
        case.setUp()
        account = {'_tenant': '1', 'address': fixture.ACCOUNT, '_sources': ()}
        try:
            original = case.client.submit_copy_ioc
            def acknowledged_lost(*args, **kwargs):
                original(*args, **kwargs)
                raise TimeoutError('acknowledgement lost')
            with patch.object(case.client, 'submit_copy_ioc', side_effect=acknowledged_lost):
                first = execute_manual_leader(engine=case.engine, account=account,
                    client=case.client, operation=None, event_id='unknown-1', action='OPEN',
                    leader_margin=10000., leader_capital=100000., allocatable_capital=100.,
                    current_margin=0., spec={'leader': fixture.SOURCE_A, 'allocation_pct': 80.,
                        'coin': 'BTC', 'side': 'LONG', 'leverage': 5, 'market_type': 'CRYPTO'})
            self.assertEqual(first.status, 'UNKNOWN')
            calls = len(case.client.calls)
            recovered = recover_pending_manual_leader(case.engine, account, case.client)
            self.assertEqual(recovered[0]['status'], 'FILLED')
            self.assertEqual(len(case.client.calls), calls)
            self.assertFalse(case.engine.journal.pending(fixture.ACCOUNT))
        finally:
            case.doCleanups()


class ManualCopyWorkerTests(unittest.TestCase):
    def setUp(self):
        import test_engine_safety as f
        from core.manual_copy_worker import ManualCopyWorker
        from unittest.mock import patch
        self.f=f;self.case=f.EngineSafetyTests(methodName='runTest');self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.case.patch(copy_enabled=False,leaders=[],ai_slot_selected=False)
        self.client=self.case.client;self.client.cash=100.
        self.now=time.time()
        def tick():
            self.now+=.002
            return self.now
        clock=patch('time.time',tick);clock.start();self.addCleanup(clock.stop)
        self.leaders={f.SOURCE_A:{'BTC':(10000.,'LONG',5)},f.SOURCE_B:{'ETH':(10000.,'LONG',5)}}
        self.capital=100000.;self.stale=False;self.reads=[]
        outer=self
        class Reader:
            network='TESTNET'
            def state(self,leader,dex=''):
                outer.reads.append(leader)
                rows=[]
                for coin,(margin,side,lev) in outer.leaders[leader].items():
                    if (coin.split(':')[0] if ':' in coin else '')!=dex:continue
                    size=margin*lev/100
                    rows.append({'position':{'coin':coin,'szi':str(size if side=='LONG' else -size),
                        'leverage':{'value':lev,'type':'cross'},'positionValue':str(margin*lev),'entryPx':'100',
                        'marginUsed':str(margin),'unrealizedPnl':'0','returnOnEquity':'0'}})
                return {'time':int(time.time()*1000)-(60000 if outer.stale else 0),
                    'marginSummary':{'accountValue':outer.capital},'withdrawable':'1','assetPositions':rows}
        self.reader=Reader();self.worker=ManualCopyWorker(self.case.engine,self.reader)
        self.account={'address':f.ACCOUNT,'_tenant':'1'}
        self.worker.service.configure(self.account,self.client,f.SOURCE_A,80.)
        self.worker.service.start(self.account,self.client)
    def cycle(self):return self.worker.cycle(1,self.case.store.profile(1)[1],self.client)
    def assert_action(self,action):
        report=self.cycle()
        self.assertEqual(report['status'],'FOLLOWING',report)
        self.assertEqual(report['results'][-1],{'market':'BTC|','action':action,'status':'FILLED'},report)
        return report
    def test_start_worker_proportional_open_add_reduce_close(self):
        report=self.assert_action('OPEN')
        self.assertEqual(report['denominator'],'PER_DEX_MARGIN_SUMMARY_ACCOUNT_VALUE')
        self.assertAlmostEqual(self.client.rows[('BTC','')]['margin_used'],8.)
        self.leaders[self.f.SOURCE_A]['BTC']=(20000.,'LONG',5);self.assert_action('ADD')
        self.assertAlmostEqual(self.client.rows[('BTC','')]['margin_used'],16.)
        self.leaders[self.f.SOURCE_A]['BTC']=(5000.,'LONG',5);self.assert_action('REDUCE')
        self.leaders[self.f.SOURCE_A]={};self.assert_action('CLOSE')
        self.assertFalse(self.client.rows)
    def test_actual_desktop_watcher_consumes_start_with_legacy_copy_disabled(self):
        import ast
        import asyncio
        from pathlib import Path
        source=Path(__file__).parents[1]/'desktop/main.py'
        node=next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
            if isinstance(n,ast.AsyncFunctionDef) and n.name=='watcher_cycle')
        messages=[]
        env={'asyncio':asyncio,'engine':self.case.engine,'reader':self.reader,'store':self.case.store,
             'account_client':lambda uid,profile:self.client,'print':lambda *args:messages.append(args)}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),env)
        asyncio.run(env['watcher_cycle']())
        self.assertFalse(messages,messages)
        self.assertAlmostEqual(self.client.rows[('BTC','')]['margin_used'],8.)
    def test_same_leader_other_instrument_keeps_consuming_allocation(self):
        self.assert_action('OPEN')
        self.leaders[self.f.SOURCE_A]['ETH']=(100000.,'LONG',5)
        report=self.cycle()
        self.assertEqual(report['results'][-1]['status'],'FILLED',report)
        self.assertLessEqual(sum(p['margin_used'] for p in self.client.rows.values()),80.)
    def test_stop_holds_and_retains_monitoring(self):
        self.assert_action('OPEN');calls=len(self.client.calls)
        self.worker.service.stop(self.account,self.client);self.leaders[self.f.SOURCE_A]={}
        report=self.cycle()
        self.assertEqual(len(self.client.calls),calls);self.assertTrue(self.client.rows)
        self.assertEqual(report['monitored'][0]['mode'],'POSITION_HOLD')
    def test_switch_retains_old_source_and_charges_its_capital(self):
        self.assert_action('OPEN')
        self.worker.service.configure(self.account,self.client,self.f.SOURCE_B,80.)
        self.worker.service.start(self.account,self.client)
        self.leaders[self.f.SOURCE_B]['ETH']=(100000.,'LONG',5)
        report=self.cycle()
        self.assertIn(self.f.SOURCE_A,[r['leader'] for r in report['monitored']])
        self.assertEqual(self.case.engine.journal.owned(self.f.ACCOUNT)['BTC|']['source_targets'][0]['wallet'],self.f.SOURCE_A)
        if ('ETH','') in self.client.rows:self.assertLessEqual(self.client.rows[('ETH','')]['margin_used'],72.)
        self.assertNotIn('OWNERSHIP_TRANSFER',str(report))
    def test_restart_does_not_repeat_current_target(self):
        from core.manual_copy_worker import ManualCopyWorker
        self.assert_action('OPEN');calls=len(self.client.calls)
        self.worker=ManualCopyWorker(self.case.engine,self.reader)
        self.cycle();self.assertEqual(len(self.client.calls),calls)
    def test_stale_or_missing_denominator_does_not_open(self):
        self.stale=True;self.cycle();self.assertFalse(self.client.calls)
        self.stale=False;self.capital=None;report=self.cycle()
        self.assertEqual(report['results'][0]['status'],'DENOMINATOR_UNKNOWN_HOLD');self.assertFalse(self.client.calls)
    def test_missing_denominator_does_not_block_proven_close(self):
        self.assert_action('OPEN');self.capital=None;self.leaders[self.f.SOURCE_A]={};self.assert_action('CLOSE')
    def test_partial_remains_reserved_and_does_not_repeat(self):
        self.client.fill_fraction=.5
        report=self.cycle();self.assertEqual(report['results'][0]['status'],'PARTIAL')
        calls=len(self.client.calls);self.cycle();self.assertEqual(len(self.client.calls),calls)
        self.assertTrue(self.case.engine.journal.pending(self.f.ACCOUNT))
    def test_unknown_recovery_is_query_only(self):
        from unittest.mock import patch
        original=self.client.submit_copy_ioc
        def lost(*args,**kwargs):original(*args,**kwargs);raise TimeoutError('lost ack')
        with patch.object(self.client,'submit_copy_ioc',side_effect=lost):report=self.cycle()
        self.assertEqual(report['results'][0]['status'],'UNKNOWN')
        calls=len(self.client.calls);report=self.cycle()
        self.assertEqual(len(self.client.calls),calls)
        self.assertEqual(report['recovery'][0]['status'],'FILLED')
    def test_restart_after_receipt_before_journal_projection(self):
        from unittest.mock import patch
        with patch.object(self.case.engine.journal,'finish',side_effect=RuntimeError('crash after receipt')):
            self.cycle()
        self.assertTrue(self.client.rows)
        calls=len(self.client.calls)
        report=self.cycle()
        self.assertEqual(report['recovery'][0]['status'],'FILLED',report)
        self.assertEqual(len(self.client.calls),calls)
        self.assertFalse(self.case.engine.journal.pending(self.f.ACCOUNT))
    def test_reverse_rejected_entry_preserves_proven_flat(self):
        self.assert_action('OPEN')
        self.leaders[self.f.SOURCE_A]['BTC']=(20000.,'SHORT',5)
        original=self.client.market_close
        def close(*args,**kwargs):
            response=original(*args,**kwargs)
            self.client.available_margin=lambda dex:0.
            return response
        self.client.market_close=close
        report=self.cycle()
        self.assertEqual(report['results'][0]['status'],'REJECTED',report)
        self.assertFalse(self.client.rows)
        self.assertFalse(self.case.engine.journal.pending(self.f.ACCOUNT))
        self.assertFalse(self.case.engine.journal.owned(self.f.ACCOUNT)['BTC|']['managed'])
    def test_reverse_uses_new_target_after_verified_close(self):
        self.assert_action('OPEN');self.leaders[self.f.SOURCE_A]['BTC']=(20000.,'SHORT',5)
        self.assert_action('REVERSE')
        p=self.client.rows[('BTC','')];self.assertEqual(p['side'],'SHORT');self.assertAlmostEqual(p['margin_used'],16.)
    def test_external_position_is_never_adopted(self):
        self.client.seed(self.f.position())
        report=self.cycle();self.assertFalse(self.client.calls)
        self.assertEqual(report['results'][0]['status'],'UNRELATED_POSITION_HOLD')
