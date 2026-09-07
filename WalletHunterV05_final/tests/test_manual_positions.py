"""Offline tests: fake exchange only, never submits a real order."""
from copy import deepcopy
import unittest

from core.manual_positions import ManualActionError, ManualPositions


def accepted(oid):
    return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}


def stop(oid=1, coin="xyz:INTC", price=80):
    return {"coin": coin, "oid": oid, "reduceOnly": True,
            "orderType": "Stop Market", "triggerPx": str(price)}


class Exchange:
    exchange = object()

    def __init__(self):
        self.rows = [{"coin": "xyz:INTC", "dex": "xyz", "side": "LONG", "size": 2}]
        self.orders = [stop()]
        self.calls = []
        self.fail_cancel = False
        self.fail_place = False
        self.unknown_place = False
        self.partial_close = False
        self.fail_read = False
        self.hide_new = False

    def positions(self, *args):
        if self.fail_read:
            raise RuntimeError("Exchange unavailable")
        return deepcopy(self.rows)

    def frontend_open_orders(self, dex):
        return deepcopy(self.orders)

    def mid(self, coin, dex):
        return 100

    def place_stop_loss(self, coin, side, size, price, dex):
        self.calls.append(("place", coin, side, size, price, dex))
        if self.unknown_place:
            raise TimeoutError("Unknown submission")
        if self.fail_place:
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "Rejected"}]}}}
        oid = max((int(o["oid"]) for o in self.orders), default=0) + 1
        if not self.hide_new:
            self.orders.append(stop(oid, coin, round(price, 2)))
        return accepted(oid)

    def cancel_order(self, coin, oid, dex):
        self.calls.append(("cancel", coin, oid, dex))
        if self.fail_cancel:
            return {"status": "ok", "response": {"data": {"statuses": [{"error": "Cancellation rejected"}]}}}
        self.orders = [o for o in self.orders if o["oid"] != oid]
        return {"status": "ok", "response": {"data": {"statuses": ["success"]}}}

    def market_close(self, coin, dex):
        self.calls.append(("close", coin, dex))
        self.rows = [dict(self.rows[0], size=0.5)] if self.partial_close else []
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "2"}}]}}}


