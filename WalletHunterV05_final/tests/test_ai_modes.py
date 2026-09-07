"""AI mode and public-data adapter contracts; no credentials or live orders."""
import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from core.ai_modes import AiModes, INTERVAL_MS


ACCOUNT = "0x" + "a" * 40
NOW = 2_000_000 * INTERVAL_MS + 60000


def profile():
    return {"account": {"address": ACCOUNT, "private_key": "untouched-ciphertext"},
            "leaders": ["0x"+"b"*40, "0x"+"c"*40], "copy_enabled": False,
            "ai_slot_selected": True, "ai_trader_enabled": True,
            "runtime": {"managed": ["BTC|"], "ai_budget_usage": {"BTC|": 7}}}


def candles(now=NOW):
    end = now // INTERVAL_MS * INTERVAL_MS
    return [{"t": end-(120-i)*INTERVAL_MS, "T": end-(119-i)*INTERVAL_MS-1,
             "o": str(100+i), "c": str(100+i), "h": str(102+i), "l": str(98+i), "v": "100"}
            for i in range(120)]


class PaperStub:
    def __init__(self):
        self.calls = []
        self.summaries = []
        self.positions = [{"coin": "BTC", "size": 1}]

    def tick(self, uid, account, budget_usdc, markets, now_ms):
        self.calls.append((uid, account, budget_usdc, copy.deepcopy(markets), now_ms))
        return {"status": "ACTIVE" if markets else "WAITING_DATA", "positions": self.positions}

    def summary(self, uid, account):
        self.summaries.append((uid, account))
        return {"status": "ACTIVE", "positions": copy.deepcopy(self.positions)}


