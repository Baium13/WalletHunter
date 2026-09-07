"""Durable order intents and strategy provenance, independent of chat cleanup."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing


class ExecutionJournal:
    def __init__(self, root):
        self.path = os.path.join(root, "data", "executions.sqlite3")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, market TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, status TEXT NOT NULL,
                intent TEXT NOT NULL, outcome TEXT);
              CREATE TABLE IF NOT EXISTS ownership (
                account TEXT NOT NULL, market TEXT NOT NULL, updated REAL NOT NULL,
                operation_id TEXT NOT NULL, record TEXT NOT NULL,
                PRIMARY KEY(account,market));
              CREATE INDEX IF NOT EXISTS operation_account ON operations(account,created);
            """)
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def pending(self, account):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT market FROM operations WHERE account=? AND status IN ('PREPARED','UNKNOWN')",
                              (account.lower(),)).fetchall()
        return {row["market"] for row in rows}

    def prepare(self, account, market, intent):
        now, oid = time.time(), uuid.uuid4().hex
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM operations WHERE account=? AND market=? AND status IN ('PREPARED','UNKNOWN')",
                          (account.lower(), market)).fetchone():
                raise RuntimeError("Unresolved execution: reconcile exchange evidence before retry")
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,NULL)",
                       (oid, account.lower(), market, now, now, "PREPARED", json.dumps(intent)))
            db.commit()
        return oid

    def finish(self, oid, result, ownership=None):
        status = "CONFIRMED" if result.get("ok") else "UNKNOWN"
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE id=?", (oid,)).fetchone()
            if not row or row["status"] != "PREPARED": raise ValueError("Operation is not pending")
            db.execute("UPDATE operations SET status=?,updated=?,outcome=? WHERE id=?",
                       (status, time.time(), json.dumps(result), oid))
            if ownership is not None:
                stamp = (ownership.get("position") or {}).get("snapshot_started_ms") or int(time.time() * 1000)
                ownership = dict(ownership, verified_at_ms=stamp)
                db.execute("INSERT OR REPLACE INTO ownership VALUES(?,?,?,?,?)",
                           (row["account"], row["market"], time.time(), oid, json.dumps(ownership)))
            db.commit()

    def owned(self, account):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT market,record FROM ownership WHERE account=?", (account.lower(),)).fetchall()
        return {r["market"]: json.loads(r["record"]) for r in rows}
