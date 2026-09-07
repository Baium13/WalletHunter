"""Public-only learning integration; no app credentials or network imports.

The desktop watcher is isolated from its module so startup cannot connect to
Telegram or instantiate a signing client. Collector tests use injected fakes.
"""
import ast
import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from core.ai_learning_worker import AiLearningWorker, BAR_MS
from core.ai_review import account_guard


ROOT = Path(__file__).resolve().parents[1]
NOW = 2_000_000 * BAR_MS + 1000


def function_node(relative, name):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8-sig"))
    return next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


class FakeLearner:
    def __init__(self):
        self.ingested = []
        self.trained = []
        self.failed_training = False

    def ingest(self, coin, rows, now_ms):
        self.ingested.append((coin, copy.deepcopy(rows), now_ms))

    def train(self, now_ms):
        if self.failed_training: raise ValueError("private-token-like-text-must-not-leak")
        self.trained.append(now_ms)

    def summary(self, now_ms=None):
        return {"status": "COLLECTING", "counts": {"candles": len(self.ingested)}, "model": None}


class PublicOnlyReader:
    def __init__(self):
        self.calls = []
        self.empty = False

    def __getattr__(self, name):
        raise AssertionError(f"Collector attempted forbidden access: {name}")

    def _info(self, payload):
        self.calls.append(copy.deepcopy(payload))
        if set(payload) != {"type", "req"} or payload["type"] != "candleSnapshot":
            raise AssertionError("Only public candles are allowed")
        req = payload["req"]
        if set(req) != {"coin", "interval", "startTime", "endTime"}:
            raise AssertionError("No user, account or private parameters allowed")
        if req["coin"] not in {"BTC", "ETH"} or req["interval"] != "15m":
            raise AssertionError("Unexpected universe")
        if self.empty: return []
        boundary = req["endTime"] // BAR_MS * BAR_MS
        return [{"t": boundary-(65-i)*BAR_MS, "T": boundary-(64-i)*BAR_MS-1,
                 "o": "100", "h": "102", "l": "99", "c": "101", "v": "10"} for i in range(65)]


class PublicLearningIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.learner, self.reader = FakeLearner(), PublicOnlyReader()
        self.worker = AiLearningWorker(self.tmp.name, self.learner)

    def test_actual_global_watcher_uses_dedicated_public_reader_without_profiles_or_keys(self):
        node = function_node("desktop/main.py", "ai_learning_watcher")
        forbidden = {"account_client", "public_account_reader", "store", "storage", "client", "engine"}
        self.assertFalse(forbidden & {n.id for n in ast.walk(node) if isinstance(n, ast.Name)})
        calls = []
        class StopLoop(Exception): pass
        public_reader = object()
        def make_reader(mode):
            self.assertEqual(mode, "MAINNET")
            return public_reader
        def cycle(reader): calls.append(reader)
        async def to_thread(call, *args): return call(*args)
        async def sleep(delay):
            self.assertEqual(delay, 60)
            raise StopLoop
        scope = {"S": SimpleNamespace(hl_mode="MAINNET"), "HyperliquidReader": make_reader,
                 "ai_learning": SimpleNamespace(cycle=cycle),
                 "asyncio": SimpleNamespace(to_thread=to_thread, sleep=sleep)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "isolated-public-watcher", "exec"), scope)
        with self.assertRaises(StopLoop): asyncio.run(scope["ai_learning_watcher"]())
        self.assertEqual(calls, [public_reader])

    def test_global_learning_export_follows_auth_and_receives_no_user_or_account(self):
        node = function_node("webapp/server.py", "ai_summary")
        first = node.body[0]
        self.assertIsInstance(first, ast.Assign)
        self.assertIsInstance(first.value, ast.Call)
        self.assertIsInstance(first.value.func, ast.Name)
        self.assertEqual(first.value.func.id, "require_user")
        exports = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                   and n.func.value.id == "ai_learning"]
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0].func.attr, "summary")
        self.assertEqual(exports[0].args, [])
        self.assertEqual(exports[0].keywords, [])

    def test_collector_scope_is_only_two_public_candle_streams(self):
        result = self.worker.cycle(self.reader, NOW)
        self.assertEqual(result["collector"]["status"], "OK")
        self.assertFalse(result["real_execution_available"])
        self.assertEqual([p["req"]["coin"] for p in self.reader.calls], ["BTC", "ETH"])
        self.assertEqual(len(self.learner.ingested), 2)
        self.assertEqual(self.learner.trained, [NOW])

    def test_training_failure_survives_restart_without_raw_errors_or_false_success(self):
        self.worker.cycle(self.reader, NOW)
        self.learner.failed_training = True
        result = self.worker.cycle(self.reader, NOW+BAR_MS)
        self.assertEqual(result["collector"]["status"], "UNAVAILABLE")
        self.assertEqual(result["collector"]["last_success_ms"], NOW)
        self.assertEqual(result["collector"]["errors"], [{"reason": "training_failed"}])
        restarted = AiLearningWorker(self.tmp.name, self.learner)
        self.assertEqual(restarted.summary(NOW+BAR_MS)["collector"], result["collector"])
        self.assertNotIn("private-token", str(result))
        self.assertEqual(self.learner.trained, [NOW])

    def test_empty_history_is_failure_not_ingested_zero_data(self):
        self.reader.empty = True
        result = self.worker.cycle(self.reader, NOW)
        self.assertEqual(result["collector"]["status"], "UNAVAILABLE")
        self.assertEqual(result["collector"]["watermarks"], {})
        self.assertIsNone(result["collector"]["last_success_ms"])
        self.assertEqual(self.learner.ingested, [])
        self.assertEqual(self.learner.trained, [])

    def test_busy_second_worker_cannot_overwrite_successful_shared_status(self):
        self.worker.cycle(self.reader, NOW)
        before = self.worker._read_status()
        other = AiLearningWorker(self.tmp.name, self.learner)
        with account_guard(self.tmp.name, "ai-learning-public-collector"):
            result = other.cycle(self.reader, NOW+BAR_MS)
        self.assertEqual(result["collector"]["status"], "BUSY")
        self.assertEqual(self.worker._read_status(), before)
        self.assertEqual(len(self.reader.calls), 2)

    def test_interrupted_fetch_is_retried_without_claiming_previous_success(self):
        initial = self.worker._read_status()
        initial.update(status="FETCHING", last_attempt_ms=NOW)
        self.worker._save_status(initial)
        restarted = AiLearningWorker(self.tmp.name, self.learner)
        self.assertEqual(restarted.summary(NOW)["collector"]["status"], "FETCHING")
        result = restarted.cycle(self.reader, NOW+1000)
        self.assertEqual(result["collector"]["status"], "OK")
        self.assertEqual(result["collector"]["last_success_ms"], NOW+1000)
        self.assertEqual(self.learner.trained, [NOW+1000])

    def test_real_learner_bootstrap_through_public_collector_remains_offline_and_shared(self):
        from core.ai_learning import AiLearning
        class HistoricalPublicReader(PublicOnlyReader):
            def _info(self, payload):
                super()._info(payload)  # Enforce no account/private parameters.
                boundary = payload["req"]["endTime"] // BAR_MS * BAR_MS
                rows = []
                for i in range(800):
                    price = 100 + i*.003 + (i % 17)*.02
                    rows.append({"t": boundary-(800-i)*BAR_MS, "T": boundary-(799-i)*BAR_MS-1,
                        "o": price, "h": price+.5, "l": price-.5, "c": price+.1, "v": 100+i%7})
                return rows
        reader = HistoricalPublicReader()
        worker = AiLearningWorker(self.tmp.name, AiLearning(self.tmp.name))
        result = worker.cycle(reader, NOW)
        self.assertEqual(result["collector"]["status"], "OK")
        self.assertEqual(result["status"], "RESEARCH_MODEL")
        self.assertEqual(result["counts"]["candles"], 1600)
        self.assertEqual(result["model"]["version"], 1)
        self.assertEqual(result["validation"]["kind"], "RETROSPECTIVE_BOOTSTRAP")
        self.assertFalse(result["real_execution_available"])
        self.assertTrue(all(p["created_ms"] < p["entry_ms"] for p in result["predictions"]))
        reopened = AiLearningWorker(self.tmp.name, AiLearning(self.tmp.name))
        self.assertEqual(reopened.cycle(reader, NOW+1000), result)
        self.assertEqual(len(reader.calls), 2, "One global model must not refetch per account or process")


if __name__ == "__main__": unittest.main()
