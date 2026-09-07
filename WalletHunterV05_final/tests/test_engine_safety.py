"""Offline end-to-end safety contracts for sync_profile.

The fake client is the exchange boundary. No credentials, network calls or live
orders are used; durable state and execution journals live in a temporary dir.
"""
import asyncio
from copy import deepcopy
import tempfile
import unittest
from types import SimpleNamespace

from cryptography.fernet import Fernet
from core.execution_journal import ExecutionJournal
from core.storage import Storage
from core.trading_engine import CopyEngine


SOURCE_A = "0x" + "a" * 40
SOURCE_B = "0x" + "b" * 40
ACCOUNT = "0x" + "c" * 40


def position(coin="BTC", notional=100, side="LONG", leverage=1, dex="", roe=0, price=100.):
    return {
        "coin": coin, "dex": dex, "side": side, "size": float(notional) / price,
        "position_value": float(notional), "entry_price": price, "mark_price": price,
        "leverage": float(leverage), "market_max_leverage": 40.,
        "margin_used": float(notional) / leverage, "roe": roe,
        "market_type": "STOCKS" if dex or ":" in coin else "CRYPTO",
    }


def snapshot(wallet=SOURCE_A, positions=None):
    return {"wallet": wallet, "enabled": True, "balance": 1000., "positions": positions or []}


class Reader:
    network = "TESTNET"
    def market_context(self, coin, dex=""):
        return {"funding_bps_hour": 0., "open_interest": 1000.}
    def _info(self, payload):
        if payload.get("type") != "userFillsByTime":
            raise AssertionError("Unexpected network operation in offline engine fixture")
        return []


class ExchangeBoundary:
    exchange = object()
    network = "TESTNET"

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.leverages = {}
        self.prices = {}
        self.cash = 300.
        self.pnl = 0.
        self.fail_positions = False
        self.fail_after_submit = False
        self.partial_close = False

    def seed(self, row):
        self.rows[CopyEngine._key(row["coin"], row.get("dex"))] = deepcopy(row)

    def balance(self): return self.cash
    def available_margin(self, dex=""): return 1_000_000.
    def frontend_open_orders(self, dex=""): return []
    def realized_pnl_since(self, since): return self.pnl
    def mid(self, coin, dex=""): return self.prices.get(CopyEngine._key(coin, dex), 100.)
    def size_step(self, coin, dex=""): return .000001
    def round_size(self, coin, size, dex=""): return int(float(size) * 1_000_000 + 1e-7) / 1_000_000
    def spread_bps(self, coin, dex=""): return 1.
    def response_error(self, response): return response.get("error", "")
    def order_error(self, response): return response.get("error", "")

    def positions(self, *args):
        if self.fail_positions or (self.fail_after_submit and any(call[0] in {"open", "reduce", "close"} for call in self.calls)):
            self.fail_after_submit = False
            raise RuntimeError("Synthetic exchange positions unavailable")
        return deepcopy(list(self.rows.values()))

    def set_leverage(self, coin, leverage, dex=""):
        key = CopyEngine._key(coin, dex)
        self.calls.append(("leverage", key, leverage))
        self.leverages[key] = leverage
        if key in self.rows: self.rows[key]["leverage"] = leverage
        return {"ok": True}

    def _trade(self, action, coin, buy, size, dex):
        key = CopyEngine._key(coin, dex)
        self.calls.append((action, key, float(size)))
        before = self.rows.get(key)
        previous = (before["size"] * (1 if before["side"] == "LONG" else -1)) if before else 0
        signed = previous
        signed += float(size) * (1 if buy else -1)
        if action == "reduce" and (previous == 0 or signed * previous < 0):
            signed = 0  # Exchange reduce-only orders cannot reverse a position.
        if abs(signed) < 1e-9:
            self.rows.pop(key, None)
        else:
            price = self.mid(coin, dex)
            self.rows[key] = position(key[0], abs(signed) * price, "LONG" if signed > 0 else "SHORT", self.leverages.get(key, 1), key[1], price=price)
        return {"ok": True}

    def market_open(self, coin, buy, size, dex, leverage, slippage):
        self.leverages[CopyEngine._key(coin, dex)] = leverage
        return self._trade("open", coin, buy, size, dex)

    def market_reduce(self, coin, buy, size, dex, slippage):
        return self._trade("reduce", coin, buy, size, dex)

    def market_close(self, coin, dex="", slippage_pct=0.5):
        key = CopyEngine._key(coin, dex)
        self.calls.append(("close", key))
        if self.partial_close and key in self.rows:
            self.rows[key]["size"] *= .5
            self.rows[key]["position_value"] *= .5
        else:
            self.rows.pop(key, None)
        return {"ok": True}


