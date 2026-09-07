"""Read-only migration evidence; all state is disposable and all time is fake."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from core.foundation.journal_bridge import JournalEvidence, SynchronizedLedger, read_journal
from core.foundation.ledger import Reservation
from test_foundation import scope, portfolio, position


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
        for bodies in (("{}", "{}"), ("{\"a\":1,\"a\":2}",), ("{\"a\":NaN}",), ("not-json",)):
            with self.subTest(bodies=bodies), tempfile.TemporaryDirectory() as root:
                path = Path(root)/"journal.sqlite3"
                with closing(sqlite3.connect(path)) as db:
                    db.executescript("CREATE TABLE ownership(account,market,record); CREATE TABLE operations(account,market,status,intent);")
                    db.executemany("INSERT INTO operations VALUES(?,?,?,?)", [(scope().account, "BTC|", "UNKNOWN", body) for body in bodies])
                    db.commit()
                with self.assertRaises(ValueError): read_journal(path, scope(), limit=1)
