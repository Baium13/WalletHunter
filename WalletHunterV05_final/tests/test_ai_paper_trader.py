import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.ai_paper_trader import AiPaperTrader, CANDLE_MS, DAY_MS


BASE = 20000 * DAY_MS + 12 * 3600000 + 1000


def market(now=BASE, price=100, side="LONG", coin="BTC", dex="", digits=5, leverage=40):
    candle = now // CANDLE_MS * CANDLE_MS - 1
    return {"coin": coin, "dex": dex, "price": price, "sz_decimals": digits,
            "max_leverage": leverage, "asof_ms": now, "candle_close_ms": candle,
            "factors": {"trend_ema20_50": {"ema20": 102 if side == "LONG" else 98,
                                             "ema50": 100},
                        "rsi14": 60 if side == "LONG" else 40,
                        "macd_hist": 1 if side == "LONG" else -1,
                        "atr14_pct": 1, "volume_ratio20": 1.2,
                        "levels20": {"support": 90, "resistance": 110},
                        "funding_bps_hour": .1, "open_interest": 500,
                        "asof_ms": now, "candle_close_ms": candle}}


class AiPaperTraderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.trader = AiPaperTrader(self.temp.name)

    def tick(self, now=BASE, price=100, side="LONG", budget=1000, **kwargs):
        return self.trader.tick("139", "AccountA", budget, [market(now, price, side, **kwargs)], now)

    def test_never_started_is_read_only_and_explicit_paper(self):
        result = self.trader.summary("139", "AccountA")
        self.assertEqual(result["mode"], "PAPER")
        self.assertEqual(result["status"], "NOT_STARTED")
        self.assertEqual(result["positions"], [])
        self.assertFalse(result["cost_model"]["funding_included"])

    def test_long_open_sizes_ten_percent_and_caps_leverage(self):
        result = self.tick()
        p = result["positions"][0]
        self.assertEqual(p["side"], "LONG")
        self.assertEqual(p["leverage"], 40)
        self.assertLessEqual(p["margin_usdc"], 100)
        self.assertGreater(p["entry_price"], 100)
        self.assertLess(result["realized_pnl_usdc"], 0)
        self.assertLess(result["equity_usdc"], 1000)
        self.assertEqual(result["counters"]["opened"], 1)

    def test_short_open_uses_market_leverage_ceiling(self):
        result = self.tick(side="SHORT", leverage=5)
        p = result["positions"][0]
        self.assertEqual(p["leverage"], 5)
        self.assertLess(p["entry_price"], 100)
        self.assertEqual(p["side"], "SHORT")

    def test_small_budget_is_skipped_never_forced_to_ten(self):
        result = self.tick(budget=1)
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["events"][0]["reason"], "below_minimum_notional")
        self.assertEqual(result["realized_pnl_usdc"], 0)

    def test_coarse_size_rounds_down_below_minimum(self):
        result = self.tick(budget=10, price=100, digits=0)
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["counters"]["skipped"], 1)

    def test_long_take_profit_close_accounts_both_sides_costs(self):
        opened = self.tick()
        p = opened["positions"][0]
        result = self.tick(BASE + CANDLE_MS, 101)
        event = result["events"][0]
        expected = (101 * .9995 - p["entry_price"]) * p["size"] - p["entry_fees_usdc"] - 101 * .9995 * p["size"] * .0005
        self.assertEqual(result["positions"], [])
        self.assertEqual(event["reason"], "take_profit_roe")
        self.assertAlmostEqual(result["realized_pnl_usdc"], expected)
        self.assertAlmostEqual(event["pnl_usdc"], expected)
        self.assertAlmostEqual(result["equity_usdc"], 1000 + expected)

    def test_short_take_profit(self):
        self.tick(side="SHORT")
        result = self.tick(BASE + CANDLE_MS, 99, "SHORT")
        self.assertEqual(result["positions"], [])
        self.assertGreater(result["realized_pnl_usdc"], 0)
        self.assertEqual(result["events"][0]["reason"], "take_profit_roe")

    def test_stop_roe_closes_before_averaging(self):
        self.tick()
        result = self.tick(BASE + CANDLE_MS, 98.5)
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["events"][0]["reason"], "stop_roe")
        self.assertEqual(result["counters"]["additions"], 0)

    def test_contrary_signal_closes_without_same_candle_reversal(self):
        self.tick()
        result = self.tick(BASE + CANDLE_MS, 100, "SHORT")
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["events"][0]["reason"], "contrary_signal")
        self.assertEqual(result["counters"]["opened"], 1)

    def test_add_updates_weighted_entry_and_never_more_than_four(self):
        result = self.tick()
        previous = result["positions"][0]
        for index in range(1, 6):
            # Keep each fresh observation near -25% ROE of the new average.
            price = previous["entry_price"] * .995
            result = self.tick(BASE + index * CANDLE_MS, price)
            if not result["positions"]:
                self.assertEqual(result["reason"], "slot_loss_limit")
                break
            p = result["positions"][0]
            if index <= 4:
                self.assertEqual(p["adds"], index)
                self.assertLess(p["entry_price"], previous["entry_price"])
                self.assertGreater(p["size"], previous["size"])
            else:
                self.assertEqual(p["adds"], 4)
                self.assertEqual(p["size"], previous["size"])
            previous = p
        self.assertLessEqual(result["counters"]["additions"], 4)
        self.assertLessEqual(result["reserved_margin_usdc"], 500)

    def test_short_averaging_updates_entry_upwards(self):
        result = self.tick(side="SHORT")
        original = result["positions"][0]
        result = self.tick(BASE + CANDLE_MS, original["entry_price"] * 1.005, "SHORT")
        p = result["positions"][0]
        self.assertEqual(p["adds"], 1)
        self.assertGreater(p["entry_price"], original["entry_price"])

    def test_same_candle_updates_mark_but_no_second_action(self):
        first = self.tick()
        second = self.tick(BASE + 1000, 98.8)
        self.assertNotEqual(first["positions"][0]["mark_price"], second["positions"][0]["mark_price"])
        self.assertEqual(first["counters"], second["counters"])
        self.assertEqual(len(first["events"]), len(second["events"]))

    def test_same_tick_is_idempotent_even_from_concurrent_instances(self):
        def run(_):
            return AiPaperTrader(self.temp.name).tick("139", "accounta", 1000, [market()], BASE)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(8)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(results[0]["counters"]["opened"], 1)

    def test_restart_preserves_every_field_and_account_scope(self):
        first = self.tick()
        restarted = AiPaperTrader(self.temp.name)
        self.assertEqual(restarted.summary("139", "ACCOUNTa"), first)
        self.assertEqual(restarted.summary("other", "accounta")["positions"], [])
        self.assertEqual(restarted.summary("139", "other-account")["positions"], [])

    def test_withdrawal_and_deposit_are_not_fake_realized_profit(self):
        result = self.tick()
        pnl = result["realized_pnl_usdc"]
        reduced = self.tick(BASE + 1000, budget=500)
        self.assertEqual(reduced["realized_pnl_usdc"], pnl)
        self.assertEqual(reduced["external_funding_adjustment_usdc"], -500)
        self.assertAlmostEqual(result["equity_usdc"] - reduced["equity_usdc"], 500)
        restored = self.tick(BASE + 2000, budget=2000)
        self.assertEqual(restored["funded_base_usdc"], 1000)
        self.assertEqual(restored["realized_pnl_usdc"], pnl)

    def test_zero_budget_then_funding_and_budget_cap_pause(self):
        first = self.tick(budget=0)
        self.assertEqual(first["status"], "WAITING_BUDGET")
        result = self.tick(BASE + CANDLE_MS)
        self.assertEqual(len(result["positions"]), 1)
        result = self.tick(BASE + CANDLE_MS + 1000, budget=1)
        self.assertIn(result["status"], {"BUDGET_PAUSED", "LOSS_LIMIT"})
        self.assertEqual(result["counters"]["additions"], 0)

    def test_slot_loss_limit_closes_and_persists_across_restart(self):
        self.tick()
        result = self.tick(BASE + CANDLE_MS, .1)
        self.assertEqual(result["status"], "LOSS_LIMIT")
        self.assertEqual(result["reason"], "slot_loss_limit")
        self.assertEqual(result["positions"], [])
        self.trader = AiPaperTrader(self.temp.name)
        result = self.tick(BASE + 2 * CANDLE_MS)
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["reason"], "slot_loss_limit")

    def test_daily_loss_limit_separate_from_lifetime_profit(self):
        self.tick()
        self.tick(BASE + CANDLE_MS, 300)
        next_day = BASE + DAY_MS
        self.tick(next_day)
        result = self.tick(next_day + CANDLE_MS, .1)
        self.assertGreater(result["realized_pnl_usdc"], 0)
        self.assertEqual(result["reason"], "daily_loss_limit")
        result = self.tick(next_day + DAY_MS)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(len(result["positions"]), 1)

    def test_max_three_positions_and_stock_identity(self):
        rows = [market(coin=coin) for coin in ("BTC", "ETH", "SOL")]
        rows.append(market(coin="INTC", dex="xyz"))
        result = self.trader.tick("139", "accounta", 1000, rows, BASE)
        self.assertEqual(len(result["positions"]), 3)
        self.assertEqual(result["counters"]["skipped"], 1)
        other = self.trader.tick("139", "other", 1000, [rows[-1]], BASE)
        self.assertEqual(other["positions"][0]["coin"], "xyz:INTC")

    def test_missing_data_is_waiting_not_fabricated_closure(self):
        self.tick()
        result = self.trader.tick("139", "accounta", 1000, [], BASE + CANDLE_MS)
        self.assertEqual(result["status"], "WAITING_DATA")
        self.assertEqual(len(result["positions"]), 1)
        self.assertEqual(result["counters"]["closed"], 0)

    def test_partial_quotes_block_all_new_risk_but_allow_verified_exit(self):
        self.tick(coin="ETH")
        next_time = BASE + CANDLE_MS
        result = self.trader.tick("139", "accounta", 1000, [market(next_time, coin="BTC")], next_time)
        self.assertEqual(result["status"], "WAITING_DATA")
        self.assertEqual([p["coin"] for p in result["positions"]], ["ETH"])
        result = self.trader.tick("139", "accounta", 1000,
                                 [market(next_time + CANDLE_MS, 101, coin="ETH")], next_time + CANDLE_MS)
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["counters"]["closed"], 1)

    def test_invalid_and_stale_market_data_leave_database_unchanged(self):
        original = self.tick()
        now = BASE + CANDLE_MS
        invalid = []
        for key, value in [("price", float("nan")), ("price", 0), ("price", float("inf")),
                           ("sz_decimals", 7), ("max_leverage", 0), ("asof_ms", now - 120001),
                           ("asof_ms", now + 1), ("candle_close_ms", now), ("dex", "cash")]:
            row = market(now); row[key] = value; invalid.append(row)
        row = market(now); row["factors"]["volume_ratio20"] = None; invalid.append(row)
        row = market(now); row["factors"]["rsi14"] = 101; invalid.append(row)
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.trader.tick("139", "accounta", 1000, [row], now)
            self.assertEqual(self.trader.summary("139", "accounta"), original)

    def test_invalid_budget_duplicate_market_and_out_of_order_rejected(self):
        self.tick()
        for budget in [-1, float("nan"), float("inf"), True, None]:
            with self.assertRaises(ValueError):
                self.trader.tick("139", "accounta", budget, [market()], BASE)
        with self.assertRaises(ValueError):
            self.trader.tick("139", "accounta", 1000, [market(), market()], BASE)
        with self.assertRaises(ValueError):
            self.trader.tick("139", "accounta", 1000, [market(BASE - CANDLE_MS)], BASE - CANDLE_MS)

    def test_neutral_signal_never_opens_and_events_stay_bounded(self):
        row = market(); row["factors"]["macd_hist"] = 0
        self.assertEqual(self.trader.tick("139", "accounta", 1, [row], BASE)["positions"], [])
        for i in range(1, 90):
            self.tick(BASE + i * CANDLE_MS, budget=1)
        result = self.trader.summary("139", "accounta")
        self.assertEqual(len(result["events"]), 80)
        self.assertEqual(result["counters"]["skipped"], 89)
        json.dumps(result, allow_nan=False)

    def test_module_has_no_exchange_or_network_dependency(self):
        source = Path(__file__).resolve().parents[1].joinpath("core", "ai_paper_trader.py").read_text(encoding="utf-8")
        for forbidden in ("from hyperliquid", "import requests", "market_open(", "market_close(", "private_key"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
