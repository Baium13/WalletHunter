"""Offline safety checks for the copy engine.

These tests use fake clients only: no private key, network request, or order is
ever sent.  They guard the reconciliation paths used by crypto and XYZ markets.
"""
import asyncio
import unittest
from types import SimpleNamespace

from core.trading_engine import CopyEngine


def settings(auto_trading=False, max_leverage=40, max_exposure=100_000):
    return SimpleNamespace(
        auto_trading=auto_trading,
        max_leverage=max_leverage,
        max_position_pct=100.0,
        max_total_exposure_usd=max_exposure,
        max_slippage_pct=0.5,
        entry_price_tolerance_pct=100.0,
    )


class PaperClient:
    exchange = None

    def mid(self, coin, dex=""):
        return 100.0


class LiveBoundaryClient(PaperClient):
    exchange = object()

    def size_step(self, coin, dex=""):
        return 0.01

    def round_size(self, coin, size, dex=""):
        return round(float(size), 2)


def spec(notional, side="LONG", market_type="CRYPTO"):
    return {
        "target_notional": float(notional), "side": side, "leverage": 5.0,
        "market_type": market_type, "entry_price": 100.0,
        "capital_pct": 1.0, "target_margin": float(notional) / 5,
    }


class VirtualCopyEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = CopyEngine(None, None, settings())
        self.account = {"id": "offline-test", "name": "Offline"}
        self.profile = {"risk_mode": "standard", "strategy_mode": "swing"}

    def reconcile(self, key, target, existing=None, managed=False):
        return asyncio.run(self.engine._reconcile(
            self.account, PaperClient(), key, target, existing, managed, self.profile
        ))

    def test_crypto_and_xyz_open_add_reduce_reverse_close(self):
        for key, market_type in (("BTC", "CRYPTO"), ("NVDA", "STOCKS")):
            dex = "xyz" if market_type == "STOCKS" else ""
            market_key = (key, dex)
            opened = self.reconcile(market_key, spec(100, "LONG", market_type))
            self.assertEqual(opened.action, "OPEN")
            added = self.reconcile(market_key, spec(200, "LONG", market_type))
            self.assertEqual(added.action, "ADD")
            reduced = self.reconcile(market_key, spec(50, "LONG", market_type))
            self.assertEqual(reduced.action, "PARTIAL_CLOSE")
            reversed_position = self.reconcile(market_key, spec(100, "SHORT", market_type))
            self.assertEqual(reversed_position.action, "REVERSE")
            closed = asyncio.run(self.engine._close(self.account, PaperClient(), {
                "coin": key, "dex": dex, "side": "SHORT", "market_type": market_type,
            }))
            self.assertTrue(closed.ok)
            self.assertEqual(closed.action, "FULL_CLOSE")

    def test_leverage_cap_and_ai_third_reservation(self):
        engine = CopyEngine(None, None, settings(max_leverage=20))
        notional, leverage, _, _ = engine.desired(
            {"leverage": 100, "position_value": 1_000}, 1_000, 100, {}
        )
        self.assertEqual(leverage, 20)
        self.assertGreater(notional, 0)
        snapshots = [{"wallet": "a", "enabled": True, "balance": 1_000, "positions": [
            {"coin": "BTC", "dex": "", "market_type": "CRYPTO", "side": "LONG",
             "leverage": 10, "position_value": 1_000, "entry_price": 100}
        ]}]
        two_slot_notional, _, _, _ = engine.desired(snapshots[0]["positions"][0], 1_000, 150, {})
        ai_slot_notional, _, _, _ = engine.desired(snapshots[0]["positions"][0], 1_000, 100, {})
        self.assertAlmostEqual(two_slot_notional, 150.0)
        self.assertAlmostEqual(ai_slot_notional, 100.0)
        two_slots = engine._plan(snapshots, 300, {"risk_mode": "standard"})
        ai_slot = engine._plan(snapshots, 300, {"risk_mode": "standard", "ai_slot_selected": True})
        # Fixed thirds: a free source slot must not donate its budget.
        self.assertAlmostEqual(two_slots[("BTC", "")]["target_notional"], 100.0)
        self.assertAlmostEqual(ai_slot[("BTC", "")]["target_notional"], 100.0)

    def test_live_dust_adjustment_is_silent_but_new_dust_entry_is_reported(self):
        engine = CopyEngine(None, None, settings(auto_trading=True))
        existing = {"coin": "BTC", "dex": None, "side": "LONG", "size": 1.0, "leverage": 5}
        result = asyncio.run(engine._reconcile(
            self.account, LiveBoundaryClient(), ("BTC", ""), spec(105), existing, True, self.profile
        ))
        self.assertEqual(result.action, "NO_CHANGE")
        new_entry = asyncio.run(engine._reconcile(
            self.account, LiveBoundaryClient(), ("ETH", ""), spec(5), None, True, self.profile
        ))
        self.assertEqual(new_entry.action, "MIN_NOTIONAL_SKIP")
        self.assertAlmostEqual(new_entry.target_notional, 5.0)
        self.assertIn("объём нашей позиции меньше $10", new_entry.error)


if __name__ == "__main__":
    unittest.main()
