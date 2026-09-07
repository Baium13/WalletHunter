"""Pure raw-history reconstruction tests: no keys, network or trading client."""
from copy import deepcopy
from decimal import Decimal
import unittest

from core.position_history import HistoryUnavailable, reconstruct_position_episode, read_position_episode, HOUR_MS


START = 1_800_000_000_000  # Exact hour boundary.
OPEN = START + 1000
END = START + 2 * HOUR_MS + 1000


def fill(*, coin="BTC", time=OPEN, start="0", size="0.00058", side="A", price="79477", pnl="0", fee="0.019913", tid=1, direction=None):
    row = {"coin": coin, "time": time, "startPosition": start, "sz": size, "side": side,
           "px": price, "closedPnl": pnl, "fee": fee, "feeToken": "USDC", "oid": tid + 100, "tid": tid}
    if direction is not None: row["dir"] = direction
    return row


def settlement(hour, amount="0.01", coin="BTC", size="-0.00058"):
    return {"time": START + hour * HOUR_MS + 100, "delta": {
        "type": "funding", "coin": coin, "usdc": amount, "szi": size, "fundingRate": ".0000125"}}


def metadata(fills, funding, end=END):
    return {"start_ms": START, "end_ms": end, "position_asof_ms": end,
            "fills": {"complete": True, "truncated": False, "aggregate_by_time": False,
                      "row_count": len(fills), "row_cap": 2000, "block_cap": 500},
            "funding": {"complete": True, "truncated": False, "row_count": len(funding), "row_cap": 500, "block_cap": 500}}


class PositionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.position = {"coin": "BTC", "dex": "", "side": "SHORT", "size": .00058,
                         "entry_price": 79477., "leverage": 40., "margin_used": 1.16}
        self.fills = [fill(direction="Open Short")]
        self.funding = [settlement(1, "0.013"), settlement(2, "0.013348")]
        self.margin = {"basis": "pre_intervention_exchange_margin", "margin_usdc": "1.16",
                       "asof_ms": END, "evidence": "Explicit first pre-intervention exchange snapshot"}

    def reconstruct(self, **kwargs):
        return reconstruct_position_episode(self.position, self.fills, self.funding,
            completeness=kwargs.pop("completeness", metadata(self.fills, self.funding)), **kwargs)

    def test_unchanged_current_episode_accounts_actual_costs_without_inferred_leverage(self):
        result = self.reconstruct(frozen_pre_intervention_margin=self.margin, intervention_history=[])
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["episode"]["remaining_signed_size"], "-0.00058")
        self.assertIsNone(result["episode"]["historical_leverage"])
        self.assertEqual(result["accounting"]["realized_closed_pnl_usdc"], "0")
        self.assertEqual(result["accounting"]["fee_net_usdc"], "0.019913")
        self.assertEqual(result["accounting"]["funding_cashflow_usdc"], "0.026348")
        self.assertEqual(result["accounting"]["historical_net_usdc"], "0.006435")
        risk = result["risk_basis"]
        self.assertEqual(risk["basis"], "original_pre_intervention")
        self.assertEqual(risk["risk_capital_usdc"], "1.16")
        self.assertEqual(risk["realized_pnl_usdc"], "0.026348")
        self.assertEqual(risk["paid_costs_usdc"], "0.019913")
        self.assertIn("not_historical_entry_margin", risk["capital_origin"])

    def test_funding_debits_and_maker_rebates_are_not_clamped_or_double_counted(self):
        self.fills[0]["fee"] = "-0.001"
        self.funding = [settlement(1, "0.01"), settlement(2, "-0.02")]
        result = self.reconstruct(frozen_pre_intervention_margin=self.margin, intervention_history=[])
        self.assertEqual(result["accounting"]["historical_net_usdc"], "-0.009")
        self.assertEqual(result["risk_basis"]["realized_pnl_usdc"], "0.011")
        self.assertEqual(result["risk_basis"]["paid_costs_usdc"], "0.02")

    def test_builder_fee_is_already_in_fill_fee(self):
        self.fills[0]["builderFee"] = "0.005"
        self.assertEqual(self.reconstruct()["accounting"]["fee_net_usdc"], "0.019913")

    def test_missing_metadata_or_incomplete_interval_never_becomes_known_zero(self):
        for change in (None, {}, {**metadata(self.fills, self.funding), "funding": {"complete": False}}):
            with self.subTest(change=change), self.assertRaises(HistoryUnavailable):
                self.reconstruct(completeness=change)

    def test_metadata_row_count_truncation_aggregation_and_caps_are_enforced(self):
        for kind, key, value in (("fills", "row_count", 0), ("fills", "truncated", True),
                                 ("fills", "aggregate_by_time", True), ("fills", "row_cap", 1),
                                 ("fills", "block_cap", 1), ("funding", "row_cap", 2)):
            proof = metadata(self.fills, self.funding); proof[kind][key] = value
            with self.subTest(kind=kind, key=key), self.assertRaises(HistoryUnavailable): self.reconstruct(completeness=proof)

    def test_missing_fee_pnl_or_unknown_fee_token_rejected(self):
        original = deepcopy(self.fills[0])
        for field in ("fee", "feeToken", "closedPnl"):
            self.fills[0] = deepcopy(original); self.fills[0].pop(field)
            with self.subTest(field=field), self.assertRaises(HistoryUnavailable): self.reconstruct()
        self.fills[0] = deepcopy(original); self.fills[0]["feeToken"] = "HYPE"
        with self.assertRaisesRegex(HistoryUnavailable, "non_usdc"): self.reconstruct()

    def test_nonfinite_and_boolean_values_rejected(self):
        for value in ("NaN", "Infinity", True, None):
            self.fills[0]["fee"] = value
            with self.subTest(value=value), self.assertRaises(HistoryUnavailable): self.reconstruct()

    def test_gap_or_duplicate_funding_hour_is_unavailable(self):
        self.funding.pop()
        with self.assertRaisesRegex(HistoryUnavailable, "coverage_incomplete"): self.reconstruct()
        self.funding = [settlement(1), settlement(2), {**settlement(2), "time": END - 100}]
        with self.assertRaisesRegex(HistoryUnavailable, "duplicate_funding_hour"): self.reconstruct()

    def test_funding_for_another_quantity_is_unavailable(self):
        self.funding[0]["delta"]["szi"] = "-0.0006"
        with self.assertRaisesRegex(HistoryUnavailable, "funding_position_size_mismatch"): self.reconstruct()

    def test_missing_funding_cashflow_is_not_zero(self):
        self.funding[0]["delta"].pop("usdc")
        with self.assertRaisesRegex(HistoryUnavailable, "funding_usdc"): self.reconstruct()

    def test_no_due_settlements_can_explicitly_total_zero(self):
        self.funding = []
        result = self.reconstruct(completeness=metadata(self.fills, [], OPEN + 100))
        self.assertEqual(result["accounting"]["funding_cashflow_usdc"], "0")
        self.assertEqual(result["completeness"]["funding"]["expected_episode_hours"], 0)

    def test_current_quantity_entry_or_direction_mismatch_rejected(self):
        original = deepcopy(self.position)
        for key, value in (("size", .00057), ("entry_price", 79400), ("side", "LONG")):
            self.position = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(HistoryUnavailable): self.reconstruct()

    def test_opening_outside_history_is_not_invented(self):
        self.fills[0]["startPosition"] = "-0.0001"
        with self.assertRaisesRegex(HistoryUnavailable, "opening_from_flat"): self.reconstruct()

    def test_known_zero_origin_selects_latest_episode_only(self):
        self.fills.insert(0, fill(time=OPEN - 800, size="1", price="100", tid=9))
        self.fills.insert(1, fill(time=OPEN - 500, start="-1", size="1", side="B", price="90", pnl="10", fee="1", tid=10))
        result = self.reconstruct()
        self.assertEqual(result["episode"]["fill_count"], 1)
        self.assertEqual(result["accounting"]["realized_closed_pnl_usdc"], "0")

    def test_duplicate_fill_cannot_double_count_costs(self):
        self.fills.append(deepcopy(self.fills[0]))
        with self.assertRaisesRegex(HistoryUnavailable, "duplicate_fills_record"): self.reconstruct()

    def test_add_reduce_chain_reconstructs_weighted_entry_and_realized_loss(self):
        self.position.update(size=1.5, side="LONG", entry_price=110)
        self.fills = [fill(size="1", side="B", price="100", fee=".01"),
                      fill(time=OPEN + 100, start="1", size="1", side="B", price="120", fee=".02", tid=2),
                      fill(time=OPEN + 200, start="2", size=".5", side="A", price="90", pnl="-10", fee=".03", tid=3)]
        self.funding = [settlement(1, "-.01", size="1.5"), settlement(2, ".005", size="1.5")]
        result = self.reconstruct(frozen_pre_intervention_margin=self.margin, intervention_history=[])
        self.assertEqual(Decimal(result["episode"]["entry_price"]), Decimal(110))
        self.assertEqual(Decimal(result["accounting"]["historical_net_usdc"]), Decimal("-10.065"))
        self.assertIsNone(result["risk_basis"])
        self.assertEqual(result["risk_basis_unavailable_reason"], "position_changed_since_flat_opening")

    def test_same_timestamp_chain_can_be_ordered_by_start_position(self):
        self.position.update(size=2, side="LONG", entry_price=110)
        self.fills = [fill(start="1", size="1", side="B", price="120", tid=2), fill(size="1", side="B", price="100")]
        self.funding = [settlement(1, size="2"), settlement(2, size="2")]
        self.assertEqual(self.reconstruct()["episode"]["fill_count"], 2)

    def test_discontinuous_fill_chain_cannot_match_by_accidental_net_size(self):
        self.fills.append(fill(time=OPEN + 100, start="-.1", size=".09942", side="B", pnl="0", tid=2))
        with self.assertRaisesRegex(HistoryUnavailable, "continuity"): self.reconstruct()

    def test_reversal_preserves_pre_reversal_realized_pnl_but_has_no_risk_basis(self):
        self.position.update(size=.5, side="LONG", entry_price=80)
        self.fills = [fill(size="1", price="100"),
                      fill(time=OPEN + 100, start="-1", size="1.5", side="B", price="80", pnl="20", tid=2, direction="Short > Long")]
        self.funding = [settlement(1, size=".5"), settlement(2, size=".5")]
        result = self.reconstruct(frozen_pre_intervention_margin=self.margin, intervention_history=[])
        self.assertEqual(result["episode"]["reversal_count"], 1)
        self.assertEqual(result["accounting"]["realized_closed_pnl_usdc"], "20")
        self.assertIsNone(result["risk_basis"])

    def test_direction_tag_must_agree_with_signed_transition(self):
        self.fills[0]["dir"] = "Open Long"
        with self.assertRaisesRegex(HistoryUnavailable, "direction_mismatch"): self.reconstruct()

    def test_canonical_xyz_alias_and_conflicting_dex(self):
        self.position.update(coin="INTC", dex="xyz")
        self.fills[0]["coin"] = "xyz:INTC"
        for row in self.funding: row["delta"]["coin"] = "xyz:INTC"
        self.assertEqual(self.reconstruct()["market"], "xyz:INTC|xyz")
        self.position.update(coin="xyz:INTC", dex="abc")
        with self.assertRaisesRegex(HistoryUnavailable, "conflicting_market_dex"): self.reconstruct()

    def test_risk_basis_requires_explicit_no_intervention_attestation(self):
        self.assertIsNone(self.reconstruct()["risk_basis"])
        self.assertEqual(self.reconstruct(frozen_pre_intervention_margin=self.margin)["risk_basis_unavailable_reason"], "intervention_history_unknown")
        self.assertIsNone(self.reconstruct(frozen_pre_intervention_margin=self.margin, intervention_history=[{"action": "ADD_MARGIN"}])["risk_basis"])

    def test_margin_must_be_labelled_evidenced_positive_and_within_episode(self):
        for margin in (1.16, {**self.margin, "basis": "historical"}, {**self.margin, "evidence": ""},
                       {**self.margin, "margin_usdc": 0}, {**self.margin, "asof_ms": END + 1}):
            with self.subTest(margin=margin), self.assertRaises(HistoryUnavailable):
                self.reconstruct(frozen_pre_intervention_margin=margin, intervention_history=[])

    def test_current_snapshot_time_must_match_history_query(self):
        proof = metadata(self.fills, self.funding); proof["position_asof_ms"] -= 1
        with self.assertRaisesRegex(HistoryUnavailable, "time_mismatch"): self.reconstruct(completeness=proof)

    def test_helper_only_uses_two_public_read_requests(self):
        calls = []
        outer = self
        class Reader:
            def _info(self, request):
                calls.append(request)
                return deepcopy(outer.fills if request["type"] == "userFillsByTime" else outer.funding)
        result = read_position_episode(Reader(), "0x" + "1" * 40, self.position, now_ms=END, position_asof_ms=END,
                                       frozen_pre_intervention_margin=self.margin, intervention_history=[])
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual([row["type"] for row in calls], ["userFillsByTime", "userFunding"])
        self.assertEqual(calls[0]["startTime"], END - 7 * 86_400_000)
        self.assertFalse(calls[0]["aggregateByTime"])

    def test_helper_does_not_claim_saturated_funding_is_complete(self):
        outer = self
        class Reader:
            def _info(self, request):
                return outer.fills if request["type"] == "userFillsByTime" else [settlement(1)] * 500
        with self.assertRaisesRegex(HistoryUnavailable, "funding_response_at_cap"):
            read_position_episode(Reader(), "test-user", self.position, now_ms=END, position_asof_ms=END)


if __name__ == "__main__": unittest.main()
