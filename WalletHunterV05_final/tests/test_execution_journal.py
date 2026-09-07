import tempfile
import unittest
from core.execution_journal import ExecutionJournal


class JournalTests(unittest.TestCase):
    def test_prepared_survives_restart_and_prevents_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            log=ExecutionJournal(root)
            log.prepare("a", "BTC|", {"target": 1})
            restarted=ExecutionJournal(root)
            self.assertEqual(restarted.pending("a"), {"BTC|"})
            with self.assertRaises(RuntimeError): restarted.prepare("a", "BTC|", {})
            self.assertEqual(restarted.pending("b"), set())

    def test_confirmed_ownership_and_intent_are_independent_of_chat(self):
        with tempfile.TemporaryDirectory() as root:
            log=ExecutionJournal(root)
            oid=log.prepare("a", "BTC|", {"source_targets": [{"wallet": "leader", "margin": 5}]})
            record={"managed": True, "size": 1, "source_targets": [{"wallet": "leader", "margin": 5}]}
            log.finish(oid, {"ok": True, "action": "OPEN"}, record)
            self.assertEqual(log.pending("a"), set())
            restored = ExecutionJournal(root).owned("a")["BTC|"]
            self.assertGreater(restored.pop("verified_at_ms"), 0)
            self.assertEqual(restored, record)

    def test_unknown_is_not_a_confirmed_order(self):
        with tempfile.TemporaryDirectory() as root:
            log=ExecutionJournal(root)
            oid=log.prepare("a", "ETH|", {})
            log.finish(oid, {"ok": False, "error": "timeout"})
            self.assertEqual(log.pending("a"), {"ETH|"})
            self.assertEqual(log.owned("a"), {})
