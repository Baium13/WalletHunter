import unittest
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
