"""Client wiring without constructors, credentials or network connections."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from core.fill_history import HistoryIncomplete
from core.hyperliquid import HyperliquidReader
from integrations.hyperliquid import HyperliquidAccount
from tests.test_fill_history import Endpoint, fill


NOW = 1_900_000_000_000


class FakeInfo:
    def __init__(self, endpoint):
        self.endpoint = endpoint
    def post(self, path, payload):
        if path != "/info":
            raise AssertionError("Unexpected API path in offline test")
        return self.endpoint(payload)
    def user_fills_by_time(self, *args, **kwargs):
        raise AssertionError("Legacy second history request must not be used")


class FillClientTests(unittest.TestCase):
    def setUp(self):
        self.time = patch("time.time", return_value=NOW/1000)
        self.time.start()
        self.addCleanup(self.time.stop)
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.rows = [fill(1, NOW-1000), dict(fill(2, NOW-999, "xyz:INTC"), closedPnl="2"),
                     dict(fill(3, NOW-998, "@107"), closedPnl="1000")]

    def reader(self, rows=None):
        endpoint = Endpoint(deepcopy(self.rows if rows is None else rows))
        reader = HyperliquidReader.__new__(HyperliquidReader)
        reader._info = endpoint
        return reader, endpoint

    def account(self, rows=None):
        endpoint = Endpoint(deepcopy(self.rows if rows is None else rows))
        client = HyperliquidAccount.__new__(HyperliquidAccount)
        client.address = "offline-wallet"
        client.info = FakeInfo(endpoint)
        client.exchange = None
        return client, endpoint

    def test_reader_default_returns_all_perps_with_one_feed(self):
        reader, endpoint = self.reader()
        result = reader.fills_90d("offline-wallet")
        self.assertEqual([r["coin"] for r in result], ["BTC", "xyz:INTC"])
        self.assertEqual(len(endpoint.calls), 1)
        self.assertNotIn("dex", endpoint.calls[0])
        self.assertEqual(endpoint.calls[0]["endTime"], NOW)
        self.assertEqual(endpoint.calls[0]["startTime"], NOW-90*86400000)

    def test_reader_explicit_dex_is_filtered_locally(self):
        for dex, expected in (("", ["BTC"]), ("xyz", ["xyz:INTC"]), ("unknown", [])):
            reader, endpoint = self.reader()
            with self.subTest(dex=dex):
                self.assertEqual([r["coin"] for r in reader.fills_90d("offline-wallet", dex)], expected)
                self.assertEqual(len(endpoint.calls), 1)
                self.assertNotIn("dex", endpoint.calls[0])

    def test_account_default_returns_all_perps_once(self):
        client, endpoint = self.account()
        self.assertEqual([r["coin"] for r in client.fills_90d()], ["BTC", "xyz:INTC"])
        self.assertEqual(len(endpoint.calls), 1)
        self.assertFalse(endpoint.calls[0]["aggregateByTime"])

    def test_account_explicit_dex_does_not_repeat_feed(self):
        for dex, expected in (("", ["BTC"]), ("xyz", ["xyz:INTC"])):
            client, endpoint = self.account()
            with self.subTest(dex=dex):
                self.assertEqual([r["coin"] for r in client.fills_90d(dex)], expected)
                self.assertEqual(len(endpoint.calls), 1)
                self.assertNotIn("dex", endpoint.calls[0])

    def test_realized_pnl_deduplicates_xyz_and_excludes_spot_without_changing_fee_scope(self):
        client, endpoint = self.account(self.rows + deepcopy(self.rows))
        self.assertEqual(client.realized_pnl_since(NOW-3600000), 3.)
        self.assertEqual(len(endpoint.calls), 1)
        self.assertEqual(endpoint.calls[0]["startTime"], NOW-3600000)

    def test_history_read_error_propagates_instead_of_zero_pnl(self):
        client, _ = self.account()
        client.info = FakeInfo(lambda _: {"error": "unavailable"})
        with self.assertRaises(HistoryIncomplete): client.realized_pnl_since(NOW-1000)
        with self.assertRaises(HistoryIncomplete): client.fills_90d()
        reader, _ = self.reader()
        reader._info = lambda _: None
        with self.assertRaises(HistoryIncomplete): reader.fills_90d("offline-wallet")

    def test_saturated_single_millisecond_never_produces_understated_pnl(self):
        client, _ = self.account([fill(i, NOW) for i in range(2000)])
        with self.assertRaises(HistoryIncomplete): client.realized_pnl_since(NOW)

    def test_fill_schema_errors_fail_closed_for_both_clients(self):
        malformed = [dict(fill(1, NOW), fee="nan")]
        client, _ = self.account(malformed)
        reader, _ = self.reader(malformed)
        with self.assertRaises(HistoryIncomplete): client.fills_90d()
        with self.assertRaises(HistoryIncomplete): reader.fills_90d("offline-wallet")


if __name__ == "__main__":
    unittest.main()