class ManualPositionTests(unittest.TestCase):
    def setUp(self):
        self.exchange = Exchange()
        self.runtime = {"managed": ["xyz:INTC|xyz"], "manual_stops": {
            "xyz:INTC|xyz": {"coin": "xyz:INTC", "dex": "xyz", "oid": 1, "price": 80}
        }}
        self.snapshots = []
        def persist():
            self.snapshots.append(deepcopy(self.runtime))
            # Exercise Storage's Snapshot-refresh semantics.
            cloned = deepcopy(self.runtime)
            self.runtime.clear()
            self.runtime.update(cloned)
        self.service = ManualPositions(self.exchange, self.runtime, persist, delay=0)

    def test_replacement_verifies_new_before_cancelling_old(self):
        result = self.service.set_stop_loss("INTC", "xyz", 90.123)
        self.assertEqual([c[0] for c in self.exchange.calls], ["place", "cancel"])
        self.assertEqual([o["oid"] for o in self.exchange.orders], [2])
        self.assertEqual(result["stop"]["price"], 90.12)
        self.assertEqual(self.runtime["manual_stops"]["xyz:INTC|xyz"]["extra_oids"], [])
        self.assertEqual(self.snapshots[0]["manual_actions"]["xyz:INTC|xyz"]["status"], "submitting")

    def test_rejected_replacement_keeps_old_stop(self):
        self.exchange.fail_place = True
        with self.assertRaises(ManualActionError):
            self.service.set_stop_loss("xyz:INTC", "", 90)
        self.assertEqual(self.runtime["manual_stops"]["xyz:INTC|xyz"]["oid"], 1)
        self.assertEqual(self.exchange.orders[0]["oid"], 1)
        self.assertFalse(any(c[0] == "cancel" for c in self.exchange.calls))

    def test_unseen_replacement_never_cancels_protection(self):
        self.exchange.hide_new = True
        with self.assertRaises(ManualActionError):
            self.service.set_stop_loss("INTC", "xyz", 90)
        self.assertEqual(self.runtime["manual_actions"]["xyz:INTC|xyz"]["status"], "unknown")
        self.assertEqual([c[0] for c in self.exchange.calls], ["place"])

    def test_cancel_error_keeps_both_stops_recorded(self):
        self.exchange.fail_cancel = True
        with self.assertRaises(ManualActionError):
            self.service.set_stop_loss("INTC", "xyz", 90)
        saved = self.runtime["manual_stops"]["xyz:INTC|xyz"]
        self.assertEqual((saved["oid"], saved["extra_oids"]), (2, [1]))
        self.assertEqual(len(self.exchange.orders), 2)

    def test_delete_nested_rejection_preserves_marker(self):
        self.exchange.fail_cancel = True
        with self.assertRaises(ManualActionError):
            self.service.delete_stop_loss("INTC", "xyz")
        self.assertIn("xyz:INTC|xyz", self.runtime["manual_stops"])

    def test_delete_discovers_orphan_without_position_or_local_record(self):
        self.runtime["manual_stops"] = {}
        self.exchange.rows = []
        result = self.service.delete_stop_loss("INTC", "xyz")
        self.assertTrue(result["ok"])
        self.assertEqual(self.exchange.orders, [])

    def test_delete_does_not_cancel_take_profit_or_other_asset(self):
        self.exchange.orders.extend([dict(stop(2), orderType="Take Profit Market"), stop(3, "xyz:NVDA")])
        self.service.delete_stop_loss("INTC", "xyz")
        self.assertEqual([o["oid"] for o in self.exchange.orders], [2, 3])

    def test_delete_verifies_already_absent(self):
        self.exchange.orders = []
        result = self.service.delete_stop_loss("INTC", "xyz")
        self.assertTrue(result["already_removed"])
        self.assertNotIn("xyz:INTC|xyz", self.runtime["manual_stops"])

    def test_failed_position_read_submits_nothing(self):
        self.exchange.fail_read = True
        with self.assertRaises(RuntimeError):
            self.service.close_position("INTC", "xyz")
        self.assertEqual(self.exchange.calls, [])

    def test_partial_close_keeps_stop_and_sets_persistent_copy_hold(self):
        self.exchange.partial_close = True
        with self.assertRaises(ManualActionError):
            self.service.close_position("INTC", "xyz")
        self.assertEqual([c[0] for c in self.exchange.calls], ["close"])
        self.assertIn("xyz:INTC|xyz", self.runtime["managed"])
        self.assertIn("xyz:INTC|xyz", self.runtime["manual_hold_keys"])
        self.assertIn("xyz:INTC|xyz", self.runtime["manual_stops"])

    def test_full_close_removes_stop_only_after_verified_flat(self):
        result = self.service.close_position("INTC", "xyz")
        self.assertTrue(result["closed"])
        self.assertEqual([c[0] for c in self.exchange.calls], ["close", "cancel"])
        self.assertEqual(self.runtime["managed"], [])
        self.assertEqual(self.runtime["manual_stops"], {})
        self.assertIn("xyz:INTC|xyz", self.snapshots[0]["manual_hold_keys"])

    def test_unknown_submission_blocks_double_click(self):
        self.exchange.unknown_place = True
        with self.assertRaises(ManualActionError):
            self.service.set_stop_loss("INTC", "xyz", 90)
        with self.assertRaises(ManualActionError):
            self.service.set_stop_loss("INTC", "xyz", 90)
        self.assertEqual(len(self.exchange.calls), 1)

    def test_invalid_price_cannot_submit(self):
        for price in [0, -1, float("nan"), float("inf"), 100, 110]:
            with self.subTest(price=price), self.assertRaises(ManualActionError):
                self.service.set_stop_loss("INTC", "xyz", price)
        self.assertEqual(self.exchange.calls, [])

    def test_short_stop_direction_and_canonical_crypto_key(self):
        self.exchange.rows = [{"coin": "BTC", "dex": None, "side": "SHORT", "size": .01}]
        self.exchange.orders = []
        with self.assertRaises(ManualActionError):
            self.service.set_stop_loss("BTC", "", 90)
        result = self.service.set_stop_loss("BTC", "", 110)
        self.assertEqual(result["stop"]["side"], "SHORT")
        self.assertIn("BTC|", self.runtime["manual_stops"])

    def test_no_order_if_initial_persistence_fails(self):
        def failure():
            raise IOError("No disk space")
        service = ManualPositions(self.exchange, self.runtime, failure)
        with self.assertRaises(IOError):
            service.close_position("INTC", "xyz")
        self.assertEqual(self.exchange.calls, [])


if __name__ == "__main__":
    unittest.main()
