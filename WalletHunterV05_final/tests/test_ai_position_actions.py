from contextlib import closing
from copy import deepcopy
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.ai_position_actions import AiPositionActions, BAR, COOLDOWN
from core.legacy_route_calls import ai_position as legacy_ai_position


NOW = 20000*86400000 + 12*3600000 + 1000
ACCOUNT = "0x" + "1"*40
SOURCE = "0x" + "2"*40


def pos(coin="ETH", side="SHORT", size=.0188, entry=2444.6, price=2510, leverage=20, dex=""):
    return {"coin": coin, "dex": dex, "size": size, "side": side, "entry_price": entry,
            "leverage": leverage, "margin_mode": "cross", "roe": -53., "margin_used": size*price/leverage,
            "unrealized_pnl": (price-entry)*size*(1 if side=="LONG" else -1)}


def facts(now=NOW, support=True, side="SHORT"):
    sign = 1 if side == "LONG" else -1
    if not support: sign *= -1
    return {"trend_ema20_50": {"ema20": 100+sign, "ema50": 100}, "rsi14": 50,
            "macd_hist": sign, "atr14_pct": 1, "volume_ratio20": 1,
            "levels20": {"support": 90, "resistance": 110}, "funding_bps_hour": .1,
            "open_interest": 100, "asof_ms": now, "candle_close_ms": now//BAR*BAR-1}


class Reader:
    def __init__(self, client): self.client, self.fills = client, []
    def _info(self, payload):
        kind = payload["type"]
        if kind == "userFillsByTime": return deepcopy(self.fills)
        if kind == "metaAndAssetCtxs":
            meta = self.client.meta(payload.get("dex", ""))
            return [meta, [{"funding": ".00001", "openInterest": "100"} for _ in meta["universe"]]]
        if kind == "candleSnapshot":
            end = payload["req"]["endTime"]//BAR*BAR
            return [{"t": end-(i+1)*BAR, "T": end-i*BAR-1, "o": 100, "h": 102,
                     "l": 98, "c": 100, "v": 10} for i in reversed(range(100))]
        raise AssertionError(kind)


class Public:
    address = ACCOUNT
    base = "https://api.hyperliquid.xyz"
    def __init__(self):
        self.live = [pos()]
        self.price = 2510.
        self.balance = 3000.
        self.capacity = 3000.
        self.orders = []
        self.digits = 4
        self.reader = Reader(self)
    def positions(self, *args): return deepcopy(self.live)
    def capital_snapshot(self): return SimpleNamespace(mode="unifiedAccount", sizing_base_usdc=self.balance)
    def available_margin(self, dex): return self.capacity
    def mid(self, coin, dex): return self.price
    def frontend_open_orders(self, dex): return deepcopy(self.orders)
    def meta(self, dex=""):
        coins = ["xyz:INTC", "xyz:NVDA"] if dex else ["ETH", "BTC", "SOL"]
        return {"universe": [{"name": coin, "szDecimals": self.digits, "maxLeverage": 40} for coin in coins]}


class Signer:
    def __init__(self, public):
        self.public = public
        self.address, self.base = public.address, public.base
        self.behavior = "filled"
        self.calls = []
    def submit_position_ioc(self, coin, is_buy, size, limit, reduce_only, cloid, dex="", *, expires_ms, expected_position):
        self.calls.append((coin, is_buy, size, limit, reduce_only, cloid, dex, expires_ms))
        if self.behavior == "timeout": raise TimeoutError("secret transport error")
        amount = round(size/2, 4) if self.behavior == "partial" else size
        index = next(i for i, position in enumerate(self.public.live) if position["coin"] == coin)
        before = self.public.live[index]
        after = deepcopy(before)
        after["size"] += -amount if reduce_only else amount
        if not reduce_only:
            after["entry_price"] = (before["size"]*before["entry_price"]+amount*limit)/after["size"]
        after["margin_used"] = after["size"]*self.public.price/after["leverage"]
        self.public.live[index] = after
        if self.behavior == "mismatch": self.public.live[index]["size"] *= 2
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 10, "totalSz": str(amount), "avgPx": str(limit)}}]}}}


class PositionActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.clock = [0.]
        self.service = AiPositionActions(self.temp.name, monotonic=lambda: self.clock[0],
                                         legacy_test_executor=legacy_ai_position)
        self.public = Public(); self.signer = Signer(self.public)
        self.profile = {"account": {"address": ACCOUNT}, "leaders": [SOURCE], "ai_review_enabled": True,
                        "max_leverage": 20, "runtime": {"managed": ["ETH|"]}}
        self.factory_calls = 0; self.persisted = []
        self.indicators = facts()
        self.mock = patch("core.ai_position_actions.analyse", side_effect=lambda *args: deepcopy(self.indicators))
        self.mock.start(); self.addCleanup(self.mock.stop)
        self.own()

    def own(self, index=0):
        p = deepcopy(self.public.live[index]); p["snapshot_started_ms"] = NOW-10000
        key = p["coin"]+"|"+(p.get("dex") or "")
        op = self.service.journal.prepare(ACCOUNT, key, {"action": "TEST_COPY"})
        self.service.journal.finish(op, {"ok": True}, {"managed": True, "position": p, "size": p["size"], "side": p["side"],
            "source_targets": [{"wallet": SOURCE, "margin": p["margin_used"], "signed": -p["size"]*self.public.price}]})

    def prepare(self, now=NOW, force=False):
        return self.service.prepare("139", self.profile, self.public, self.public.reader, now, force_refresh=force)

    def proposal(self, action="REDUCE"):
        result = self.prepare()
        matches = [p for p in result["pending"] if p["payload"]["action"] == action]
        self.assertEqual(len(matches), 1, result)
        return matches[0]

    def factory(self):
        self.factory_calls += 1
        self.assertTrue(self.profile["runtime"]["ai_position_action_holds"])
        self.assertTrue(self.persisted)
        return self.signer

    def persist(self): self.persisted.append(deepcopy(self.profile["runtime"]))

    def decide(self, proposal, confirm=True, now=NOW+1000):
        return self.service.decide("139", proposal["id"], confirm, self.profile, self.public, self.factory, self.persist, now)

    def test_actual_eth_short_reduction_preview_no_signing(self):
        p = self.proposal()["payload"]
        self.assertEqual(p["size"], .0047)
        self.assertEqual(p["position_before"]["size"], .0188)
        self.assertAlmostEqual(p["expected_after"]["size"], .0141)
        self.assertTrue(p["is_buy"])
        self.assertTrue(p["reduce_only"])
        self.assertEqual(p["source_slot_usdc"], 1000)
        self.assertLess(p["realized_pnl_estimate_usdc"], 0)
        self.assertIsNone(p["hypothesis"]["probability"])
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(self.signer.calls, [])

    def test_rescue_above_minus40_boundary_offers_nothing(self):
        self.public.live[0]["roe"] = -39.99
        self.assertEqual(self.prepare()["pending"], [])
        self.assertEqual(self.factory_calls, 0)

    def test_rescue_at_minus40_boundary_prepares_but_never_signs(self):
        self.public.live[0]["roe"] = -40
        rows = self.prepare()["pending"]
        self.assertEqual({row["payload"]["action"] for row in rows}, {"REDUCE", "AVERAGE"})
        for row in rows:
            self.assertEqual(row["payload"]["policy"], {"trigger_roe_pct": -40, "target_roe_pct": 3,
                "failure_roe_pct": -120, "max_extra_source_fraction": .5, "max_additions": 4})
        self.assertEqual(self.factory_calls, 0)

    def test_rescue_below_minus40_boundary_prepares(self):
        self.public.live[0]["roe"] = -40.01
        self.assertEqual({row["payload"]["action"] for row in self.prepare()["pending"]}, {"REDUCE", "AVERAGE"})
        self.assertEqual(self.factory_calls, 0)

    def test_exact_minus40_confirm_rechecks_then_sends_once(self):
        self.public.live[0]["roe"] = -40
        row = self.proposal()
        result = self.decide(row)
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(len(self.signer.calls), 1)
        self.assertEqual(self.decide(row)["status"], "FILLED")
        self.assertEqual(len(self.signer.calls), 1)

    def test_recovery_above_minus40_during_prepare_does_not_offer(self):
        self.public.live[0]["roe"] = -40
        original = self.public.positions
        calls = []
        def fresh(*args):
            calls.append(True)
            positions = original(*args)
            if len(calls) > 1:
                positions[0]["roe"] = -39.99
            return positions
        self.public.positions = fresh
        self.assertEqual(self.prepare()["pending"], [])
        self.assertEqual(self.factory_calls, 0)

    def test_supportive_average_is_small_and_unknown_probability(self):
        p = self.proposal("AVERAGE")["payload"]
        self.assertFalse(p["reduce_only"])
        self.assertFalse(p["is_buy"])
        self.assertLessEqual(p["margin_change_estimate_usdc"], self.public.live[0]["margin_used"]*.25)
        self.assertLessEqual(p["margin_change_estimate_usdc"], p["extra_remaining_usdc"]*.25)
        self.assertEqual(p["hypothesis"]["classification"], "EXPERIMENTAL_REBOUND")
        self.assertIsNone(p["hypothesis"]["probability"])

    def test_adverse_signals_do_not_offer_averaging(self):
        self.indicators = facts(support=False)
        result = self.prepare()
        self.assertEqual([p["payload"]["action"] for p in result["pending"]], ["REDUCE"])
        self.assertIn("rebound_not_supported", str(result["availability"]))

    def test_decline_without_account_or_signer_cooldown_then_refresh(self):
        p = self.proposal()
        result = self.service.decide("139", p["id"], False, {}, None, None, None, NOW+1)
        self.assertEqual(result["status"], "DECLINED")
        self.assertFalse(any(r["payload"]["action"]=="REDUCE" for r in self.prepare(NOW+1000, True)["pending"]))
        self.assertTrue(any(r["payload"]["action"]=="REDUCE" for r in self.prepare(NOW+COOLDOWN+1, True)["pending"]))
        self.assertEqual(self.factory_calls, 0)

    def test_expired_ui_form_can_refresh_but_background_does_not_spam(self):
        self.proposal()
        self.assertEqual(self.prepare(NOW+91000)["pending"], [])
        result = self.prepare(NOW+92000, True)
        self.assertTrue(result["pending"])

    def test_verified_reduce_preserves_source_and_holds_copy_after_restart(self):
        p = self.proposal()
        result = self.decide(p)
        self.assertEqual(result["status"], "FILLED")
        owned = self.service.journal.owned(ACCOUNT)["ETH|"]
        self.assertEqual(owned["source_targets"][0]["wallet"], SOURCE)
        self.assertAlmostEqual(owned["position"]["size"], .0141)
        self.assertIn("ETH|", self.service.reserved_markets(None, ACCOUNT))
        restarted = AiPositionActions(self.temp.name, legacy_test_executor=legacy_ai_position)
        self.assertIn("ETH|", restarted.reserved_markets(None, ACCOUNT))
        self.assertEqual(self.decide(p)["status"], "FILLED")
        self.assertEqual(len(self.signer.calls), 1)

    def test_verified_average_updates_weighted_entry_and_cumulative_budget(self):
        p = self.proposal("AVERAGE")
        result = self.decide(p)
        self.assertEqual(result["status"], "FILLED")
        self.assertGreater(result["result"]["position_after"]["size"], .0188)
        self.assertEqual(result["result"]["cumulative_source_additions"], 1)
        self.assertGreater(result["result"]["actual_extra_margin_usdc"], 0)
        self.assertEqual(self.service.journal.owned(ACCOUNT)["ETH|"]["source_targets"][0]["wallet"], SOURCE)

    def test_partial_fill_has_actual_size_and_no_retries(self):
        self.signer.behavior = "partial"
        p = self.proposal()
        result = self.decide(p)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertNotEqual(result["result"]["position_after"]["size"], p["payload"]["expected_after"]["size"])
        self.decide(p)
        self.assertEqual(len(self.signer.calls), 1)

    def test_missing_canonical_context_fails_closed_without_legacy_or_signer(self):
        proposal = self.proposal()
        strict = AiPositionActions(self.temp.name)
        result = strict.decide("139", proposal["id"], True, self.profile, self.public,
                               self.factory, self.persist, NOW + 1000)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(self.signer.calls, [])

    def test_timeout_is_durable_unknown_never_retry_or_resume(self):
        self.signer.behavior = "timeout"
        p = self.proposal()
        result = self.decide(p)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertNotIn("secret", str(result))
        self.assertIn("ETH|", self.service.journal.pending(ACCOUNT))
        self.decide(p)
        self.assertEqual(len(self.signer.calls), 1)
        with self.assertRaises(ValueError):
            self.service.release("139", "ETH|", self.profile, self.public, self.persist, NOW+2000)

    def test_user_resume_requires_unchanged_verified_position(self):
        self.decide(self.proposal())
        result = self.service.release("139", "ETH|", self.profile, self.public, self.persist, NOW+2000)
        self.assertTrue(result["released"])
        self.assertEqual(self.service.reserved_markets(None, ACCOUNT), {})
        self.assertEqual(self.profile["runtime"]["ai_position_action_holds"], {})

    def test_resume_rejects_new_fills_after_verified_snapshot(self):
        self.decide(self.proposal())
        self.public.reader.fills = [{"coin": "ETH", "time": NOW+1500}]
        with self.assertRaises(ValueError):
            self.service.release("139", "ETH|", self.profile, self.public, self.persist, NOW+2000)

    def test_changed_position_source_quote_budget_or_expiry_never_sign(self):
        for index, change in enumerate(("size", "source", "quote", "roe", "expiry")):
            prepared = self.prepare(NOW+index*3000, True)
            p = next(row for row in prepared["pending"] if row["payload"]["action"] == "REDUCE")
            original = deepcopy(self.public.live); leaders = self.profile["leaders"][:]
            if change == "size": self.public.live[0]["size"] += .01
            if change == "source": self.profile["leaders"] = []
            if change == "quote": self.public.price *= 1.01
            if change == "roe": self.public.live[0]["roe"] = -39.99
            result = self.decide(p, now=p["expires_ms"] if change=="expiry" else p["created_ms"]+1000)
            self.assertIn(result["status"], ("INVALIDATED", "EXPIRED"), change)
            self.assertEqual(self.factory_calls, 0)
            self.public.live=original; self.public.price=2510.; self.profile["leaders"]=leaders

    def test_two_source_positions_cannot_reuse_stale_addition_counter(self):
        self.public.live.append(pos("BTC"))
        self.own(1)
        prepared = self.prepare()
        averages = {row["payload"]["coin"]: row for row in prepared["pending"] if row["payload"]["action"] == "AVERAGE"}
        first = self.decide(averages["ETH"])
        self.assertEqual(first["status"], "FILLED")
        self.assertEqual(first["result"]["cumulative_source_additions"], 1)
        self.assertEqual(self.decide(averages["BTC"], now=NOW+2000)["status"], "INVALIDATED")
        self.assertEqual(len(self.signer.calls), 1)
        fresh = self.prepare(NOW+3000, True)
        second = next(row for row in fresh["pending"] if row["payload"]["action"] == "AVERAGE" and row["payload"]["coin"] == "BTC")
        result = self.decide(second, now=NOW+4000)
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["result"]["cumulative_source_additions"], 2)
        self.assertAlmostEqual(result["result"]["cumulative_source_extra_usdc"], first["result"]["actual_extra_margin_usdc"]+result["result"]["actual_extra_margin_usdc"])

    def test_confirmation_clock_cannot_precede_immutable_form(self):
        p = self.proposal()
        self.assertEqual(self.decide(p, now=NOW-1)["status"], "INVALIDATED")
        self.assertEqual(self.factory_calls, 0)

    def test_slow_public_validation_expires_before_signer(self):
        p = self.proposal()
        original = self.public.available_margin
        def slow(dex):
            self.clock[0] += 100
            return original(dex)
        self.public.available_margin = slow
        average = next(row for row in self.service.summary("139", self.profile, NOW)["pending"] if row["payload"]["action"] == "AVERAGE")
        self.assertEqual(self.decide(average)["status"], "INVALIDATED")
        self.assertEqual(self.factory_calls, 0)

    def test_disabled_market_permits_reduce_but_not_increase(self):
        self.profile["crypto_enabled"] = False
        self.assertEqual([p["payload"]["action"] for p in self.prepare()["pending"]], ["REDUCE"])

    def test_other_core_source_position_is_in_budget_not_target(self):
        self.public.live.append(pos("SOL", size=1))
        self.own(1)
        prepared = self.prepare()
        self.assertTrue(prepared["pending"])
        self.assertTrue(all(row["payload"]["coin"] == "ETH" for row in prepared["pending"]))
        expected = sum(position["margin_used"] for position in self.public.live)
        self.assertAlmostEqual(prepared["pending"][0]["payload"]["source_reserved_usdc"], expected)

    def test_raw_xyz_fill_after_verification_is_rejected(self):
        self.public.live = [pos("xyz:INTC", "SHORT", 5, 40, 42, 5, "xyz")]
        self.public.price = 42
        self.own()
        self.public.reader.fills = [{"coin": "INTC", "time": NOW-1000}]
        self.assertEqual(self.prepare()["pending"], [])
        self.assertIn("fills_after_verified_ownership", str(self.prepare()["availability"]))

    def test_changed_signer_address_never_submits_and_preserves_hold(self):
        p = self.proposal()
        self.signer.address = "0x"+"9"*40
        result = self.decide(p)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.signer.calls, [])
        self.assertIn("ETH|", self.service.reserved_markets(None, ACCOUNT))

    def test_below_minimum_never_forces_reduction_or_average(self):
        self.public.live[0]["size"] = .001; self.public.live[0]["margin_used"] = .1255
        self.own()
        result = self.prepare()
        self.assertEqual(result["pending"], [])
        self.assertIn("minimum_notional", str(result["availability"]))

    def test_zero_capacity_blocks_average_but_not_reduce(self):
        self.public.capacity = 0
        actions = [p["payload"]["action"] for p in self.prepare()["pending"]]
        self.assertEqual(actions, ["REDUCE"])

    def test_addition_count_four_and_fifty_percent_cap_block_average(self):
        for usage in ({"source_wallet": SOURCE, "extra_margin_usdc": 0, "additions": 4},
                      {"source_wallet": SOURCE, "extra_margin_usdc": 500, "additions": 1}):
            with closing(sqlite3.connect(self.service.path)) as db:
                db.execute("DELETE FROM position_proposals"); db.commit()
            self.profile["runtime"]["ai_budget_usage"] = {"ETH|": usage}
            self.assertEqual([p["payload"]["action"] for p in self.prepare()["pending"]], ["REDUCE"])

    def test_unattributed_prior_extra_budget_fails_closed(self):
        self.profile["runtime"]["ai_budget_usage"] = {"ETH|": {"extra_margin_usdc": 5, "additions": 1}}
        self.assertEqual(self.prepare()["pending"], [])
        self.assertIn("prior_budget_usage_source_unknown", str(self.prepare()["availability"]))

    def test_existing_stop_never_cancelled_or_silently_ignored(self):
        self.public.orders = [{"coin": "ETH", "isTrigger": True, "oid": 3}]
        result = self.prepare()
        self.assertEqual(result["pending"], [])
        self.assertIn("stop_review_required", str(result["availability"]))
        self.assertEqual(self.public.orders[0]["oid"], 3)

    def test_xyz_raw_order_names_also_block(self):
        self.public.live = [pos("xyz:INTC", "SHORT", 5, 40, 42, 5, "xyz")]
        self.public.price = 42; self.public.orders = [{"coin": "INTC", "isTrigger": True}]
        self.own()
        self.assertEqual(self.prepare()["pending"], [])
        self.assertIn("stop_review_required", str(self.prepare()["availability"]))

    def test_history_incomplete_or_fills_at_snapshot_are_rejected(self):
        for rows in ([{"coin": "ETH", "time": NOW-10000}], [{"coin": "BTC", "time": NOW-10000}]*2000):
            self.public.reader.fills = rows
            self.assertEqual(self.prepare()["pending"], [])

    def test_user_isolation_and_no_boolean_coercion(self):
        p = self.proposal()
        for value in ("yes", 1, None):
            with self.assertRaises(ValueError): self.decide(p, value)
        self.assertEqual(self.service.summary("other", self.profile, NOW)["pending"], [])
        with self.assertRaises(ValueError):
            self.service.decide("other", p["id"], False, {}, None, None, None, NOW)

    def test_failed_persistence_before_signer_leaves_sql_hold(self):
        p = self.proposal()
        def fail(): raise IOError("failed")
        result = self.service.decide("139", p["id"], True, self.profile, self.public, self.factory, fail, NOW+1000)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.factory_calls, 0)
        self.assertIn("ETH|", self.service.reserved_markets(None, ACCOUNT))

    def test_notifications_once_and_foreign_user_cannot_mark(self):
        p = self.proposal()
        self.service.mark_notified("other", p["id"])
        self.assertTrue(any(r["id"]==p["id"] for r in self.service.pending_for_notification("139",self.profile,NOW)))
        self.service.mark_notified("139", p["id"])
        self.assertFalse(any(r["id"]==p["id"] for r in self.service.pending_for_notification("139",self.profile,NOW)))

    def test_corrupted_payload_is_rejected_without_signing(self):
        p = self.proposal()
        with closing(sqlite3.connect(self.service.path)) as db:
            db.execute("UPDATE position_proposals SET payload='{}' WHERE id=?", (p["id"],)); db.commit()
        with self.assertRaises(ValueError): self.decide(p)
        self.assertEqual(self.factory_calls, 0)

    def test_watermark_is_before_position_read_not_later_order_queries(self):
        p = self.proposal()
        original = self.public.frontend_open_orders
        def later(dex):
            result = original(dex)
            if self.signer.calls: self.clock[0] += 10
            return result
        self.public.frontend_open_orders = later
        result = self.decide(p)
        self.assertEqual(result["status"], "FILLED")
        owned = self.service.journal.owned(ACCOUNT)["ETH|"]
        self.assertEqual(owned["verified_at_ms"], NOW+1000)
        self.assertLess(owned["verified_at_ms"], NOW+11000)
