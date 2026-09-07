from copy import deepcopy
import unittest

from core.fill_history import HistoryIncomplete, fetch_fills, filter_perp_fills


def fill(index, timestamp=None, coin="BTC"):
    return {"coin": coin, "time": index if timestamp is None else timestamp,
            "tid": index, "oid": index//2, "hash": "0x" + "0"*64,
            "px": "100", "sz": "1", "side": "B", "closedPnl": "1", "fee": "0.01",
            "startPosition": str(index), "feeToken": "USDC", "dir": "Open Long", "crossed": True}


class Endpoint:
    def __init__(self, rows, *, latest=False, retention=False):
        self.rows, self.calls, self.latest, self.retention = rows, [], latest, retention
    def __call__(self, payload):
        self.calls.append(deepcopy(payload))
        rows = self.rows[-10000:] if self.retention else self.rows
        selected = [r for r in rows if payload["startTime"] <= r["time"] <= payload["endTime"]]
        return deepcopy(selected[-2000:] if self.latest else selected[:2000])


class FillHistoryTests(unittest.TestCase):
    def fetch(self, endpoint, start=0, end=20000, **kwargs):
        return fetch_fills(endpoint, "offline-wallet", start, end, now_ms=end, **kwargs)

    def test_uncapped_history_is_sorted_and_never_queries_dex_twice(self):
        endpoint = Endpoint([fill(3, coin="xyz:INTC"), fill(1), fill(2, coin="@107")])
        rows = self.fetch(endpoint)
        self.assertEqual([r["tid"] for r in rows], [1, 2, 3])
        self.assertEqual(len(endpoint.calls), 1)
        self.assertNotIn("dex", endpoint.calls[0])
        self.assertFalse(endpoint.calls[0]["aggregateByTime"])

    def test_saturated_windows_are_split_without_skipping_boundary_fills(self):
        rows = [fill(i, 999 if i<1700 else 1000) for i in range(3500)]
        for latest in (False, True):
            endpoint = Endpoint(rows, latest=latest)
            result = self.fetch(endpoint, end=2000)
            self.assertEqual(len(result), 3500)
            self.assertEqual({r["tid"] for r in result}, set(range(3500)))
            self.assertLessEqual(len(endpoint.calls), 32)

    def test_identical_duplicate_response_fills_are_counted_once(self):
        values = [fill(1), fill(2)]
        result = self.fetch(Endpoint(values+deepcopy(values)))
        self.assertEqual(len(result), 2)

    def test_numeric_formatting_does_not_defeat_deduplication(self):
        first = fill(1)
        duplicate = dict(first, px="100.0", sz=1.0, fee="0.0100")
        self.assertEqual(len(self.fetch(Endpoint([first, duplicate]))), 1)

    def test_conflicting_stable_fill_id_is_not_counted_as_two_fills(self):
        first = fill(1)
        with self.assertRaisesRegex(HistoryIncomplete, "Conflicting"):
            self.fetch(Endpoint([first, dict(first, closedPnl="2")]))

    def test_partial_fills_with_same_order_hash_and_timestamp_remain_distinct(self):
        a, b = fill(1, 10), fill(2, 10)
        b.update(oid=a["oid"], hash=a["hash"])
        self.assertEqual(len(self.fetch(Endpoint([a, b]))), 2)

    def test_same_millisecond_saturation_fails_closed(self):
        endpoint = Endpoint([fill(i, 50) for i in range(2001)])
        with self.assertRaisesRegex(HistoryIncomplete, "millisecond"):
            self.fetch(endpoint, start=50, end=50)

    def test_exactly_2000_same_millisecond_is_still_ambiguous(self):
        with self.assertRaises(HistoryIncomplete):
            self.fetch(Endpoint([fill(i, 10) for i in range(2000)]), start=10, end=10)

    def test_retention_boundary_never_claims_complete_90_days(self):
        endpoint = Endpoint([fill(i) for i in range(15000)], retention=True)
        with self.assertRaisesRegex(HistoryIncomplete, "retention"):
            self.fetch(endpoint)

    def test_exactly_10000_cannot_prove_no_older_retained_loss(self):
        with self.assertRaisesRegex(HistoryIncomplete, "retention"):
            self.fetch(Endpoint([fill(i) for i in range(10000)]))

    def test_9999_recent_fills_below_retention_boundary_can_be_complete(self):
        endpoint = Endpoint([fill(i) for i in range(9999)])
        self.assertEqual(len(self.fetch(endpoint)), 9999)
        self.assertLessEqual(len(endpoint.calls), 32)

    def test_request_budget_fails_instead_of_returning_partial_rows(self):
        endpoint = Endpoint([fill(i) for i in range(4000)])
        with self.assertRaisesRegex(HistoryIncomplete, "budget"):
            self.fetch(endpoint, max_requests=1)
        self.assertEqual(len(endpoint.calls), 1)

    def test_noncurrent_historical_end_is_rejected_before_api_request(self):
        endpoint = Endpoint([])
        with self.assertRaisesRegex(HistoryIncomplete, "non-current"):
            fetch_fills(endpoint, "offline", 0, 100, now_ms=100000)
        self.assertEqual(endpoint.calls, [])

    def test_endpoint_failure_cannot_masquerade_as_empty_wallet(self):
        def failed(payload):
            raise RuntimeError("offline failure")
        with self.assertRaises(HistoryIncomplete): self.fetch(failed)
        with self.assertRaises(HistoryIncomplete): self.fetch(lambda _: {"error": "unavailable"})

    def test_nonfinite_or_incomplete_fill_fields_fail_closed(self):
        for change in ({"px": "nan"}, {"sz": "inf"}, {"px": "1e9999"}, {"fee": None}, {"time": -1},
                       {"side": "?"}, {"closedPnl": float("nan")}):
            record = dict(fill(1), **change)
            # Direct callable deliberately returns malformed out-of-window rows.
            with self.subTest(change=change), self.assertRaises(HistoryIncomplete):
                self.fetch(lambda _: [record])

    def test_out_of_window_fills_are_not_silently_accepted(self):
        with self.assertRaises(HistoryIncomplete):
            self.fetch(lambda _: [fill(1, 10)], start=20)

    def test_zero_hash_without_stable_trade_id_is_not_deduplicated_by_guess(self):
        record = fill(1)
        record.pop("tid")
        with self.assertRaisesRegex(HistoryIncomplete, "identity"):
            self.fetch(Endpoint([record]))

    def test_signed_funding_or_fee_rebates_are_not_rejected(self):
        record = dict(fill(1), fee="-0.01", closedPnl="-2.5")
        self.assertEqual(self.fetch(Endpoint([record]))[0]["fee"], "-0.01")

    def test_perp_and_dex_selection_is_local_and_excludes_spot(self):
        rows = [fill(1), fill(2, coin="xyz:INTC"), fill(3, coin="@107"), fill(4, coin="PURR/USDC")]
        self.assertEqual([r["coin"] for r in filter_perp_fills(rows)], ["BTC", "xyz:INTC"])
        self.assertEqual([r["coin"] for r in filter_perp_fills(rows, "")], ["BTC"])
        self.assertEqual([r["coin"] for r in filter_perp_fills(rows, "xyz")], ["xyz:INTC"])


if __name__ == "__main__":
    unittest.main()
