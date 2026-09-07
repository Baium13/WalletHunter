import ast
import asyncio
import html
import json
import math
from pathlib import Path
import tempfile
import time
from datetime import datetime
from types import SimpleNamespace
import unittest

from cryptography.fernet import Fernet
from core.analysis_metrics import persisted_profit_factor
from core.follower_simulator import FollowerSimulator
from core.storage import Storage
from core.profile_mutation import save_profile_guarded


class AnalysisPersistenceTests(unittest.TestCase):
    def test_ratio_encoding_preserves_finite_and_infinite_semantics(self):
        for value in (0, 1, 2.75):
            self.assertEqual(persisted_profit_factor(value), value)
        self.assertEqual(persisted_profit_factor(math.inf), "Infinity")
        json.dumps({"pf": persisted_profit_factor(math.inf)}, allow_nan=False)

    def test_invalid_ratios_are_rejected_not_coerced_to_zero(self):
        for value in (math.nan, -math.inf, -1, True, "Infinity", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                persisted_profit_factor(value)

    def test_actual_telegram_analysis_persists_unbounded_pfs_and_survives_reload(self):
        # Importing the bot would create a live Telegram client. Compile only the
        # real report function, retaining its production persistence call path.
        source = Path(__file__).parents[1] / "desktop" / "main.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "analyse")
        fills = [{"closedPnl": "10", "sz": "1", "px": "100", "time": 1_700_000_000_000 + i * 86400000}
                 for i in range(40)]
        report = SimpleNamespace(profit_factor=math.inf, rating=80, recommendation="Историческая оценка",
                                 trades=40, win_rate=100., net_pnl=400., expectancy=10., max_drawdown=0.,
                                 positive_days=40, active_days=40, top_coin_share=100.)
        messages = []

        class Event:
            async def edit(self, text, **kwargs):
                messages.append(text)

        with tempfile.TemporaryDirectory() as root:
            master_key = Fernet.generate_key()
            storage = Storage(root, master_key)
            _, profile = storage.profile(1)
            profile["runtime"]["managed"] = ["BTC|"]
            profile["leaders"] = ["configured", "unanalysed"]
            profile["copy_enabled"] = False
            profile["leader_enabled"] = {"configured": False}
            profile["leader_models"] = {"configured": {"eligible": False}}
            storage.update_profile(1, profile)
            _, profile = storage.profile(1)

            async def save_profile(uid, p):
                save_profile_guarded(storage, uid, p)

            scope = {"asyncio": asyncio, "time": time, "esc": html.escape,
                     "reader": SimpleNamespace(fills_90d=lambda _: fills, balance=lambda _: 1000., positions=lambda *args: []),
                     "analyzer": SimpleNamespace(report=lambda *args: report), "simulator": FollowerSimulator(),
                     "persisted_profit_factor": persisted_profit_factor, "save_profile": save_profile,
                     "Button": SimpleNamespace(inline=lambda *args: None)}
            exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), scope)
            asyncio.run(scope["analyse"](Event(), "wallet", 1, profile))
            saved = Storage(root, master_key).profile(1)[1]
            self.assertEqual(saved["wallet_research"]["wallet"]["train_pf"], "Infinity")
            self.assertEqual(saved["wallet_research"]["wallet"]["test_pf"], "Infinity")
            self.assertEqual(saved["leader_models"], {"configured": {"eligible": False}})
            self.assertFalse(saved["copy_enabled"])
            self.assertEqual(saved["leaders"], ["configured", "unanalysed"])
            self.assertEqual(saved["leader_enabled"], {"configured": False})
            self.assertEqual(saved["runtime"]["managed"], ["BTC|"])
            json.dumps(saved, allow_nan=False)
            self.assertIn("СТРЕСС-ТЕСТ ИЗДЕРЖЕК", messages[-1])
            self.assertNotIn("Допущен", messages[-1])
            self.assertNotIn("Ошибка анализа", messages[-1])
            # Neither a configured nor an unrelated research target changes
            # the plan, including when some configured sources lack research.
            from tests.test_engine_safety import EngineSafetyTests, position, snapshot
            from core.trading_engine import CopyEngine
            settings = SimpleNamespace(max_leverage=40, max_position_pct=100., max_total_exposure_usd=100000.)
            engine = CopyEngine(None, storage, settings)
            snapshots = [snapshot(w, [position()]) for w in ("configured", "unanalysed")]
            for policy in ({}, {"configured": {"eligible": False}}):
                saved.update(copy_enabled=True, leader_models=policy)
                storage.update_profile(1, saved)
                expected = engine._plan(snapshots, 300., saved)
                for wallet in ("configured", "unrelated"):
                    current = storage.profile(1)[1]
                    asyncio.run(scope["analyse"](Event(), wallet, 1, current))
                    saved = Storage(root, master_key).profile(1)[1]
                    self.assertTrue(saved["copy_enabled"])
                    self.assertEqual(saved["leader_models"], policy)
                    self.assertEqual(engine._plan(snapshots, 300., saved), expected)

    def test_legacy_dashboard_always_shows_fixed_thirds_and_selected_ai(self):
        source = Path(__file__).parents[1] / "desktop" / "main.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "dashboard")
        scope = {"engine": SimpleNamespace(risk=lambda _: {"label": "Риск"}, strategy=lambda _: {"label": "Стратегия"}),
                 "leader_is_enabled": lambda p, w: (p.get("leader_enabled") or {}).get(w, True),
                 "S": SimpleNamespace(auto_trading=False, max_leverage=20), "datetime": datetime}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), scope)
        for count in (0, 1, 2, 3):
            for selected_ai in (False, True):
                if selected_ai and count == 3:
                    continue
                profile = {"leaders": [f"wallet{i}" for i in range(count)], "ai_slot_selected": selected_ai}
                text = scope["dashboard"](1, profile, 90.)
                self.assertIn("<b>1/3</b> на каждый слот", text)
                self.assertEqual("🧠 3 · ИИ исследование" in text, selected_ai)


if __name__ == "__main__":
    unittest.main()
