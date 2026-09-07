"""Offline exact-IOC position actions; any non-IOC exchange mutation is fatal."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from integrations.hyperliquid import HyperliquidAccount


ACCOUNT = "0x" + "a"*40
CLOID = "0x" + "e1"*16


class PublicInfo:
    def __init__(self, calls):
        self.calls = calls
        self.metadata = {}
        self.state = {}
        self.orders = []
        self.after_read = {}
        self.fail = None

    def _after(self, name):
        if self.fail == name: raise TimeoutError("Read unavailable")
        callback = self.after_read.get(name)
        if callback: callback()

    def meta(self, dex=""):
        self.calls.append(("meta", dex)); self._after("meta")
        return deepcopy(self.metadata)

    def frontend_open_orders(self, account, dex=""):
        self.calls.append(("orders", account, dex)); self._after("orders")
        return deepcopy(self.orders)

    def user_state(self, account, dex=""):
        self.calls.append(("positions", account, dex)); self._after("positions")
        return deepcopy(self.state)


class ExactExchange:
    def __init__(self, calls):
        self.calls = calls
        self.expires_after = None
        self.expiries = []
        self.order_expiries = []
        self.result = {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": ".05", "avgPx": "2500", "oid": 17}}]}}}
        self.fail = False

    def set_expires_after(self, value):
        self.expires_after = value; self.expiries.append(value)

    def order(self, coin, buy, size, limit, order_type, *, reduce_only, cloid):
        self.calls.append(("order", coin, buy, size, limit, deepcopy(order_type), reduce_only, cloid.to_raw()))
        self.order_expiries.append(self.expires_after)
        if self.fail: raise TimeoutError("Uncertain IOC")
        return deepcopy(self.result)

    def update_leverage(self, *args, **kwargs): raise AssertionError("Must not change leverage")
    def update_isolated_margin(self, *args, **kwargs): raise AssertionError("Must not change margin")
    def market_open(self, *args, **kwargs): raise AssertionError("Must not reprice")
    def market_close(self, *args, **kwargs): raise AssertionError("Must not close full position")
    def cancel(self, *args, **kwargs): raise AssertionError("Must not cancel protective orders")


class PositionActionSdkTests(unittest.TestCase):
    def test_ai_closure_requires_complete_balanced_unique_fills_and_flat_market(self):
        from types import SimpleNamespace
        client = HyperliquidAccount.__new__(HyperliquidAccount)
        client.address = ACCOUNT
        client.info = SimpleNamespace(post=lambda *args: None)
        client.positions = lambda *args: []
        client.frontend_open_orders = lambda *args: []
        entry = {"coin": "BTC", "tid": 1, "oid": 7, "sz": "1", "side": "B"}
        close = {"coin": "BTC", "tid": 2, "oid": 8, "sz": "1", "side": "A"}
        payload, result = {"coin": "BTC", "created_ms": 0}, {"oid": 7, "filled_size": 1}
        with patch("integrations.hyperliquid.fetch_fills", return_value=[entry, close]):
            self.assertEqual(client.verify_ai_closed(payload, result)["status"], "CLOSED_RECONCILED")
            client.frontend_open_orders = lambda *args: [{"coin": "BTC", "oid": 9}]
            with self.assertRaises(ValueError): client.verify_ai_closed(payload, result)
        client.frontend_open_orders = lambda *args: []
        for fills in ([], [entry], [entry, close, close], [dict(entry, sz="NaN"), close], [dict(entry, oid=99), close]):
            with self.subTest(fills=fills), patch("integrations.hyperliquid.fetch_fills", return_value=fills), self.assertRaises(ValueError):
                client.verify_ai_closed(payload, result)

    def test_reduce_uses_explicit_percent_slippage_and_reduce_only_ioc(self):
        from types import SimpleNamespace
        calls = []
        client = HyperliquidAccount.__new__(HyperliquidAccount)
        client._sz_decimals = lambda coin: 4
        def limit(coin, buy, slippage, price):
            calls.append(("price", coin, buy, slippage, price))
            return price*(1+slippage if buy else 1-slippage)
        client.exchange = SimpleNamespace(info=SimpleNamespace(post=lambda *args: {"levels": [[{"px": "100"}], [{"px": "101"}]]}),
            _slippage_price=limit, order=lambda *args, **kwargs: calls.append(("order", args, kwargs)))
        client.market_reduce("BTC", False, 1, slippage_pct=0.5)
        self.assertEqual(calls[0], ("price", "BTC", False, 0.005, 100.))
        self.assertEqual(calls[1], ("order", ("BTC", False, 1., 99.5, {"limit": {"tif": "Ioc"}}), {"reduce_only": True}))

    def test_invalid_network_is_rejected_before_any_transport(self):
        from core.settings import validated_network
        from core.hyperliquid import HyperliquidReader
        for mode in ("PAPER", "", "unknown", None):
            for factory in (validated_network, HyperliquidReader, lambda m: HyperliquidAccount(ACCOUNT, None, m)):
                with self.subTest(mode=mode), self.assertRaises(ValueError): factory(mode)
        self.assertEqual(validated_network(" testnet "), "TESTNET")

    def test_close_slippage_is_explicit_and_cancellation_preserves_unrelated_orders(self):
        from types import SimpleNamespace
        calls = []
        client = HyperliquidAccount.__new__(HyperliquidAccount)
        client.address = ACCOUNT
        client.exchange = SimpleNamespace(market_close=lambda coin, **kw: calls.append((coin, kw)),
            cancel=lambda coin, oid: calls.append((coin, oid)))
        client.info = SimpleNamespace(open_orders=lambda address, dex: [{"coin": "BTC", "oid": 1}, {"coin": "BTC", "oid": 2}] if not dex else [])
        client.market_close("BTC", slippage_pct=0.5)
        self.assertEqual(calls, [("BTC", {"slippage": 0.005})])
        for value in (float("nan"), float("inf"), -1, 0, 11):
            with self.assertRaises(ValueError): client.market_close("BTC", slippage_pct=value)
        calls.clear()
        self.assertEqual(client.cancel_open_orders(), [])
        self.assertEqual(calls, [])
        client.cancel_open_orders([1])
        self.assertEqual(calls, [("BTC", 1)])

    def test_account_control_is_derived_or_exchange_delegated_without_trading(self):
        from eth_account import Account
        from integrations.hyperliquid import verify_account_control
        wallet = Account.create()
        calls = []
        def info(query):
            calls.append(query)
            return {"role": "agent", "data": {"user": ACCOUNT}}
        direct = verify_account_control(wallet.address, wallet.key, info)
        self.assertEqual(direct["account_type"], "DIRECT")
        self.assertEqual(calls, [])
        delegated = verify_account_control(ACCOUNT, wallet.key, info)
        self.assertEqual(delegated["account_type"], "API_AGENT")
        self.assertEqual(calls[0]["user"], wallet.address.lower())
        for role in ({"role": "user"}, {"role": "vault"},
                     {"role": "agent", "data": {"user": "0x"+"b"*40}},
                     {"role": "subAccount", "data": {"master": ACCOUNT}}, None):
            with self.subTest(role=role), self.assertRaises(ValueError):
                verify_account_control(ACCOUNT, wallet.key, lambda query: role)
        secret = "invalid-credential-must-not-appear"
        with self.assertRaises(ValueError) as error:
            verify_account_control(ACCOUNT, secret, info)
        self.assertNotIn(secret, str(error.exception))
        def unavailable(query): raise RuntimeError(secret)
        with self.assertRaises(ValueError) as error:
            verify_account_control(ACCOUNT, wallet.key, unavailable)
        self.assertNotIn(secret, str(error.exception))

    def setUp(self):
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden"))
        network.start(); self.addCleanup(network.stop)
        clock = patch("integrations.hyperliquid.time.time", return_value=1)
        self.clock = clock.start(); self.addCleanup(clock.stop)
        self.reset()

    def reset(self, coin="ETH", dex="", side="SHORT", leverage=20, mode="cross"):
        self.clock.return_value = 1
        self.calls = []
        self.client = HyperliquidAccount.__new__(HyperliquidAccount)
        self.client.address = ACCOUNT
        self.client.info = PublicInfo(self.calls)
        self.client.exchange = ExactExchange(self.calls)
        self.expected = {"coin": coin, "dex": dex or None, "side": side, "size": .2,
                         "entry_price": 2400., "leverage": float(leverage), "margin_mode": mode}
        self.client.info.metadata = {"universe": [{"name": coin, "szDecimals": 4, "maxLeverage": 40}]}
        self.client.info.state = {"assetPositions": [{"position": {
            "coin": coin, "szi": "0.2" if side == "LONG" else "-0.2", "entryPx": "2400",
            "leverage": {"type": mode, "value": leverage}, "positionValue": "500",
            "marginUsed": str(500/leverage), "unrealizedPnl": "-20", "returnOnEquity": "-.8",
        }}]}

    def submit(self, **changes):
        args = {"coin": self.expected["coin"], "is_buy": self.expected["side"] == "SHORT",
                "size": .05, "limit_price": 2500., "reduce_only": True, "cloid": CLOID,
                "dex": self.expected["dex"] or "", "expires_ms": 2000, "expected_position": deepcopy(self.expected)}
        args.update(changes)
        return self.client.submit_position_ioc(**args)

    def no_order(self):
        self.assertFalse(any(row[0] == "order" for row in self.calls), self.calls)

    def test_reduce_short_is_exact_opposite_side_ioc_without_leverage_change(self):
        self.assertEqual(self.submit(), self.client.exchange.result)
        self.assertEqual([row[0] for row in self.calls], ["meta", "orders", "positions", "order"])
        self.assertEqual(self.calls[-1], ("order", "ETH", True, .05, 2500.,
                                        {"limit": {"tif": "Ioc"}}, True, CLOID))
        self.assertEqual(self.client.exchange.expiries, [2000, None])
        self.assertEqual(self.client.exchange.order_expiries, [2000])

    def test_average_short_keeps_existing_side_and_leverage(self):
        self.submit(is_buy=False, reduce_only=False)
        self.assertEqual(self.calls[-1], ("order", "ETH", False, .05, 2500.,
                                        {"limit": {"tif": "Ioc"}}, False, CLOID))

    def test_reduce_and_average_long_use_correct_sides(self):
        for reducing in (True, False):
            with self.subTest(reducing=reducing):
                self.reset(side="LONG")
                self.submit(is_buy=not reducing, reduce_only=reducing)
                self.assertEqual(self.calls[-1][2], not reducing)
                self.assertEqual(self.calls[-1][-2], reducing)

    def test_verified_xyz_isolated_position_stays_isolated(self):
        self.reset(coin="xyz:INTC", dex="xyz", side="LONG", leverage=5, mode="isolated")
        self.client.info.metadata["universe"][0].update(onlyIsolated=True, marginMode="strictIsolated")
        self.submit(coin="INTC", is_buy=True, reduce_only=False)
        self.assertEqual(self.calls[0], ("meta", "xyz"))
        self.assertEqual(self.calls[2], ("positions", ACCOUNT, "xyz"))
        self.assertEqual(self.calls[-1][1], "xyz:INTC")

    def test_unrelated_core_position_does_not_block_verified_eth_action(self):
        self.client.info.state["assetPositions"].append({"position": {
            "coin": "SOL", "szi": "1", "entryPx": "100", "positionValue": "100",
            "marginUsed": "20", "unrealizedPnl": "0", "returnOnEquity": "0",
            "leverage": {"type": "cross", "value": 5}}})
        self.submit()
        self.assertEqual(self.calls[-1][0], "order")

    def test_wrong_side_full_close_and_reversal_are_rejected(self):
        for changes in ({"is_buy": False}, {"size": .2}, {"size": .3},
                        {"reduce_only": False, "is_buy": True}):
            with self.subTest(changes=changes):
                self.reset()
                with self.assertRaises(ValueError): self.submit(**changes)
                self.no_order()

    def test_readonly_client_never_falls_back_to_paper_execution(self):
        self.client.exchange = None
        with self.assertRaises(RuntimeError): self.submit()
        self.assertEqual(self.calls, [])

    def test_invalid_numeric_boolean_precision_and_client_id_inputs_fail_closed(self):
        for changes in ([{field: value} for field in ("size", "limit_price") for value in (
                True, None, "1", 0, -1, float("nan"), float("inf"), 10**400, 2**53+1)] +
                [{"size": .05001}, {"limit_price": 2500.123}, {"is_buy": 1},
                 {"reduce_only": "true"}, {"cloid": "0x" + "z"*32}]):
            with self.subTest(changes=changes):
                self.reset()
                with self.assertRaises(ValueError): self.submit(**changes)
                self.no_order()

    def test_unsupported_markets_and_cross_dex_aliases_are_rejected(self):
        for changes in ({"coin": "SOL"}, {"coin": "xyz:ETH"}, {"coin": "xyz:ETH", "dex": "other"},
                        {"coin": "other:INTC", "dex": "xyz"}, {"coin": " INT C ", "dex": "xyz"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError): self.submit(**changes)
                self.no_order()

    def test_invalid_missing_duplicate_or_delisted_metadata_fail_closed(self):
        valid = deepcopy(self.client.info.metadata)
        variants = [None, {}, {"universe": []}, {"universe": valid["universe"]*2}]
        for field, value in (("szDecimals", None), ("szDecimals", True), ("szDecimals", 7),
                             ("maxLeverage", 0), ("maxLeverage", "40"), ("isDelisted", True)):
            metadata = deepcopy(valid); metadata["universe"][0][field] = value; variants.append(metadata)
        for metadata in variants:
            with self.subTest(metadata=metadata):
                self.reset(); self.client.info.metadata = metadata
                with self.assertRaises(ValueError): self.submit()
                self.no_order()

    def test_current_leverage_above_new_limit_allows_only_reduction(self):
        self.client.info.metadata["universe"][0]["maxLeverage"] = 10
        self.submit()
        self.reset(); self.client.info.metadata["universe"][0]["maxLeverage"] = 10
        with self.assertRaises(ValueError): self.submit(is_buy=False, reduce_only=False)
        self.no_order()

    def test_cross_position_never_becomes_isolated_to_fit_metadata(self):
        self.client.info.metadata["universe"][0]["onlyIsolated"] = True
        with self.assertRaises(ValueError): self.submit()
        self.no_order()

    def test_expected_position_identity_is_required_and_must_match(self):
        variants = [None, {}, {**self.expected, "coin": "BTC"}, {**self.expected, "dex": "xyz"}]
        variants += [{**self.expected, field: value} for field, value in (
            ("size", .1), ("entry_price", 2401.), ("leverage", 10.), ("margin_mode", "isolated"),
            ("side", "LONG"), ("size", float("nan")), ("leverage", True))]
        for expected in variants:
            with self.subTest(expected=expected):
                self.reset()
                with self.assertRaises(ValueError): self.submit(expected_position=expected)
                self.no_order()

    def test_missing_duplicate_or_malformed_fresh_positions_fail_closed(self):
        valid = deepcopy(self.client.info.state)
        for state in ({}, {"assetPositions": []}, {"assetPositions": valid["assetPositions"]*2},
                      {"assetPositions": [{"position": {"coin": "ETH", "szi": "nan"}}]}):
            with self.subTest(state=state):
                self.reset(); self.client.info.state = state
                with self.assertRaises((ValueError, RuntimeError)): self.submit()
                self.no_order()

    def test_same_market_open_or_trigger_order_blocks_without_cancelling(self):
        for order in ({"coin": "ETH", "oid": 1}, {"coin": "ETH", "oid": 2, "isTrigger": True}, {}):
            with self.subTest(order=order):
                self.reset(); self.client.info.orders = [order]
                with self.assertRaises(ValueError): self.submit()
                self.no_order()
        self.reset(); self.client.info.orders = [{"coin": "BTC", "oid": 3}]
        self.submit()

    def test_fresh_snapshot_after_order_check_catches_manual_position_change(self):
        def manual_change(): self.client.info.state["assetPositions"][0]["position"]["szi"] = "-.1"
        self.client.info.after_read["orders"] = manual_change
        with self.assertRaises(ValueError): self.submit()
        self.no_order()

    def test_xyz_trigger_alias_is_normalized_before_collision_check(self):
        self.reset(coin="xyz:INTC", dex="xyz", leverage=5, mode="isolated")
        self.client.info.orders = [{"coin": "INTC", "oid": 1, "isTrigger": True}]
        with self.assertRaises(ValueError): self.submit()
        self.no_order()

    def test_expired_or_malformed_deadline_has_no_order(self):
        for value in (None, 0, True, "2000", 2000., 1000):
            with self.subTest(value=value):
                self.reset()
                with self.assertRaises((ValueError, TimeoutError)): self.submit(expires_ms=value)
                self.assertEqual(self.calls, [])

    def test_expiration_during_each_public_read_stops_before_ioc(self):
        for read in ("meta", "orders", "positions"):
            with self.subTest(read=read):
                self.reset(); self.client.info.after_read[read] = lambda: setattr(self.clock, "return_value", 3)
                with self.assertRaises(TimeoutError): self.submit()
                self.no_order()
                self.assertIsNone(self.client.exchange.expires_after)

    def test_read_errors_propagate_without_fallback_or_retry(self):
        for read in ("meta", "orders", "positions"):
            with self.subTest(read=read):
                self.reset(); self.client.info.fail = read
                with self.assertRaises((TimeoutError, RuntimeError)): self.submit()
                self.no_order()
                self.assertEqual(sum(row[0] == read for row in self.calls), 1)

    def test_order_timeout_stays_uncertain_and_never_retries(self):
        self.client.exchange.fail = True
        with self.assertRaises(TimeoutError): self.submit()
        self.assertEqual(sum(row[0] == "order" for row in self.calls), 1)
        self.assertIsNone(self.client.exchange.expires_after)

    def test_partial_and_rejected_responses_are_returned_without_fake_success(self):
        for response in (None, {"status": "err", "response": "Rejected"},
                         {"status": "ok", "response": {"data": {"statuses": [{"filled": {
                             "totalSz": ".001", "avgPx": "2500", "oid": 17}}]}}}):
            with self.subTest(response=response):
                self.reset(); self.client.exchange.result = response
                self.assertEqual(self.submit(), response)
                self.assertEqual(sum(row[0] == "order" for row in self.calls), 1)

    def test_earlier_sdk_expiry_is_preserved_and_restored(self):
        self.client.exchange.expires_after = 1500
        self.submit()
        self.assertEqual(self.client.exchange.expiries, [1500, 1500])
        self.assertEqual(self.client.exchange.order_expiries, [1500])


if __name__ == "__main__":
    unittest.main()
