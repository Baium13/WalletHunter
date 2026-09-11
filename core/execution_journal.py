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

    def pending_intents(self, account):
        """Read unresolved reservation evidence without changing order state.

        Invalid JSON is an error, never evidence of zero reserved capital.
        Account scoping is identical to pending() and owned().
        """
        with closing(self.connect()) as db:
            rows = db.execute("SELECT market,intent FROM operations WHERE account=? AND status IN ('PREPARED','UNKNOWN')",
                              (account.lower(),)).fetchall()
        intents = {row["market"]: json.loads(row["intent"]) for row in rows}
        if len(intents) != len(rows):
            raise ValueError("Multiple unresolved intents for one market require reconciliation")
        return intents

    def prepare(self, account, market, intent):
        intent = dict(intent)
        network = intent.get("network", "LEGACY_UNKNOWN")
        if network not in {"MAINNET", "TESTNET", "LEGACY_UNKNOWN"}:
            raise ValueError("Invalid execution network identity")
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
            links = json.loads(row['intent']).get('canonical_intents', [])
            if links and not result.get('ok'):
                states = [db.execute('SELECT status FROM intents WHERE id=?', (identity,)).fetchone() for identity in links]
                if all(state and state[0] == 'REJECTED' for state in states): status = 'REJECTED'
            db.execute("UPDATE operations SET status=?,updated=?,outcome=? WHERE id=?",
                       (status, time.time(), json.dumps(result), oid))
            if ownership is not None:
                network = json.loads(row["intent"]).get("network", "LEGACY_UNKNOWN")
                if network != "LEGACY_UNKNOWN": ownership = dict(ownership, network=network)
                stamp = (ownership.get("position") or {}).get("snapshot_started_ms") or int(time.time() * 1000)
                ownership = dict(ownership, verified_at_ms=stamp)
                db.execute("INSERT OR REPLACE INTO ownership VALUES(?,?,?,?,?)",
                           (row["account"], row["market"], time.time(), oid, json.dumps(ownership)))
            db.commit()

    def owned(self, account):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT market,record FROM ownership WHERE account=?", (account.lower(),)).fetchall()
        return {r["market"]: json.loads(r["record"]) for r in rows}

    def resolve_canonical_copy(self, intent_id):
        """Recover only a linked, proven full fill; never infer it from a delta."""
        from core.foundation.contracts import OrderIntent, ExecutionReceipt, PortfolioSnapshot
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM intents WHERE id=?', (intent_id,)).fetchone()
            if not row or row['status'] != 'FILLED': return False
            intent = OrderIntent.model_validate_json(row['body'])
            receipt = ExecutionReceipt.model_validate_json(row['receipt'])
            parent = db.execute('SELECT * FROM operations WHERE id=?', (intent.parent_intent_id,)).fetchone()
            if not parent or parent['status'] not in {'PREPARED','UNKNOWN'}: return False
            envelope = json.loads(parent['intent'])
            if (intent.version != 2 or receipt.provenance != 'EXCHANGE' or not receipt.fills
                    or parent['account'] != intent.scope.account or envelope.get('network') != intent.scope.network
                    or intent_id not in envelope.get('canonical_intents', [])): return False
            siblings = [db.execute('SELECT status FROM intents WHERE id=?', (i,)).fetchone()
                for i in envelope['canonical_intents'] if i != intent_id]
            if any(not s or s[0] not in {'FILLED','REJECTED','CONFIGURED'} for s in siblings): return False
            snap = db.execute('SELECT body FROM portfolios WHERE scope=?', (row['scope'],)).fetchone()
            if not snap: return False
            portfolio = PortfolioSnapshot.model_validate_json(snap[0])
            if portfolio.scope != intent.scope or portfolio.received_ms > receipt.received_ms: return False
            p = next((p for p in portfolio.positions if p.instrument == intent.instrument), None)
            position = None if p is None else dict(coin=intent.instrument.market_key.split('|')[0],
                dex=intent.instrument.dex,side=p.side,size=p.size,entry_price=p.entry_price,position_value=p.notional,
                leverage=p.leverage,margin_used=p.margin,snapshot_started_ms=portfolio.received_ms)
            evidence = {'intent_id': intent_id, 'order_ids': list(receipt.order_ids),
                'trade_ids': [f.trade_id for f in receipt.fills], 'network': intent.scope.network}
            record = dict(managed=p is not None, size=p.size if p else 0.,side=p.side if p else '',position=position,
                network=intent.scope.network,verified_at_ms=portfolio.received_ms,execution_evidence=evidence,
                source_targets=[dict(wallet=c.source,margin=c.target_margin,
                    signed_notional=c.target_notional*(1 if p and p.side=='LONG' else -1)) for c in intent.source_contributions],
                attribution='strategy_targets_not_individual_exchange_fills')
            db.execute('UPDATE operations SET status=?,updated=?,outcome=? WHERE id=?', ('CONFIRMED',time.time(),
                json.dumps({'ok':True,'action':intent.action,'canonical_recovery':intent_id,'execution_evidence':evidence}),parent['id']))
            db.execute('INSERT OR REPLACE INTO ownership VALUES(?,?,?,?,?)',
                (parent['account'],parent['market'],time.time(),parent['id'],json.dumps(record)))
            db.commit()
            return True
