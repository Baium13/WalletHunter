"""Exact user-confirmed IOC adapter tests. No network or real SDK signing."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from integrations.hyperliquid import HyperliquidAccount


ACCOUNT = "0x" + "a" * 40
CLOID = "0x" + "12ab" * 8


class FakeInfo:
    def __init__(self, calls):
        self.calls = calls
        self.metadata = {"universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
        ]}
        self.active = {"user": ACCOUNT, "coin": "BTC", "leverage": {"type": "cross", "value": 20}}
        self.orders = []
        self.query = {"status": "unknownOid"}
        self.fail = None

    def meta(self, dex=""):
        self.calls.append(("meta", dex))
        if self.fail == "meta": raise TimeoutError("metadata timeout")
        return deepcopy(self.metadata)

    def post(self, path, payload):
        self.calls.append(("active", path, deepcopy(payload)))
        assert path == "/info" and payload["type"] == "activeAssetData"
        if self.fail == "active": raise TimeoutError("snapshot timeout")
        return deepcopy(self.active)

    def frontend_open_orders(self, address, dex=""):
        self.calls.append(("orders", address, dex))
        return deepcopy(self.orders)

    def query_order_by_cloid(self, address, cloid):
        self.calls.append(("query", address, cloid.to_raw()))
        return deepcopy(self.query)


class FakeExchange:
    def __init__(self, calls):
        self.calls = calls
        self.ack = {"status": "ok", "response": {"type": "default"}}
        self.result = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"totalSz": "0.00123", "avgPx": "60000", "oid": 123}},
        ]}}}
        self.fail = None
        self.expires_after = None
        self.expiries = []
        self.order_expiries = []

    def set_expires_after(self, expires_after):
        self.expires_after = expires_after
        self.expiries.append(expires_after)

    def update_leverage(self, leverage, coin, is_cross):
        self.calls.append(("leverage", leverage, coin, is_cross))
        if self.fail == "leverage": raise TimeoutError("leverage timeout")
        return deepcopy(self.ack)

    def order(self, coin, is_buy, size, price, order_type, reduce_only, cloid):
        self.calls.append(("order", coin, is_buy, size, price, deepcopy(order_type), reduce_only, cloid.to_raw()))
        self.order_expiries.append(self.expires_after)
        if self.fail == "order": raise TimeoutError("order timeout")
        return deepcopy(self.result)

    def market_open(self, *args, **kwargs):
        raise AssertionError("An exact confirmed order must never use market_open")


class UserIocTests(unittest.TestCase):
    def setUp(self):
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.reset()

    def reset(self):
        self.calls = []
        self.client = HyperliquidAccount.__new__(HyperliquidAccount)
        self.client.address = ACCOUNT
        self.client.info = FakeInfo(self.calls)
        self.client.exchange = FakeExchange(self.calls)

    def submit(self, **changes):
        args = dict(coin="BTC", is_buy=True, size=.00123, limit_price=60000., leverage=20, cloid=CLOID)
        args.update(changes)
        return self.client.submit_user_ioc(**args)

    def assert_no_mutations(self):
        self.assertFalse(any(row[0] in ("leverage", "order") for row in self.calls), self.calls)

    def assert_no_order(self):
        self.assertFalse(any(row[0] == "order" for row in self.calls), self.calls)

    def test_exact_ioc_and_fresh_leverage_are_used_once_in_order(self):
        result = self.submit()
        self.assertEqual(result, self.client.exchange.result)
        self.assertEqual([row[0] for row in self.calls], ["meta", "leverage", "active", "order"])
        self.assertEqual(self.calls[1], ("leverage", 20, "BTC", True))
        self.assertEqual(self.calls[2], ("active", "/info", {
            "type": "activeAssetData", "user": ACCOUNT, "coin": "BTC"}))
        self.assertEqual(self.calls[3], ("order", "BTC", True, .00123, 60000.,
                                       {"limit": {"tif": "Ioc"}}, False, CLOID))

    def test_eth_sell_uses_unchanged_limit_size_and_one_leverage(self):
        self.client.info.active.update(coin="ETH", leverage={"type": "cross", "value": 1})
        self.submit(coin="ETH", is_buy=False, size=.0123, limit_price=3000.1, leverage=1)
        self.assertEqual(self.calls[-1], ("order", "ETH", False, .0123, 3000.1,
                                       {"limit": {"tif": "Ioc"}}, False, CLOID))

    def test_readonly_client_cannot_submit_or_fall_back_to_paper(self):
        self.client.exchange = None
        with self.assertRaises(RuntimeError): self.submit()
        self.assertEqual(self.calls, [])

    def test_invalid_sides_markets_leverage_are_rejected_before_mutation(self):
        for changes in [{"coin": x} for x in ("xyz:BTC", "btc", "ETH ", "SOL", None)] + [
                {"is_buy": x} for x in (1, 0, "false", None)] + [
                {"leverage": x} for x in (0, 41, 20., 1.5, "20", True, float("nan"))]:
            with self.subTest(changes=changes):
                self.reset()
                with self.assertRaises(ValueError): self.submit(**changes)
                self.assert_no_mutations()

    def test_invalid_numeric_inputs_cannot_mutate_leverage_or_send(self):
        for field in ("size", "limit_price"):
            for value in (True, None, "0.00123", 0, -1, float("nan"), float("inf"),
                          -float("inf"), 10**400, 2**53 + 1):
                with self.subTest(field=field, value=value):
                    self.reset()
                    with self.assertRaises(ValueError): self.submit(**{field: value})
                    self.assert_no_mutations()
        with self.assertRaises(ValueError): self.submit(size=1e200, limit_price=1e200)
        self.assert_no_mutations()

    def test_invalid_client_ids_rejected_without_mutation(self):
        for value in (None, 1, "", "ab" * 16, "0x" + "a"*31, "0x" + "a"*33,
                      "0x" + "g"*32, CLOID + "\n"):
            with self.subTest(value=value):
                self.reset()
                with self.assertRaises(ValueError): self.submit(cloid=value)
                self.assert_no_mutations()

    def test_unaligned_tick_lot_and_sub_lot_never_round_to_a_different_order(self):
        for changes in ({"size": .001234}, {"size": .000001}, {"limit_price": 60000.1},
                        {"coin": "ETH", "limit_price": 3000.123}):
            with self.subTest(changes=changes):
                self.reset()
                with self.assertRaises(ValueError): self.submit(**changes)
                self.assert_no_mutations()

    def test_missing_malformed_duplicate_market_metadata_fail_closed(self):
        valid = deepcopy(self.client.info.metadata["universe"][0])
        for metadata in (None, {}, {"universe": None}, {"universe": []},
                         {"universe": [valid, valid]}, {"universe": [None]}):
            with self.subTest(metadata=metadata):
                self.reset(); self.client.info.metadata = metadata
                with self.assertRaises(ValueError): self.submit()
                self.assert_no_mutations()

    def test_metadata_precision_limits_and_margin_mode_must_be_verified(self):
        for changes in ([{"szDecimals": x} for x in (None, -1, 7, True, "5", 5.)] +
                        [{"maxLeverage": x} for x in (None, 0, 19, True, "40", 40.)] +
                        [{"isDelisted": True}, {"onlyIsolated": True},
                         {"marginMode": "strictIsolated"}, {"marginMode": "noCross"}]):
            with self.subTest(changes=changes):
                self.reset(); self.client.info.metadata["universe"][0].update(changes)
                with self.assertRaises(ValueError): self.submit()
                self.assert_no_mutations()

    def test_metadata_read_failure_never_reuses_cached_precision(self):
        self.client.info.fail = "meta"
        with self.assertRaises(TimeoutError): self.submit()
        self.assert_no_mutations()

    def test_user_maximum_40_requires_matching_fresh_market_and_configured_leverage(self):
        self.client.info.active["leverage"]["value"] = 40
        self.submit(leverage=40)
        self.assertEqual(self.calls[1], ("leverage", 40, "BTC", True))
        self.assertEqual(sum(row[0] == "order" for row in self.calls), 1)
        self.reset()
        with self.assertRaises(ValueError): self.submit(coin="ETH", leverage=40)
        self.assert_no_mutations()  # ETH fixture's published maximum is only 25.

    def test_leverage_ack_errors_or_malformed_ack_stop_before_ioc(self):
        for ack in (None, {}, {"status": "err", "response": "denied"},
                    {"status": "ok"}, {"status": "ok", "response": {}},
                    {"status": "ok", "response": {"type": "order"}},
                    {"status": "ok", "response": {"type": "default", "data": {
                        "statuses": [{"error": "cannot change leverage"}]}}}):
            with self.subTest(ack=ack):
                self.reset(); self.client.exchange.ack = ack
                with self.assertRaises(RuntimeError): self.submit()
                self.assertEqual([row[0] for row in self.calls], ["meta", "leverage"])
                self.assert_no_order()

    def test_leverage_timeout_is_not_retried_and_never_sends_ioc(self):
        self.client.exchange.fail = "leverage"
        with self.assertRaises(TimeoutError): self.submit()
        self.assertEqual([row[0] for row in self.calls], ["meta", "leverage"])
        self.assert_no_order()

    def test_verified_leverage_account_coin_and_margin_mode_must_match(self):
        valid = deepcopy(self.client.info.active)
        variants = [None, {}, {**valid, "coin": "ETH"}, {**valid, "user": "0x" + "b"*40}]
        variants += [{**valid, "leverage": x} for x in (None, {}, 20,
                    {"type": "isolated", "value": 20}, {"type": "cross", "value": 10},
                    {"type": "cross", "value": "20"}, {"type": "cross", "value": 20.})]
        for active in variants:
            with self.subTest(active=active):
                self.reset(); self.client.info.active = active
                with self.assertRaises(RuntimeError): self.submit()
                self.assertEqual([row[0] for row in self.calls], ["meta", "leverage", "active"])
                self.assert_no_order()

    def test_active_leverage_read_failure_never_sends_ioc_or_retries(self):
        self.client.info.fail = "active"
        with self.assertRaises(TimeoutError): self.submit()
        self.assertEqual([row[0] for row in self.calls], ["meta", "leverage", "active"])
        self.assert_no_order()

    def test_order_timeout_is_propagated_without_retry_or_repricing(self):
        self.client.exchange.fail = "order"
        with self.assertRaises(TimeoutError): self.submit()
        self.assertEqual([row[0] for row in self.calls], ["meta", "leverage", "active", "order"])

    def test_expiry_is_signed_and_prior_sdk_state_is_restored(self):
        self.client.exchange.expires_after = 3000
        with patch("integrations.hyperliquid.time.time", return_value=1):
            self.submit(expires_ms=2000)
        self.assertEqual(self.client.exchange.expiries, [2000, 3000])
        self.assertEqual(self.client.exchange.order_expiries, [2000])
        self.assertEqual(self.client.exchange.expires_after, 3000)

    def test_existing_earlier_expiry_is_never_extended(self):
        self.client.exchange.expires_after = 2000
        with patch("integrations.hyperliquid.time.time", return_value=1):
            self.submit(expires_ms=3000)
        self.assertEqual(self.client.exchange.expiries, [2000, 2000])
        self.assertEqual(self.client.exchange.order_expiries, [2000])

    def test_expired_or_malformed_deadline_cannot_mutate_or_read(self):
        for expiry in (True, 0, -1, "2000", 2000., 999, 1000):
            with self.subTest(expiry=expiry):
                self.reset()
                with patch("integrations.hyperliquid.time.time", return_value=1):
                    with self.assertRaises((ValueError, TimeoutError)): self.submit(expires_ms=expiry)
                self.assertEqual(self.calls, [])

    def test_metadata_consuming_deadline_cannot_change_leverage(self):
        with patch("integrations.hyperliquid.time.time", side_effect=[1, 2.001]):
            with self.assertRaises(TimeoutError): self.submit(expires_ms=2000)
        self.assertEqual([row[0] for row in self.calls], ["meta"])
        self.assert_no_mutations()

    def test_leverage_verification_consuming_deadline_never_sends_ioc(self):
        with patch("integrations.hyperliquid.time.time", side_effect=[1, 1, 1, 2.001]):
            with self.assertRaises(TimeoutError): self.submit(expires_ms=2000)
        self.assertEqual([row[0] for row in self.calls], ["meta", "leverage", "active"])
        self.assert_no_order()
        self.assertEqual(self.client.exchange.expiries, [2000, None])
        self.assertIsNone(self.client.exchange.expires_after)

    def test_expiry_restored_when_leverage_or_order_times_out(self):
        for failure in ("leverage", "order"):
            with self.subTest(failure=failure):
                self.reset(); self.client.exchange.fail = failure
                with patch("integrations.hyperliquid.time.time", return_value=1):
                    with self.assertRaises(TimeoutError): self.submit(expires_ms=2000)
                self.assertEqual(self.client.exchange.expiries, [2000, None])
                self.assertIsNone(self.client.exchange.expires_after)

    def test_local_deadline_still_enforced_without_sdk_expiry_extension(self):
        self.client.exchange.set_expires_after = None
        with patch("integrations.hyperliquid.time.time", side_effect=[1, 1, 1, 2.001]):
            with self.assertRaises(TimeoutError): self.submit(expires_ms=2000)
        self.assert_no_order()

    def test_order_rejection_partial_or_ambiguous_results_are_returned_raw(self):
        for result in (None, {"status": "err", "response": "rejected"},
                       {"status": "ok", "response": {"data": {"statuses": [{"error": "No fill"}]}}},
                       {"status": "ok", "response": {"data": {"statuses": [{"filled": {
                           "totalSz": ".00001", "avgPx": "59999", "oid": 123}}]}}}):
            with self.subTest(result=result):
                self.reset(); self.client.exchange.result = result
                self.assertEqual(self.submit(), result)
                self.assertEqual(sum(row[0] == "order" for row in self.calls), 1)

    def test_public_helpers_work_without_signing_client_and_keep_account_identity(self):
        self.client.exchange = None
        self.assertEqual(self.client.meta(), self.client.info.metadata)
        self.assertEqual(self.client.frontend_open_orders("xyz"), [])
        self.assertEqual(self.client.query_order_by_cloid(CLOID), {"status": "unknownOid"})
        self.assertEqual(self.calls, [("meta", ""), ("orders", ACCOUNT, "xyz"), ("query", ACCOUNT, CLOID)])
        self.assert_no_mutations()

    def test_public_helpers_reject_unsupported_dex_malformed_responses_and_client_ids(self):
        for method in (self.client.meta, self.client.frontend_open_orders):
            with self.assertRaises(ValueError): method("other")
        self.client.info.orders = {}
        with self.assertRaises(ValueError): self.client.frontend_open_orders()
        self.client.info.orders = [None]
        with self.assertRaises(ValueError): self.client.frontend_open_orders()
        self.client.info.query = None
        with self.assertRaises(ValueError): self.client.query_order_by_cloid(CLOID)
        with self.assertRaises(ValueError): self.client.query_order_by_cloid("invalid")
        self.assert_no_mutations()


if __name__ == "__main__":
    unittest.main()