class EngineSafetyTests(unittest.TestCase):
    def test_emergency_does_not_close_network_ambiguous_managed_position(self):
        self.client.network = self.engine.reader.network = "MAINNET"
        self.run_cycle()
        self.client.calls.clear()
        self.client.cancel_open_orders = lambda: []
        self.client.network = "TESTNET"
        _, profile = self.store.profile(1)
        result = asyncio.run(self.engine.emergency_stop(1, profile, self.client, True))
        self.assertFalse(result[-1].ok)
        self.assertEqual(self.client.calls, [])
        self.assertTrue(self.client.rows)

    def test_network_mismatched_pending_intent_blocks_other_new_risk(self):
        self.client.network = self.engine.reader.network = "MAINNET"
        self.engine.journal.prepare(ACCOUNT, "ETH|", {"network": "TESTNET", "action": "UNKNOWN"})
        self.run_cycle()
        self.assertEqual(self.client.calls, [])
        self.assertIn("ETH|", self.store.profile(1)[1]["runtime"]["recovery_required"])

    def test_reader_signer_network_mismatch_cannot_trade(self):
        self.client.network, self.engine.reader.network = "MAINNET", "TESTNET"
        with self.assertRaises(ValueError): self.run_cycle()
        self.assertEqual(self.client.calls, [])

    def test_execution_network_identity_is_persisted_and_mismatch_holds(self):
        self.client.network = self.engine.reader.network = "MAINNET"
        self.run_cycle()
        owned = self.engine.journal.owned(ACCOUNT)
        self.assertTrue(owned)
        self.assertTrue(all(row["network"] == "MAINNET" for row in owned.values()))
        self.client.calls.clear()
        self.client.network = self.engine.reader.network = "TESTNET"
        self.run_cycle()
        self.assertEqual(self.client.calls, [])
        self.assertTrue(self.store.profile(1)[1]["runtime"]["recovery_required"])
        self.assertEqual(self.engine.journal.owned(ACCOUNT), owned)

    def test_delayed_execution_proof_keeps_unknown_and_does_not_retry(self):
        def unavailable(*args): raise ValueError("Synthetic delayed order visibility")
        self.client.verify_copy_execution = unavailable
        self.run_cycle()
        self.assertIn("BTC|", self.engine.journal.pending(ACCOUNT))
        self.assertNotIn("BTC|", self.engine.journal.owned(ACCOUNT))
        count = len(self.trades())
        self.run_cycle()
        self.assertEqual(len(self.trades()), count)

    def test_copy_proof_requires_order_fill_and_position_agreement(self):
        from integrations.hyperliquid import HyperliquidAccount
        client = HyperliquidAccount.__new__(HyperliquidAccount)
        client.address, client.base = ACCOUNT, "https://api.hyperliquid-testnet.xyz"
        client._sdk_coin = lambda coin, dex="": coin
        fill = {"coin": "BTC", "oid": 7, "tid": 8, "sz": "1", "side": "B"}
        client.info = SimpleNamespace(
            query_order_by_oid=lambda *args: {"status": "order", "order": {"status": "filled", "order": {"oid": 7, "coin": "BTC", "side": "B"}}},
            user_fills_by_time=lambda *args: [dict(fill)])
        response = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 7, "totalSz": "1"}}]}}}
        proof = client.verify_copy_execution(response, "BTC", "", None, position(), 1000)
        self.assertEqual(proof["trade_ids"], [8])
        for rows in ([], [dict(fill, oid=9)], [fill, fill], [dict(fill, sz="NaN")], [dict(fill, side="A")]):
            client.info.user_fills_by_time = lambda *args, rows=rows: rows
            with self.assertRaises(ValueError): client.verify_copy_execution(response, "BTC", "", None, position(), 1000)

    def test_unknown_funding_and_nonfinite_spread_never_authorize_entry(self):
        for context in ({}, {"funding_bps_hour": None}, {"funding_bps_hour": float("nan")},
                        {"funding_bps_hour": float("inf")}, {"funding_bps_hour": 0, "observed_monotonic": -1000}):
            self.engine.market_cache.clear()
            self.engine.reader.market_context = lambda *args, value=context: value
            self.run_cycle()
            self.assertEqual(self.client.calls, [])
        self.engine.reader.market_context = lambda *args: {"funding_bps_hour": 0}
        self.engine.market_cache.clear()
        for value in (float("nan"), float("inf"), -1, None):
            self.client.spread_bps = lambda *args, value=value: value
            self.run_cycle()
            self.assertEqual(self.client.calls, [])

    def test_raw_position_unknown_is_not_flat_for_either_adapter(self):
        from core.hyperliquid import HyperliquidReader
        from integrations.hyperliquid import HyperliquidAccount
        for normalizer in (HyperliquidReader._positions, HyperliquidAccount._positions):
            self.assertEqual(normalizer({"assetPositions": []}, "CRYPTO", ""), [])
            self.assertEqual(normalizer({"assetPositions": [{"position": {"coin": "BTC", "szi": "0"}}]}, "CRYPTO", ""), [])
            for value in (None, "NaN", "Infinity", True, "garbage"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    normalizer({"assetPositions": [{"position": {"coin": "BTC", "szi": value}}]}, "CRYPTO", "")

    def test_market_context_missing_is_error_valid_zero_is_preserved(self):
        from core.hyperliquid import HyperliquidReader
        reader = HyperliquidReader.__new__(HyperliquidReader)
        for raw in ({}, [], [{"universe": [{"name": "BTC"}]}, []],
                    [{"universe": [{"name": "BTC"}]}, [{"markPx": "100", "openInterest": "0"}]]):
            reader._info = lambda payload, raw=raw: raw
            with self.assertRaises(ValueError): reader.market_context("BTC")
        reader._info = lambda payload: [{"universe": [{"name": "BTC"}]}, [{"funding": "0", "markPx": "100", "openInterest": "0"}]]
        self.assertEqual(reader.market_context("BTC")["funding_bps_hour"], 0)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = Fernet.generate_key()
        self.store = Storage(self.directory.name, self.key)
        _, profile = self.store.profile(1)
        profile.update(account={"id": "offline", "name": "Offline", "address": ACCOUNT},
                       leaders=[SOURCE_A], copy_enabled=True, max_leverage=40,
                       risk_mode="standard", strategy_mode="swing", leader_exit_only=True)
        self.store.update_profile(1, profile)
        self.settings = SimpleNamespace(auto_trading=True, max_leverage=40, max_position_pct=100.,
                                        max_total_exposure_usd=100000., max_slippage_pct=.5,
                                        entry_price_tolerance_pct=1.)
        self.engine = CopyEngine(Reader(), self.store, self.settings)
        self.client = ExchangeBoundary()
        self.notifications = []

    def patch(self, **changes):
        _, profile = self.store.profile(1)
        profile.update(changes)
        self.store.update_profile(1, profile)

    def runtime(self, **changes):
        _, profile = self.store.profile(1)
        profile["runtime"].update(changes)
        self.store.update_runtime(1, profile["runtime"])

    def managed(self, row, *, source_evidence=True):
        self.client.seed(row)
        self.runtime(managed=[CopyEngine._runtime_key(CopyEngine._key(row["coin"], row.get("dex")))])
        if not source_evidence:
            return  # Explicit legacy managed-flag fixture, no invented journal.
        # Explicit synthetic bot provenance, not merely a runtime managed flag.
        # These tests exercise execution/collateral behavior for known ownership.
        # Missing/unsafe attribution is covered separately in source-allocation tests.
        market = CopyEngine._runtime_key(CopyEngine._key(row["coin"], row.get("dex")))
        operation = self.engine.journal.prepare(ACCOUNT, market, {
            "action": "OFFLINE_TEST_FIXTURE", "network": self.client.network})
        self.engine.journal.finish(operation, {"ok": True}, {
            "managed": True, "position": deepcopy(row), "size": row["size"], "side": row["side"],
            "source_targets": [{"wallet": SOURCE_A, "margin": row["margin_used"],
                "signed_notional": row["position_value"] * (1 if row["side"] == "LONG" else -1),
                "slot_budget": self.client.cash / 3}], "attribution": "synthetic_test_fixture"})

    def run_cycle(self, snapshots=None, notify=None):
        _, profile = self.store.profile(1)
        async def capture(result, account): self.notifications.append(result)
        return asyncio.run(self.engine.sync_profile(1, profile, self.client,
            snapshots if snapshots is not None else [snapshot(positions=[position(notional=1000)])], notify or capture))

    def trades(self): return [call for call in self.client.calls if call[0] in {"open", "reduce", "close"}]

    def test_pausing_all_copying_prevents_every_exchange_mutation(self):
        self.managed(position())
        self.patch(copy_enabled=False)
        self.run_cycle([snapshot()])
        self.assertEqual(self.client.calls, [])
        self.assertIn(("BTC", ""), self.client.rows)

    def test_disabled_market_neither_opens_nor_closes(self):
        self.managed(position())
        self.patch(crypto_enabled=False)
        self.run_cycle([snapshot(positions=[position("ETH", 1000)])])
        self.assertEqual(self.client.calls, [])
        self.assertIn(("BTC", ""), self.client.rows)

    def test_paused_source_holds_existing_position(self):
        self.managed(position(notional=50))
        self.patch(leader_enabled={SOURCE_A: False})
        self.run_cycle()
        self.assertEqual(self.client.calls, [])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["size"], .5)

    def test_source_pause_survives_source_closing_its_own_position(self):
        self.run_cycle()
        self.patch(leader_enabled={SOURCE_A: False})
        self.client.calls.clear()
        self.run_cycle([snapshot()])
        self.assertEqual(self.client.calls, [], "Pausing a source must not become a close when its snapshot becomes empty")
        self.assertIn(("BTC", ""), self.client.rows)

    def test_shared_market_with_paused_source_is_not_rebalanced(self):
        self.patch(leaders=[SOURCE_A, SOURCE_B], leader_enabled={SOURCE_A: False, SOURCE_B: True})
        self.managed(position(notional=100))
        self.run_cycle([snapshot(SOURCE_A, [position(notional=500)]), snapshot(SOURCE_B, [position(notional=500)])])
        self.assertEqual(self.client.calls, [], "A source HOLD protects shared exposure from partial rebalancing")

    def test_fixed_thirds_and_cumulative_source_budget(self):
        results = self.run_cycle([snapshot(positions=[position(coin, 1000) for coin in ("BTC", "ETH", "SOL")])])
        self.assertEqual(len([r for r in results if r.action == "OPEN"]), 3)
        self.assertLessEqual(sum(p["margin_used"] for p in self.client.rows.values()), 100.00001)
        records = ExecutionJournal(self.directory.name).owned(ACCOUNT)
        source_margins = [source["margin"] for record in records.values() for source in record["source_targets"]]
        self.assertAlmostEqual(sum(source_margins), 100.)
        self.assertTrue(all(source["slot_budget"] == 100. for record in records.values() for source in record["source_targets"]))

    def test_lower_follower_leverage_keeps_source_margin_percentage(self):
        self.patch(max_leverage=5)
        self.run_cycle([snapshot(positions=[position(notional=4000, leverage=40)])])
        actual = self.client.rows[("BTC", "")]
        self.assertAlmostEqual(actual["position_value"], 50.)
        self.assertAlmostEqual(actual["margin_used"], 10.)
        self.assertEqual(actual["leverage"], 5)

    def test_daily_loss_blocks_new_positions(self):
        self.client.pnl = -100
        self.run_cycle()
        self.assertEqual(self.client.calls, [])
        self.assertIn("RISK_STOP", [row.action for row in self.notifications])

    def test_daily_loss_blocks_add_but_still_follows_exit(self):
        self.managed(position(notional=50))
        self.client.pnl = -100
        self.run_cycle()
        self.assertEqual(self.trades(), [])
        self.run_cycle([snapshot()])
        self.assertEqual([call[0] for call in self.trades()], ["close"])
        self.assertEqual(self.client.rows, {})
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], [])

    def test_daily_loss_allows_reduction(self):
        self.managed(position(notional=100))
        self.client.pnl = -100
        self.run_cycle([snapshot(positions=[position(notional=500)])])
        self.assertEqual([call[0] for call in self.trades()], ["reduce"])
        self.assertAlmostEqual(self.client.rows[("BTC", "")]["size"], .5)

    def test_manual_and_ai_holds_block_reopen_and_close(self):
        for field in ("manual_hold_keys", "ai_hold_keys"):
            with self.subTest(field=field):
                self.runtime(**{field: {"BTC|": {"reason": "offline-test"}}})
                self.client.rows.clear(); self.client.calls.clear()
                self.run_cycle()
                self.assertEqual(self.client.calls, [])
                self.managed(position(), source_evidence=False)
                self.run_cycle([snapshot()])
                self.assertEqual(self.client.calls, [])
                self.runtime(**{field: {}})

    def test_xyz_alias_is_one_managed_market(self):
        self.client.seed(position("INTC", 100, dex="xyz"))
        self.runtime(managed=["INTC|xyz"])
        self.run_cycle([snapshot(positions=[position("xyz:INTC", 1000)])])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], ["xyz:INTC|xyz"])

    def test_open_ownership_commits_before_notification_failure(self):
        async def broken_notify(result, account): raise RuntimeError("Synthetic Telegram failure")
        try: self.run_cycle(notify=broken_notify)
        except RuntimeError: pass
        restored = Storage(self.directory.name, self.key).profile(1)[1]
        self.assertIn("BTC|", restored["runtime"]["managed"])
        ownership = ExecutionJournal(self.directory.name).owned(ACCOUNT)["BTC|"]
        self.assertTrue(ownership["managed"])
        self.assertEqual(ownership["source_targets"][0]["wallet"], SOURCE_A)

    def test_prepared_execution_holds_market_after_restart_without_duplicate(self):
        self.engine.journal.prepare(ACCOUNT, "BTC|", {"action": "RECONCILE"})
        self.engine = CopyEngine(Reader(), Storage(self.directory.name, self.key), self.settings)
        self.run_cycle()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["recovery_required"], ["BTC|"])

    def test_unknown_post_submit_read_prevents_retry_after_restart(self):
        self.client.fail_after_submit = True
        self.run_cycle()
        self.assertEqual(len(self.trades()), 1)
        self.assertEqual(self.engine.journal.pending(ACCOUNT), {"BTC|"})
        self.engine = CopyEngine(Reader(), self.store, self.settings)
        self.run_cycle()
        self.assertEqual(len(self.trades()), 1)

    def test_follower_read_failure_never_closes_or_forgets_positions(self):
        self.managed(position())
        self.client.fail_positions = True
        self.run_cycle([snapshot()])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], ["BTC|"])

    def test_partial_close_retains_ownership_and_blocks_blind_retry(self):
        self.managed(position())
        self.client.partial_close = True
        self.run_cycle([snapshot()])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], ["BTC|"])
        self.assertIn("BTC|", self.engine.journal.pending(ACCOUNT))
        self.run_cycle([snapshot()])
        self.assertEqual(len(self.trades()), 1)

    def test_max_positions_applies_within_one_cycle(self):
        self.run_cycle([snapshot(positions=[position(f"COIN{i}", 1000) for i in range(8)])])
        self.assertLessEqual(len(self.client.rows), self.engine.risk(self.store.profile(1)[1])["max_positions"])

    def test_disabled_market_also_holds_when_optional_roe_stops_enabled(self):
        self.managed(position(roe=-90))
        self.patch(crypto_enabled=False, leader_exit_only=False)
        self.run_cycle([snapshot()])
        self.assertEqual(self.client.calls, [])

    def test_detached_market_also_holds_when_optional_roe_stops_enabled(self):
        self.managed(position(roe=-90))
        self.patch(leader_exit_only=False)
        self.runtime(detached_keys=["BTC|"])
        self.run_cycle([snapshot()])
        self.assertEqual(self.client.calls, [])

    def test_existing_manual_position_is_never_adopted(self):
        self.client.seed(position(notional=50))
        self.run_cycle()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], [])

    def test_telegram_failure_cannot_prevent_a_leader_exit_at_daily_limit(self):
        self.managed(position())
        self.client.pnl = -100
        async def broken_notify(result, account): raise RuntimeError("Synthetic Telegram failure")
        try: self.run_cycle([snapshot()], notify=broken_notify)
        except RuntimeError: pass
        self.assertNotIn(("BTC", ""), self.client.rows, "An alert transport failure must not bypass an eligible leader exit")

    def test_telegram_failure_cannot_drop_risk_close_cooldown(self):
        self.managed(position(roe=-90))
        self.patch(leader_exit_only=False)
        async def broken_notify(result, account): raise RuntimeError("Synthetic Telegram failure")
        try: self.run_cycle(notify=broken_notify)
        except RuntimeError: pass
        self.assertNotIn(("BTC", ""), self.client.rows)
        self.engine = CopyEngine(Reader(), self.store, self.settings)
        self.run_cycle()
        self.assertNotIn(("BTC", ""), self.client.rows, "Risk-close cooldown must survive failed Telegram delivery and restart")

    def test_small_btc_reverse_is_close_then_open_not_oversized_reduce_only(self):
        self.client.prices[("BTC", "")] = 80000.
        self.managed(position(notional=46.4, side="SHORT", leverage=40, price=80000.))
        results = self.run_cycle([snapshot(positions=[position(notional=464., side="LONG", leverage=40, price=80000.)])])
        self.assertEqual([call[0] for call in self.trades()], ["close", "open"],
                         "Opposite signs require reversal; size-squared cannot be compared to a size tolerance")
        self.assertEqual([row.action for row in results], ["REVERSE"])
        actual = self.client.rows[("BTC", "")]
        self.assertEqual(actual["side"], "LONG")
        self.assertAlmostEqual(actual["size"], .00058)

    def test_reverse_never_opens_new_side_below_exchange_minimum(self):
        self.client.prices[("BTC", "")] = 80000.
        self.managed(position(notional=46.4, side="SHORT", leverage=40, price=80000.))
        self.run_cycle([snapshot(positions=[position(notional=40., side="LONG", leverage=40, price=80000.)])])
        opens = [call for call in self.trades() if call[0] == "open"]
        self.assertTrue(all(call[2] * self.client.mid(*call[1]) >= 10. for call in opens),
                        "Combined reverse delta cannot make a new opposite entry below $10 executable")

    def test_new_entry_rejects_missing_malformed_or_insufficient_fresh_margin_before_intent(self):
        for capacity in (None, False, -1., float("nan"), float("inf"), "1000", 0., 100.):
            with self.subTest(capacity=capacity):
                self.runtime(entry_block_notified=[])
                self.client.available_margin = None if capacity is None else lambda dex="", value=capacity: value
                results = self.run_cycle()
                self.assertEqual(self.client.calls, [], "No leverage or order mutation before collateral preflight")
                self.assertEqual(results[0].action, "EXECUTION_BLOCK")
                self.assertEqual(self.engine.journal.pending(ACCOUNT), set())
        db = self.engine.journal.connect()
        try: self.assertEqual(db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
        finally: db.close()

    def test_available_margin_error_never_becomes_unknown_order(self):
        def unavailable(dex=""): raise RuntimeError("Synthetic capacity outage")
        self.client.available_margin = unavailable
        self.assertEqual(self.run_cycle()[0].action, "EXECUTION_BLOCK")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.engine.journal.pending(ACCOUNT), set())

    def test_add_uses_incremental_margin_and_fee_slippage_buffer(self):
        self.managed(position(notional=30))
        self.client.available_margin = lambda dex="": 70.
        self.assertEqual(self.run_cycle()[0].action, "EXECUTION_BLOCK")
        self.assertEqual(self.client.calls, [])
        self.client.available_margin = lambda dex="": 70.43
        self.assertEqual(self.run_cycle()[0].action, "ADD")
        self.assertEqual([c[0] for c in self.trades()], ["open"])

    def test_leverage_decrease_needs_extra_collateral_even_without_size_change(self):
        self.managed(position(notional=100, leverage=5))
        self.client.available_margin = lambda dex="": 79.99
        self.assertEqual(self.run_cycle()[0].action, "EXECUTION_BLOCK")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.engine.journal.pending(ACCOUNT), set())
        self.client.available_margin = lambda dex="": 80.
        self.assertEqual(self.run_cycle()[0].action, "LEVERAGE_UPDATE")
        self.assertEqual([c[0] for c in self.client.calls], ["leverage"])

    def test_reduction_and_exit_ignore_unavailable_collateral(self):
        self.managed(position(notional=100))
        def unavailable(dex=""): raise AssertionError("Reduction must not read capacity")
        self.client.available_margin = unavailable
        self.assertEqual(self.run_cycle([snapshot(positions=[position(notional=400)])])[0].action, "PARTIAL_CLOSE")
        self.assertEqual(self.run_cycle([snapshot()])[0].action, "FULL_CLOSE")
        self.assertEqual([c[0] for c in self.trades()], ["reduce", "close"])

    def test_reduction_defers_leverage_decrease_instead_of_blocking_exit(self):
        self.managed(position(notional=100, leverage=5))
        self.client.leverages[("BTC", "")] = 5
        def unavailable(dex=""): raise AssertionError("Reduce first; collateral is not needed")
        self.client.available_margin = unavailable
        results = self.run_cycle([snapshot(positions=[position(notional=400)])])
        self.assertEqual(results[0].action, "PARTIAL_CLOSE")
        self.assertEqual([c[0] for c in self.client.calls], ["reduce"])
        self.assertEqual(self.client.rows[("BTC", "")]["leverage"], 5)

    def test_reverse_refetches_after_close_and_records_flat_when_capacity_insufficient(self):
        self.managed(position(notional=100, side="SHORT"))
        checked = []
        def capacity(dex=""):
            checked.append(dex)
            self.assertNotIn(("BTC", ""), self.client.rows)
            self.assertEqual([c[0] for c in self.trades()], ["close"])
            return 0.
        self.client.available_margin = capacity
        result = self.run_cycle()[0]
        self.assertEqual(result.action, "FULL_CLOSE")
        self.assertTrue(result.ok)
        self.assertEqual(checked, [""])
        self.assertEqual([c[0] for c in self.client.calls], ["close"])
        self.assertEqual(self.engine.journal.pending(ACCOUNT), set())
        self.assertFalse(self.engine.journal.owned(ACCOUNT)["BTC|"]["managed"])
        self.assertNotIn("BTC|", self.store.profile(1)[1]["runtime"]["managed"])

    def test_reverse_capacity_outage_preserves_verified_close_not_unknown(self):
        self.managed(position(notional=100, side="SHORT"))
        def unavailable(dex=""): raise RuntimeError("Synthetic capacity outage")
        self.client.available_margin = unavailable
        result = self.run_cycle()[0]
        self.assertEqual(result.action, "FULL_CLOSE")
        self.assertTrue(result.ok)
        self.assertEqual([c[0] for c in self.client.calls], ["close"])
        self.assertEqual(self.engine.journal.pending(ACCOUNT), set())

    def test_xyz_entry_checks_its_dex_collateral(self):
        checked = []
        def capacity(dex=""):
            checked.append(dex)
            return 0.
        self.client.available_margin = capacity
        self.run_cycle([snapshot(positions=[position("xyz:INTC", 1000, dex="xyz")])])
        self.assertEqual(checked, ["xyz"])
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
