"""Read-only migration evidence; all state is disposable and all time is fake."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from core.foundation.journal_bridge import JournalEvidence, SynchronizedLedger, read_journal
from core.foundation.ledger import Reservation
from test_foundation import scope, portfolio, position, intent


def owned():
    return {"BTC|": dict(network="TESTNET", managed=True, side="LONG", size=7.,
        position=dict(side="LONG", size=7., entry_price=100., position_value=700., leverage=1., margin_used=700.),
        source_targets=[dict(wallet="a", signed_notional=700., margin=700.)])}


def pending():
    return {"ETH|": dict(network="TESTNET", action="RECONCILE", before=None,
        target=dict(side="LONG", target_notional=200., leverage=1.,
            sources=[dict(wallet="a", signed_notional=200., margin=200.)]))}


class JournalBridgeTests(unittest.TestCase):
    def ledger(self, *, evidence=None, snapshot=None, **kwargs):
        return SynchronizedLedger(snapshot or portfolio(positions=(position(),), available_collateral=2300.),
            ("a", "b", "c"), evidence or JournalEvidence(scope(), owned(), pending()),
            now_ms=kwargs.pop("now_ms", 1000), max_age_ms=100, **kwargs)

    def test_held_capital_and_pending_target_both_consume_own_third(self):
        row = self.ledger()
        self.assertEqual(row.errors, [])
        self.assertEqual((row.allocation("a").committed, row.allocation("a").reserved, row.allocation("a").available), (700., 200., 100.))
        self.assertEqual(row.allocation("b").available, 1000.)
        self.assertEqual(row.available_capacity, 2100.)

    def test_pending_same_market_reserves_increment_not_position_twice(self):
        p = pending()["ETH|"]
        p["target"]["target_notional"] = 900.
        p["target"]["sources"] = [dict(wallet="a", signed_notional=900., margin=900.)]
        p["before"] = owned()["BTC|"]["position"]
        row = self.ledger(evidence=JournalEvidence(scope(), owned(), {"BTC|": p}))
        self.assertEqual(row.errors, [])
        self.assertEqual(row.allocation("a").reserved, 200.)

    def test_flat_exchange_does_not_fabricate_legacy_release(self):
        row = self.ledger(snapshot=portfolio())
        self.assertIn("OWNERSHIP_NOT_CURRENTLY_PROVEN", row.errors)
        self.assertIsNone(row.available_capacity)

    def test_unproven_exchange_position_not_adopted(self):
        p = position(evidence="EXTERNAL", contributions=(), order_ids=())
        row = self.ledger(snapshot=portfolio(positions=(p,)))
        self.assertIn("OWNERSHIP_NOT_CURRENTLY_PROVEN", row.errors)
        with self.assertRaises(ValueError): row.allocation("a")

    def test_legacy_or_other_network_pending_blocks(self):
        for network in (None, "MAINNET", "LEGACY_UNKNOWN"):
            p = pending()
            p["ETH|"]["network"] = network
            row = self.ledger(evidence=JournalEvidence(scope(), owned(), p))
            self.assertIn("LEGACY_OR_NETWORK_MISMATCH", row.errors)
            self.assertIsNone(row.available_capacity)

    def test_tenant_and_account_isolation(self):
        for other in (scope(tenant="two"), scope(network="MAINNET")):
            row = self.ledger(evidence=JournalEvidence(other, owned(), pending()))
            self.assertIn("JOURNAL_SCOPE_MISMATCH", row.errors)

    def test_expired_account_evidence_never_authorizes(self):
        self.assertIn("ACCOUNT_EVIDENCE_STALE", self.ledger(now_ms=1101).errors)
        self.assertIsNone(self.ledger(now_ms=999).available_capacity)

    def test_unknown_ai_pending_not_treated_as_zero(self):
        p = pending()
        p["ETH|"]["action"] = "AI_USER_OPEN"
        row = self.ledger(evidence=JournalEvidence(scope(), owned(), p))
        self.assertIn("JOURNAL_RECONCILIATION_REQUIRED", row.errors)
        self.assertIsNone(row.available_capacity)

    def test_independent_reservation_overlap_requires_identity(self):
        row = self.ledger(reservations=[Reservation("ai", "a", 200., 200.)])
        self.assertIn("RESERVATION_IDENTITY_UNRESOLVED", row.errors)

    def test_invalid_financial_values_fail_closed(self):
        for number in (float("nan"), float("inf"), -1., True):
            p = pending()
            p["ETH|"]["target"]["target_notional"] = number
            self.assertIsNone(self.ledger(evidence=JournalEvidence(scope(), owned(), p)).available_capacity)

    def test_restart_and_inputs_unchanged(self):
        evidence = JournalEvidence(scope(), owned(), pending())
        before = json.dumps([evidence.owned, evidence.pending], sort_keys=True)
        one = self.ledger(evidence=evidence)
        two = self.ledger(evidence=JournalEvidence(scope(), *json.loads(before)))
        self.assertEqual(one.allocations, two.allocations)
        self.assertEqual(before, json.dumps([evidence.owned, evidence.pending], sort_keys=True))

    def test_readonly_journal_atomic_snapshot_and_account_filter(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"journal.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.executescript("CREATE TABLE ownership(account,market,record); CREATE TABLE operations(account,market,status,intent);")
                db.execute("INSERT INTO ownership VALUES(?,?,?)", (scope().account, "BTC|", json.dumps(owned()["BTC|"])))
                db.execute("INSERT INTO operations VALUES(?,?,?,?)", (scope().account, "ETH|", "UNKNOWN", json.dumps(pending()["ETH|"])))
                db.execute("INSERT INTO operations VALUES(?,?,?,?)", ("other", "ETH|", "UNKNOWN", "invalid"))
                db.commit()
            before = path.read_bytes()
            result = read_journal(path, scope())
            self.assertEqual(result.pending, pending())
            self.assertEqual(result.owned, owned())
            self.assertEqual(path.read_bytes(), before)

    def test_missing_journal_is_not_created_or_empty(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"missing.sqlite3"
            with self.assertRaises(ValueError): read_journal(path, scope())
            self.assertFalse(path.exists())

    def test_truncation_duplicates_and_corrupt_json_fail_closed(self):
        for bodies in (("{}", "{}"), ("{\"a\":1,\"a\":2}",), ("{\"a\":NaN}",), ("{\"a\":1e999}",), ("not-json",)):
            with self.subTest(bodies=bodies), tempfile.TemporaryDirectory() as root:
                path = Path(root)/"journal.sqlite3"
                with closing(sqlite3.connect(path)) as db:
                    db.executescript("CREATE TABLE ownership(account,market,record); CREATE TABLE operations(account,market,status,intent);")
                    db.executemany("INSERT INTO operations VALUES(?,?,?,?)", [(scope().account, "BTC|", "UNKNOWN", body) for body in bodies])
                    db.commit()
                with self.assertRaises(ValueError): read_journal(path, scope(), limit=1)


class LiveReconciliationTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from core.foundation.live_reconciliation import client_order_id
        self.intent = intent(execution_mode="LIVE", authorization="USER_CONFIRMED")
        self.before = portfolio(evidence="EXCHANGE")
        self.after = portfolio(evidence="EXCHANGE", revision=2, exchange_ms=1002, received_ms=1002,
            positions=(position(size=1., notional=100., margin=100., evidence="EXTERNAL", contributions=(), order_ids=()),))
        self.response = dict(status="order", order=dict(status="filled", statusTimestamp=1001,
            order=dict(oid=12, cloid=client_order_id(self.intent), coin="BTC", side="B", origSz="1", limitPx="100", timestamp=1000, reduceOnly=False)))
        self.fills = [dict(oid=12, tid=1, coin="BTC", side="B", sz="1", px="100", time=1001)]
        self.queries = []
        self.client = SimpleNamespace(network="TESTNET", address=scope().account,
            query_order_by_cloid=lambda cloid: self.response,
            info=SimpleNamespace(user_fills_by_time=lambda *args: self.history(*args)))

    def history(self, *args):
        self.queries.append(args)
        return self.fills

    def result(self):
        from core.foundation.live_reconciliation import reconcile_order
        return reconcile_order(self.intent, self.before, self.after, self.client, now_ms=1002, max_age_ms=100)

    def test_live_compatible_proof_without_signing_or_ack(self):
        result = self.result()
        self.assertEqual((result.status, result.provenance, result.order_ids), ("FILLED", "EXCHANGE", ("12",)))
        self.assertEqual(self.queries, [(scope().account, 1000, 1002)])
        self.assertEqual(self.after.positions[0].evidence, "EXTERNAL")  # No fabricated adoption.

    def test_partial_fill(self):
        self.fills[0]["sz"] = ".5"
        self.response["order"]["status"] = "iocCancel"
        self.after = self.after.model_copy(update={"positions": (self.after.positions[0].model_copy(update={"size": .5}),)})
        self.assertEqual(self.result().status, "PARTIAL")

    def test_terminal_unfilled_is_rejected_not_retried(self):
        self.fills = []
        self.after = self.after.model_copy(update={"positions": ()})
        self.response["order"]["status"] = "iocCancel"
        self.assertEqual(self.result().status, "REJECTED")

    def test_close_and_reduce_use_fill_proof(self):
        from core.foundation.live_reconciliation import client_order_id
        for action in ("CLOSE", "REDUCE"):
            self.intent = intent(execution_mode="LIVE", authorization="USER_CONFIRMED", action=action, side="SELL")
            self.before = portfolio(evidence="EXCHANGE", positions=(position(size=1., notional=100., margin=100.),))
            self.after = self.after.model_copy(update={"positions": ()})
            self.response["order"]["order"].update(cloid=client_order_id(self.intent), side="A", reduceOnly=True)
            self.fills[0]["side"] = "A"
            self.assertEqual(self.result().status, "FILLED")

    def test_external_concurrent_fill_requires_hold(self):
        self.fills.append(dict(self.fills[0], oid=99, tid=2))
        self.assertEqual(self.result().status, "UNKNOWN")

    def test_aggregate_delta_alone_cannot_prove_fill(self):
        self.fills = []
        self.assertEqual(self.result().status, "UNKNOWN")

    def test_duplicate_history_is_not_double_counted(self):
        self.fills.append(dict(self.fills[0]))
        self.assertEqual(self.result().status, "UNKNOWN")

    def test_timeouts_and_ack_visibility_delay_remain_unknown(self):
        def timeout(*_): raise TimeoutError("synthetic request detail must not escape")
        self.client.query_order_by_cloid = timeout
        self.assertEqual(self.result().status, "UNKNOWN")
        self.assertEqual(self.queries, [])
        self.client.query_order_by_cloid = lambda _: {"status": "unknownOid"}
        self.assertEqual(self.result().status, "UNKNOWN")

    def test_network_account_and_tenant_mismatch(self):
        self.client.network = "MAINNET"
        self.assertEqual(self.result().status, "UNKNOWN")
        self.client.network = "TESTNET"
        self.client.address = "0x"+"b"*40
        self.assertEqual(self.result().status, "UNKNOWN")
        self.client.address = scope().account
        self.after = self.after.model_copy(update={"scope": scope(tenant="two")})
        self.assertEqual(self.result().status, "UNKNOWN")

    def test_stale_or_unknown_account_evidence(self):
        self.after = self.after.model_copy(update={"exchange_ms": 1})
        self.assertEqual(self.result().status, "UNKNOWN")
        self.after = self.after.model_copy(update={"exchange_ms": 1002, "completeness": "UNKNOWN"})
        self.assertEqual(self.result().status, "UNKNOWN")

    def test_wrong_legacy_client_id_not_adopted(self):
        self.response["order"]["order"]["cloid"] = "0x"+"0"*32
        self.assertEqual(self.result().status, "UNKNOWN")

    def test_nonfinite_malformed_and_truncated_fill_evidence(self):
        for value in ("NaN", "Infinity", "-1", None):
            self.fills[0]["sz"] = value
            self.assertEqual(self.result().status, "UNKNOWN")
        self.fills = [self.fills[0]]*2000
        self.assertEqual(self.result().status, "UNKNOWN")