class PublicReader:
    def __init__(self):
        self.calls = []
        self.cash = 300.
        self.now = NOW
        self.contexts = [{"funding": ".00001", "openInterest": "100"},
                         {"funding": "-.00001", "openInterest": "200"}]
        self.assets = [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                       {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]
        self.quotes = {"BTC": "219", "ETH": "219"}
        self.bad_candles = {}

    def balance(self, account):
        self.calls.append(("balance", account))
        return self.cash

    def mids(self, dex=""):
        self.calls.append(("mids", dex))
        return copy.deepcopy(self.quotes)

    def _info(self, payload):
        self.calls.append(copy.deepcopy(payload))
        if payload["type"] == "metaAndAssetCtxs":
            return [copy.deepcopy({"universe": self.assets}), copy.deepcopy(self.contexts)]
        if payload["type"] == "candleSnapshot":
            coin = payload["req"]["coin"]
            return self.bad_candles.get(coin, candles(self.now))
        raise AssertionError("Unexpected public request")


class AiModesTests(unittest.TestCase):
    def setUp(self):
        self.paper = PaperStub()
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.modes = AiModes(self.root.name, paper=self.paper)
        self.reader = PublicReader()
        self.profile = profile()
        self.clock = patch("core.ai_modes.time.time", return_value=NOW/1000)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_summary_independent_modes_and_no_network(self):
        self.profile["ai_review_enabled"] = False
        result = self.modes.summary(1, self.profile)
        self.assertTrue(result["trader"]["enabled"])
        self.assertFalse(result["rescue"]["enabled"])
        self.assertEqual(result["trader"]["execution_mode"], "PAPER")
        self.assertFalse(result["trader"]["real_execution_available"])
        self.assertEqual(result["trader"]["limits"], {"entry_pct": 10, "max_leverage": 40, "max_loss_pct": 10})
        self.assertEqual(result["trader"]["universe"], ["BTC", "ETH"])
        self.assertEqual(result["rescue"]["trigger_roe_pct"], -40)
        self.assertEqual(self.paper.summaries, [(1, ACCOUNT)])
        self.assertEqual(self.reader.calls, [])

    def test_each_flag_changes_without_touching_account_positions_copying_or_other_mode(self):
        before = copy.deepcopy(self.profile)
        result = self.modes.set_enabled(self.profile, "rescue", False)
        self.assertIs(result, self.profile)
        self.assertEqual(self.profile, dict(before, ai_review_enabled=False))
        before = copy.deepcopy(self.profile)
        self.modes.set_enabled(self.profile, "trader", False)
        self.assertEqual(self.profile, dict(before, ai_trader_enabled=False))
        self.assertEqual(self.paper.positions, [{"coin": "BTC", "size": 1}])

    def test_paper_observations_preserve_users_lower_leverage_limit(self):
        self.profile["max_leverage"] = 5
        self.modes.tick(1, self.profile, self.reader)
        self.assertTrue(self.paper.calls)
        self.assertTrue(all(row["max_leverage"] == 5 for row in self.paper.calls[-1][3]))

    def test_enable_rejects_missing_account_unselected_slot_and_three_wallets(self):
        for change in ({"account": None}, {"account": {"address": "invalid"}},
                       {"ai_slot_selected": False}, {"leaders": ["a", "b", "c"]}):
            with self.subTest(change=change):
                candidate = dict(profile(), **change)
                before = copy.deepcopy(candidate)
                with self.assertRaises(ValueError): self.modes.set_enabled(candidate, "trader", True)
                self.assertEqual(candidate, before)
                self.modes.set_enabled(candidate, "trader", False)
                self.assertFalse(candidate["ai_trader_enabled"])

    def test_invalid_mode_or_nonboolean_does_not_mutate_profile(self):
        for mode, enabled in (("live", True), ("trader", "false"), ("rescue", 1), ("rescue", None)):
            before = copy.deepcopy(self.profile)
            with self.assertRaises(ValueError): self.modes.set_enabled(self.profile, mode, enabled)
            self.assertEqual(self.profile, before)

    def test_disabled_or_unavailable_trader_reads_no_public_data_and_keeps_paper_positions(self):
        for change in ({"ai_trader_enabled": False}, {"account": None}, {"ai_slot_selected": False},
                       {"leaders": ["a", "b", "c"]}):
            result = self.modes.tick(1, dict(self.profile, **change), self.reader)
            self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.paper.calls, [])
        self.assertEqual(self.paper.positions, [{"coin": "BTC", "size": 1}])

    def test_tick_fixed_third_strict_market_metadata_and_closed_factors(self):
        before = copy.deepcopy(self.profile)
        result = self.modes.tick(1, self.profile, self.reader)
        self.assertEqual(result["status"], "OK")
        uid, account, budget, markets, stamp = self.paper.calls[0]
        self.assertEqual((uid, account, budget, stamp), (1, ACCOUNT, 100., NOW))
        self.assertEqual([m["coin"] for m in markets], ["BTC", "ETH"])
        self.assertEqual(markets[0]["sz_decimals"], 5)
        self.assertEqual(markets[1]["max_leverage"], 25)
        self.assertLess(markets[0]["candle_close_ms"], stamp)
        self.assertEqual(markets[0]["factors"]["asof_ms"], stamp)
        self.assertEqual(markets[1]["factors"]["funding_bps_hour"], -.1)
        self.assertEqual(self.profile, before)

    def test_candles_cached_only_within_closed_candle_but_quotes_refresh(self):
        self.modes.tick(1, self.profile, self.reader)
        self.reader.quotes["BTC"] = "220"
        self.modes.tick(1, self.profile, self.reader)
        requests = [v for v in self.reader.calls if isinstance(v, dict) and v["type"] == "candleSnapshot"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(self.paper.calls[-1][3][0]["price"], 220.)
        later = NOW+INTERVAL_MS
        self.reader.now = later
        with patch("core.ai_modes.time.time", return_value=later/1000):
            self.modes.tick(1, self.profile, self.reader)
        requests = [v for v in self.reader.calls if isinstance(v, dict) and v["type"] == "candleSnapshot"]
        self.assertEqual(len(requests), 4)

    def test_missing_funding_or_size_precision_never_invents_zero_or_default(self):
        del self.reader.contexts[0]["funding"]
        del self.reader.assets[1]["szDecimals"]
        result = self.modes.tick(1, self.profile, self.reader)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.paper.calls[-1][3], [])
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(self.modes.summary(1, self.profile)["trader"]["last_tick"]["status"], "UNAVAILABLE")

    def test_partial_invalid_market_keeps_other_market_and_reports_problem(self):
        self.reader.quotes["BTC"] = "NaN"
        result = self.modes.tick(1, self.profile, self.reader)
        self.assertEqual(result["status"], "PARTIAL_DATA")
        self.assertEqual([m["coin"] for m in self.paper.calls[-1][3]], ["ETH"])
        self.assertEqual(result["errors"][0]["coin"], "BTC")

    def test_gapped_stale_or_malformed_candles_not_forwarded_as_signal(self):
        bad = candles()
        del bad[20]
        self.reader.bad_candles = {"BTC": bad, "ETH": candles(NOW-INTERVAL_MS)}
        result = self.modes.tick(1, self.profile, self.reader)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.paper.calls[-1][3], [])
        self.assertEqual(self.modes._candles, {})

    def test_bad_balance_never_becomes_zero_budget_or_fake_profit(self):
        for bad in (float("nan"), float("inf"), -1., True, None):
            self.reader.cash = bad
            result = self.modes.tick(1, self.profile, self.reader)
            self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(self.paper.calls, [])

    def test_summary_does_not_show_previous_accounts_virtual_positions_without_current_account(self):
        self.profile["account"] = None
        result = self.modes.summary(1, self.profile)
        self.assertEqual(result["trader"]["paper"]["positions"], [])
        self.assertEqual(self.paper.summaries, [])
        self.assertTrue(result["rescue"]["enabled"])

    def test_observer_error_survives_new_instance_without_zeroing_paper_account(self):
        self.modes.tick(1, self.profile, self.reader)
        self.reader.cash = float("nan")
        self.modes.tick(1, self.profile, self.reader)
        reopened = AiModes(self.root.name, paper=self.paper)
        last = reopened.summary(1, self.profile)["trader"]["last_tick"]
        self.assertEqual(last["status"], "UNAVAILABLE")
        self.assertEqual(last["asof_ms"], NOW)
        self.assertEqual(last["reason"], "public_data_unavailable")
        self.assertEqual(len(self.paper.calls), 1, "Bad sizing data never becomes a new zero-budget snapshot")
        self.assertEqual(self.paper.calls[0][2], 100.)
        self.assertEqual(self.paper.positions, [{"coin": "BTC", "size": 1}])
        if os.name != "nt": self.assertEqual(os.stat(reopened.path).st_mode & 0o777, 0o600)

    def test_persisted_observer_status_isolated_by_user_and_account_and_pauses_visible(self):
        self.modes.tick(1, self.profile, self.reader)
        reopened = AiModes(self.root.name, paper=self.paper)
        self.assertEqual(reopened.summary(1, self.profile)["trader"]["last_tick"]["status"], "OK")
        self.assertIsNone(reopened.summary(2, self.profile)["trader"]["last_tick"])
        another = dict(self.profile, account={"address": "0x"+"d"*40})
        self.assertIsNone(reopened.summary(1, another)["trader"]["last_tick"])
        self.profile["ai_trader_enabled"] = False
        reopened.tick(1, self.profile, self.reader)
        self.assertEqual(self.modes.summary(1, self.profile)["trader"]["last_tick"]["status"], "PAUSED")

    def test_real_paper_module_accepts_adapter_schema_and_survives_restart(self):
        modes = AiModes(self.root.name)
        result = modes.tick(1, self.profile, self.reader)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["paper"]["mode"], "PAPER")
        self.assertEqual(result["paper"]["budget_usdc"], 100.)
        reopened = AiModes(self.root.name).summary(1, self.profile)["trader"]
        self.assertEqual(reopened["last_tick"]["status"], "OK")
        self.assertEqual(reopened["paper"]["budget_usdc"], 100.)
        self.assertEqual(reopened["paper"]["positions"], [], "Overbought test data must not invent a new signal")


if __name__ == "__main__": unittest.main()
