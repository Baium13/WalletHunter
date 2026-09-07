import copy
import unittest
from types import SimpleNamespace

from core.capital_snapshot import (
    UnsupportedCapitalMode, available_margin_usdc, read_capital_snapshot,
    strict_spot_usdc,
)
from core.hyperliquid import HyperliquidReader
from integrations.hyperliquid import HyperliquidAccount


def spot(total="100", hold="30", maintenance="60"):
    value = {"balances": [{"coin": "USDC", "token": 0, "total": total, "hold": hold}]}
    if maintenance is not None:
        value["tokenToAvailableAfterMaintenance"] = [[0, maintenance]]
    return value


class Feed:
    def __init__(self, mode="unifiedAccount", spot_state=None):
        self.mode = mode
        self.spot = spot() if spot_state is None else spot_state
        self.calls = []
        self.perps = {
            "": {"marginSummary": {"accountValue": "8.10"}, "withdrawable": "2.5"},
            "xyz": {"marginSummary": {"accountValue": "12.2"}, "withdrawable": "4.5"},
        }

    def __call__(self, payload):
        self.calls.append(copy.deepcopy(payload))
        kind = payload["type"]
        if kind == "userAbstraction":
            return self.mode
        if kind == "spotClearinghouseState":
            return copy.deepcopy(self.spot)
        if kind == "clearinghouseState":
            return copy.deepcopy(self.perps[payload["dex"]])
        raise AssertionError(kind)


class CapitalSnapshotTests(unittest.TestCase):
    def test_unified_counts_spot_total_once_without_dex_or_pnl(self):
        feed = Feed(spot_state=spot("91.043944", "9.316084", "79"))
        result = read_capital_snapshot(feed, "wallet")
        self.assertEqual(result.sizing_base_usdc, 91.043944)
        self.assertEqual(result.basis, "unified_usdc_total")
        self.assertEqual([row["type"] for row in feed.calls], ["userAbstraction", "spotClearinghouseState"])

    def test_holds_change_capacity_not_sizing_base(self):
        for hold, expected in [("0", 60), ("30", 60), ("90", 10), ("100", 0)]:
            feed = Feed(spot_state=spot("100", hold))
            self.assertEqual(read_capital_snapshot(feed, "wallet").sizing_base_usdc, 100)
            self.assertEqual(available_margin_usdc(feed, "wallet"), expected)

    def test_capacity_uses_both_limits_and_clamps_negative(self):
        for state, expected in [(spot("100", "30", "60"), 60), (spot("100", "30", "90"), 70),
                                (spot("100", "30", None), 70), (spot("100", "30", "-1"), 0),
                                (spot("-1", "0", "5"), 0)]:
            self.assertEqual(available_margin_usdc(Feed(spot_state=state), "wallet", "xyz"), expected)

    def test_standard_excludes_spot_and_keeps_dex_capacity_separate(self):
        feed = Feed("disabled", spot("9999999", "0"))
        result = read_capital_snapshot(feed, "wallet")
        self.assertAlmostEqual(result.sizing_base_usdc, 20.3)
        self.assertEqual(result.basis, "supported_perp_equity")
        self.assertFalse(any(row["type"] == "spotClearinghouseState" for row in feed.calls))
        self.assertEqual(available_margin_usdc(feed, "wallet"), 2.5)
        self.assertEqual(available_margin_usdc(feed, "wallet", "xyz"), 4.5)

    def test_standard_zero_does_not_fall_back_to_raw_usd(self):
        feed = Feed("disabled")
        feed.perps[""]["marginSummary"] = {"accountValue": "0", "totalRawUsd": "500"}
        feed.perps["xyz"]["marginSummary"]["accountValue"] = "0"
        self.assertEqual(read_capital_snapshot(feed, "wallet").sizing_base_usdc, 0)

    def test_unsupported_modes_fail_explicitly_before_balance_query(self):
        for mode in ["portfolioMargin", "dexAbstraction", "default", "other", None, {}, []]:
            feed = Feed(mode)
            for operation in (read_capital_snapshot, available_margin_usdc):
                with self.assertRaises(UnsupportedCapitalMode) as caught:
                    operation(feed, "wallet")
                self.assertEqual(caught.exception.mode, mode)
            self.assertTrue(all(row["type"] == "userAbstraction" for row in feed.calls))

    def test_valid_no_usdc_is_zero(self):
        for state in [{"balances": []}, {"balances": [{"coin": "HYPE", "token": 150,
                                                      "total": "5", "hold": "1"}]}]:
            self.assertEqual(strict_spot_usdc(state).total, 0)
            self.assertEqual(strict_spot_usdc(state).available, 0)

    def test_malformed_balances_are_never_silently_skipped(self):
        bad = [None, {}, {"balances": None}, {"balances": [None]}, {"balances": [{}]}]
        for field in ("total", "hold"):
            for value in [None, "nan", "inf", "-inf", "", True, {}, []]:
                state = spot()
                state["balances"][0][field] = value
                bad.append(state)
        state = spot(); state["balances"].append(copy.deepcopy(state["balances"][0])); bad.append(state)
        state = spot(); state["balances"][0]["token"] = 5; bad.append(state)
        state = spot(); state["balances"][0]["hold"] = "-1"; bad.append(state)
        for state in bad:
            with self.subTest(state=state), self.assertRaises(ValueError):
                strict_spot_usdc(state)

    def test_malformed_maintenance_is_rejected(self):
        for value in [None, {}, [[0]], [[0, "nan"]], [[0, "5"], [0, "6"]], [[False, "5"]], []]:
            state = spot()
            state["tokenToAvailableAfterMaintenance"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                strict_spot_usdc(state)

    def test_standard_malformed_equity_or_capacity_rejected(self):
        for value in [None, "nan", "inf", True, {}]:
            feed = Feed("disabled")
            feed.perps[""]["marginSummary"]["accountValue"] = value
            with self.assertRaises(ValueError): read_capital_snapshot(feed, "wallet")
            feed.perps[""]["withdrawable"] = value
            with self.assertRaises(ValueError): available_margin_usdc(feed, "wallet")

    def test_clients_share_balance_semantics_and_capacity_is_fresh(self):
        feed = Feed()
        reader = HyperliquidReader.__new__(HyperliquidReader)
        reader._info = feed
        account = HyperliquidAccount.__new__(HyperliquidAccount)
        account.address = "wallet"
        account.info = SimpleNamespace(post=lambda _path, payload: feed(payload))
        self.assertEqual(reader.balance("wallet"), 100)
        self.assertEqual(account.balance(), 100)
        self.assertEqual(account.available_margin(), 60)
        feed.spot = spot("100", "95", "4")
        self.assertEqual(account.balance(), 100)
        self.assertEqual(account.available_margin(), 4)
        feed.mode = "disabled"
        self.assertEqual(account.available_margin("xyz"), 4.5)
        self.assertEqual(reader.spot_usdc_balance("wallet"), 4)
        self.assertEqual(account.spot_usdc_balance(), 4)

    def test_invalid_dex_is_rejected_before_network(self):
        feed = Feed()
        with self.assertRaises(ValueError): available_margin_usdc(feed, "wallet", "cash")
        self.assertEqual(feed.calls, [])


if __name__ == "__main__":
    unittest.main()
