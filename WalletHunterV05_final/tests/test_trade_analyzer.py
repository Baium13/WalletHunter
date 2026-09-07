"""Descriptive accounting contracts, not trading strategy profitability tests."""
from copy import deepcopy
import math
import unittest

from core.trade_analyzer import AnalysisUnavailable, TradeAnalyzer


def fill(tid=1, *, pnl="0", fee="0", direction="Close Long", side="A", start="1", size="1", price="100", time=1000, coin="BTC"):
    return {"tid": tid, "oid": 100, "coin": coin, "time": time, "side": side, "dir": direction,
            "startPosition": start, "sz": size, "px": price, "closedPnl": pnl, "fee": fee, "feeToken": "USDC"}


class TradeAnalyzerTests(unittest.TestCase):
    def setUp(self): self.analyzer = TradeAnalyzer()

    def test_all_fees_including_opening_fees_reduce_net_and_cashflow_profit_factor(self):
        opening = fill(1, fee="1", direction="Open Long", side="B", start="0", price="100")
        closing = fill(2, pnl="20", fee="2", price="120", time=2000)
        report = self.analyzer.report([closing, opening])
        self.assertEqual(report.trades, 1)
        self.assertEqual(report.wins, 1)
        self.assertEqual(report.net_pnl, 17)
        self.assertEqual(report.gross_profit, 18)
        self.assertEqual(report.gross_loss, -1)
        self.assertEqual(report.profit_factor, 18)
        self.assertEqual(report.expectancy, 17)
        self.assertEqual(report.avg_win, 18)  # Closing-fill net, not a paired trade.
        self.assertEqual(report.max_drawdown, 1)
        self.assertIn("entry fees not allocated", report.methodology["win_rate"])

    def test_breakeven_zero_pnl_zero_fee_close_counts_in_denominator(self):
        report = self.analyzer.report([fill()])
        self.assertEqual(report.trades, 1)
        self.assertEqual(report.wins, 0)
        self.assertEqual(report.losses, 0)
        self.assertEqual(report.win_rate, 0)
        self.assertEqual(report.net_pnl, 0)
        self.assertEqual(report.methodology["breakeven_closing_fills"], 1)
        self.assertFalse(report.methodology["profit_factor_available"])

    def test_win_rate_does_not_drop_breakeven_closes(self):
        report = self.analyzer.report([fill(1, pnl="1"), fill(2, pnl="-1"), fill(3)])
        self.assertEqual(report.trades, 3)
        self.assertAlmostEqual(report.win_rate, 100/3)
        self.assertEqual(report.methodology["breakeven_closing_fills"], 1)

    def test_prefee_breakeven_is_loss_after_actual_fee(self):
        report = self.analyzer.report([fill(fee=".1")])
        self.assertEqual(report.trades, 1)
        self.assertEqual(report.losses, 1)
        self.assertEqual(report.net_pnl, -.1)
        self.assertEqual(report.methodology["breakeven_closing_fills"], 0)

    def test_small_gross_win_cannot_ignore_larger_execution_fee(self):
        report = self.analyzer.report([fill(pnl=".05", fee=".1")])
        self.assertEqual(report.wins, 0)
        self.assertEqual(report.losses, 1)
        self.assertEqual(report.net_pnl, -.05)

    def test_reversal_is_a_closing_execution_even_when_pnl_is_zero(self):
        for direction, side, before in (("Long > Short", "A", "1"), ("Short > Long", "B", "-1")):
            report = self.analyzer.report([fill(direction=direction, side=side, start=before, size="2")])
            self.assertEqual(report.trades, 1)
            self.assertEqual(report.methodology["breakeven_closing_fills"], 1)

    def test_start_position_and_side_can_identify_a_zero_pnl_close_without_dir(self):
        row = fill(); row.pop("dir")
        self.assertEqual(self.analyzer.report([row]).trades, 1)

    def test_ambiguous_zero_pnl_fill_without_direction_is_not_assumed_open(self):
        row = fill(); row.pop("dir"); row.pop("startPosition")
        with self.assertRaisesRegex(AnalysisUnavailable, "Zero-PnL"): self.analyzer.report([row])
        row = fill(); row.pop("dir"); row.pop("side")
        with self.assertRaisesRegex(AnalysisUnavailable, "Zero-PnL"): self.analyzer.report([row])

    def test_stable_id_duplicates_removed_once_even_if_number_format_differs(self):
        row = fill(pnl="1", fee=".1")
        same = dict(row, closedPnl="1.0", fee="0.10", sz="1.0000")
        report = self.analyzer.report([row, same])
        self.assertEqual(report.trades, 1)
        self.assertEqual(report.net_pnl, .9)
        self.assertEqual(report.methodology["duplicate_fills_removed"], 1)

    def test_conflicting_stable_id_fails_instead_of_counting_twice(self):
        row = fill(pnl="1")
        with self.assertRaisesRegex(AnalysisUnavailable, "Conflicting"):
            self.analyzer.report([row, dict(row, fee="1")])

    def test_partial_fills_on_same_order_are_separate_executions(self):
        rows = [fill(1, pnl="1", start="2"), fill(2, pnl="1", start="1")]
        report = self.analyzer.report(rows)
        self.assertEqual(report.trades, 2)
        self.assertEqual(report.net_pnl, 2)
        self.assertEqual(report.methodology["duplicate_fills_removed"], 0)

    def test_missing_stable_ids_are_reported_and_ambiguous_duplicates_rejected(self):
        row = fill(); row.pop("tid")
        report = self.analyzer.report([row])
        self.assertEqual(report.methodology["fills_without_stable_id"], 1)
        with self.assertRaisesRegex(AnalysisUnavailable, "Ambiguous duplicate"):
            self.analyzer.report([row, deepcopy(row)])

    def test_opening_fee_alone_is_real_negative_cashflow_without_fake_closed_trade(self):
        report = self.analyzer.report([fill(fee=".1", direction="Open Short", side="A", start="0")])
        self.assertEqual(report.trades, 0)
        self.assertEqual(report.net_pnl, -.1)
        self.assertEqual(report.max_drawdown, .1)
        self.assertEqual(report.rating, 0)

    def test_maker_rebate_is_positive_cashflow_and_builder_fee_not_added_twice(self):
        report = self.analyzer.report([dict(fill(fee="-.1", direction="Open Short", side="A", start="0"), builderFee="0")])
        self.assertEqual(report.net_pnl, .1)
        report = self.analyzer.report([dict(fill(pnl="1", fee=".1"), builderFee=".02")])
        self.assertEqual(report.net_pnl, .9)

    def test_missing_fee_or_realized_pnl_is_never_assumed_zero(self):
        for field in ("fee", "closedPnl"):
            row = fill(); row.pop(field)
            with self.subTest(field=field), self.assertRaises(AnalysisUnavailable): self.analyzer.report([row])

    def test_fee_token_is_required_even_in_default_zero_fee_fixtures(self):
        row = fill(); row.pop("feeToken")
        with self.assertRaisesRegex(AnalysisUnavailable, "feeToken"): self.analyzer.report([row])
        report = self.analyzer.report([row], allow_missing_fee_token_for_zero_fee=True)
        self.assertEqual(report.net_pnl, 0)
        self.assertEqual(report.methodology["zero_fee_missing_token_explicitly_allowed"], 1)

    def test_unknown_nonzero_token_cannot_be_assumed_usdc_under_fixture_policy(self):
        for fee, token in ((".1", None), (".1", "HYPE"), ("0", "HYPE")):
            row = fill(fee=fee); row["feeToken"] = token
            with self.subTest(fee=fee, token=token), self.assertRaises(AnalysisUnavailable):
                self.analyzer.report([row], allow_missing_fee_token_for_zero_fee=True)

    def test_nonfinite_negative_size_fractional_time_and_booleans_rejected(self):
        changes = ({"closedPnl": "NaN"}, {"fee": "Infinity"}, {"fee": True}, {"px": "0"},
                   {"sz": "-1"}, {"sz": None}, {"time": -1}, {"time": 1.5}, {"time": True})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(AnalysisUnavailable): self.analyzer.report([dict(fill(), **change)])
        with self.assertRaises(AnalysisUnavailable): self.analyzer.report([fill(size="1e308", price="1e308")])

    def test_nonfinite_reference_balance_cannot_generate_a_rating(self):
        for value in (float("nan"), float("inf"), -1, True):
            with self.subTest(value=value), self.assertRaises(AnalysisUnavailable): self.analyzer.report([fill()], value)

    def test_direction_and_realized_pnl_must_match_fill_semantics(self):
        rows = [fill(direction="Open Long", side="B", start="0", pnl="1"),
                fill(direction="Close Long", side="B", start="-1"),
                fill(direction="Long > Short", side="A", start="1", size=".5"),
                fill(direction="Open Long", side="B", start="-2")]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(AnalysisUnavailable): self.analyzer.report([row])

    def test_unavailable_roi_sharpe_and_equity_dd_are_none_not_invented_percentages(self):
        first = self.analyzer.report([fill(pnl="-10")], reference_balance=100)
        second = self.analyzer.report([fill(pnl="-10")], reference_balance=1000000)
        self.assertEqual(first.max_drawdown, second.max_drawdown)
        for name in ("max_drawdown_pct", "avg_roi", "median_roi", "sharpe", "risk_score"):
            self.assertIsNone(getattr(first, name))
        self.assertFalse(first.methodology["reference_balance_used"])

    def test_zero_net_days_remain_active_and_cashflows_use_utc_days(self):
        report = self.analyzer.report([fill(1, pnl="1", time=1000), fill(2, pnl="-1", time=2000), fill(3, pnl="1", time=86400001)])
        self.assertEqual(report.active_days, 2)
        self.assertEqual(report.positive_days, 1)
        self.assertEqual(report.consistency_score, 50)

    def test_simultaneous_fills_do_not_invent_intrablock_drawdown(self):
        report = self.analyzer.report([fill(1, pnl="10"), fill(2, pnl="-10")])
        self.assertEqual(report.max_drawdown, 0)
        self.assertIn("grouped by millisecond", report.methodology["max_drawdown"])

    def test_coin_concentration_uses_gross_execution_volume_not_cancelled_pnl(self):
        report = self.analyzer.report([fill(1, pnl="10"), fill(2, pnl="-10"), fill(3, pnl="1", coin="ETH")])
        self.assertAlmostEqual(report.top_coin_share, 200/300*100)

    def test_methodology_is_defensive_and_explicitly_not_copy_admission(self):
        report = self.analyzer.report([fill(i, pnl="1", time=i) for i in range(50)])
        description = report.methodology
        description["funding_included"] = True
        self.assertFalse(report.methodology["funding_included"])
        self.assertIn("НЕ ДОПУСК", report.recommendation)
        self.assertIn("not a probability", report.methodology["rating"])

    def test_empty_report_has_no_positive_rating_or_manufactured_return_series(self):
        report = self.analyzer.report([])
        self.assertEqual(report.trades, 0)
        self.assertEqual(report.net_pnl, 0)
        self.assertEqual(report.rating, 0)
        self.assertIsNone(report.sharpe)
        self.assertIn("МАЛО", report.recommendation)


if __name__ == "__main__": unittest.main()
