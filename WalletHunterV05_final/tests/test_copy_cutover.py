"""Actual sync_profile -> canonical gateway -> mocked existing HL boundary."""
import ast
import json
from pathlib import Path
from contextlib import closing
from unittest import TestCase
from unittest.mock import patch
import test_engine_safety as fixture
from test_engine_safety import position, snapshot, ACCOUNT
from core.foundation.execution import ExecutionGateway
from core.foundation.risk import RiskGateway
from core.foundation.copy_execution import HyperliquidExecutionAdapter


class CopyCutoverTests(TestCase):
    setUp = fixture.EngineSafetyTests.setUp
    managed = fixture.EngineSafetyTests.managed
    runtime = fixture.EngineSafetyTests.runtime
    run_cycle = fixture.EngineSafetyTests.run_cycle
    patch = fixture.EngineSafetyTests.patch

    def run_traced(self, rows=None):
        seen = {'gateway': [], 'risk': [], 'adapter': []}
        original_execute, original_risk, original_submit = ExecutionGateway.execute, RiskGateway.evaluate, HyperliquidExecutionAdapter.submit
        def execute(gateway, intent, market, **kwargs):
            seen['gateway'].append(intent.action)
            return original_execute(gateway, intent, market, **kwargs)
        def risk(gateway, intent, *args, **kwargs):
            seen['risk'].append(intent.action)
            return original_risk(gateway, intent, *args, **kwargs)
        def submit(adapter, intent, before, now):
            seen['adapter'].append(intent.action)
            with closing(self.engine.journal.connect()) as db:
                row = db.execute('SELECT * FROM intents WHERE id=?', (intent.intent_id,)).fetchone()
                parent = db.execute('SELECT intent FROM operations WHERE id=?', (intent.parent_intent_id,)).fetchone()
                self.assertEqual(row['status'], 'SUBMITTING')
                self.assertIn(intent.intent_id, json.loads(parent[0])['canonical_intents'])
                self.assertEqual(json.loads(row['reservation'])['intent_id'], intent.intent_id)
            return original_submit(adapter, intent, before, now)
        with patch.object(ExecutionGateway, 'execute', execute), patch.object(RiskGateway, 'evaluate', risk), patch.object(HyperliquidExecutionAdapter, 'submit', submit):
            result = self.run_cycle(rows)
        self.assertEqual(seen['gateway'], seen['risk'])
        self.assertEqual(seen['risk'], seen['adapter'])
        return seen, result

    def test_open(self):
        seen, result = self.run_traced()
        self.assertEqual(seen['adapter'], ['OPEN'])
        self.assertTrue(result[0].ok)

    def test_add(self):
        self.managed(position(notional=30))
        seen, result = self.run_traced()
        self.assertEqual(seen['adapter'], ['ADD'])
        self.assertTrue(result[0].ok)

    def test_reduce(self):
        self.managed(position())
        seen, result = self.run_traced([snapshot(positions=[position(notional=400)])])
        self.assertEqual(seen['adapter'], ['REDUCE'])
        self.assertTrue(result[0].ok)

    def test_close(self):
        self.managed(position())
        seen, result = self.run_traced([snapshot()])
        self.assertEqual(seen['adapter'], ['CLOSE'])
        self.assertTrue(result[0].ok)

    def test_leverage(self):
        self.managed(position(leverage=5))
        seen, result = self.run_traced()
        self.assertEqual(seen['adapter'], ['LEVERAGE_UPDATE'])
        self.assertTrue(result[0].ok)

    def test_reverse(self):
        self.managed(position(side='SHORT'))
        seen, result = self.run_traced()
        self.assertEqual(seen['adapter'], ['CLOSE','OPEN'])
        self.assertTrue(result[0].ok)
        with closing(self.engine.journal.connect()) as db:
            bodies = [json.loads(r[0]) for r in db.execute('SELECT body FROM intents')]
        self.assertEqual(len({b['parent_intent_id'] for b in bodies}), 1)
        self.assertEqual(len({b['intent_id'] for b in bodies}), 2)

    def test_risk_rejection_has_no_adapter_and_releases_parent(self):
        original = RiskGateway.evaluate
        def reject(gateway, *args, **kwargs):
            return original(gateway, *args, **dict(kwargs, authorized=False))
        with patch.object(RiskGateway, 'evaluate', reject), patch.object(HyperliquidExecutionAdapter, 'submit') as submit:
            self.run_cycle()
            submit.assert_not_called()
        self.assertFalse(self.engine.journal.pending(ACCOUNT))

    def test_network_rejection_no_adapter(self):
        self.client.network = 'invalid'
        with patch.object(HyperliquidExecutionAdapter, 'submit') as submit:
            with self.assertRaises(ValueError): self.run_cycle()
            submit.assert_not_called()

    def test_unknown_never_retries(self):
        with patch.object(self.client, 'submit_copy_ioc', side_effect=TimeoutError()) as submit:
            self.run_cycle()
            self.run_cycle()
            self.assertEqual(submit.call_count, 1)
        self.assertIn('BTC|', self.engine.journal.pending(ACCOUNT))

    def test_reverse_unknown_close_forbids_open(self):
        self.managed(position(side='SHORT'))
        with patch.object(self.client, 'submit_copy_ioc', side_effect=TimeoutError()) as submit:
            self.run_cycle()
            self.assertEqual(submit.call_count, 1)
            self.assertTrue(submit.call_args.args[4])

    def test_partial_proof_is_not_full_fill(self):
        self.client.fill_fraction = .5
        self.run_cycle()
        with closing(self.engine.journal.connect()) as db:
            row = db.execute('SELECT receipt FROM intents').fetchone()
        receipt = json.loads(row[0])
        self.assertEqual(receipt['status'], 'PARTIAL')
        self.assertEqual(self.client.rows[('BTC','')]['size'], .5)

    def test_normal_writer_guard(self):
        import core.trading_engine as engine
        tree = ast.parse(Path(engine.__file__).read_text(encoding='utf-8-sig'))
        forbidden = {'market_open', 'market_reduce', 'market_close', 'set_leverage'}
        self.assertFalse([n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in forbidden])
        cancellations = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
            and any(isinstance(x, ast.Attribute) and x.attr == 'cancel_open_orders' for x in ast.walk(n))]
        self.assertEqual([n.name for n in cancellations], ['emergency_stop'])
