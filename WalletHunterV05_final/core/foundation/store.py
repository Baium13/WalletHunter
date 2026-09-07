"""Explicitly provisioned SQLite core database; never opens legacy state.

Events, intents, reservations and local consumer offsets share transactions.
Replay exposes data, not authorization. External notification delivery is at-least-once.
"""
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .contracts import DomainEvent, PortfolioSnapshot, Scope


def encoded(value):
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def scope_key(scope):
    return encoded(scope)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("Core storage symlinks forbidden")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        os.chmod(self.path, 0o600)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS core_meta(version INTEGER PRIMARY KEY CHECK(version=1));
                INSERT OR IGNORE INTO core_meta VALUES(1);
                CREATE TABLE IF NOT EXISTS owners(network TEXT,account TEXT,tenant TEXT,PRIMARY KEY(network,account));
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,
                    scope TEXT NOT NULL,body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS scoped_events ON events(scope,seq);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TABLE IF NOT EXISTS portfolios(scope TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS consumers(scope TEXT,name TEXT,seq INTEGER NOT NULL,PRIMARY KEY(scope,name));
                CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY,scope TEXT NOT NULL,body TEXT NOT NULL,
                    status TEXT NOT NULL,decision TEXT,reservation TEXT,receipt TEXT);
                CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY,scope TEXT NOT NULL,intent_hash TEXT NOT NULL);
            """)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def bind(db, scope):
        row = db.execute("SELECT tenant FROM owners WHERE network=? AND account=?", (scope.network, scope.account)).fetchone()
        if row and row[0] != scope.tenant:
            raise ValueError("Account already bound to another tenant")
        db.execute("INSERT OR IGNORE INTO owners VALUES(?,?,?)", (scope.network, scope.account, scope.tenant))

    def append_in(self, db, event):
        self.bind(db, event.scope)
        body = encoded(event)
        old = db.execute("SELECT seq,body FROM events WHERE id=?", (event.event_id,)).fetchone()
        if old:
            if old["body"] != body: raise ValueError("Event identity collision")
            return old["seq"]
        return db.execute("INSERT INTO events(id,scope,body) VALUES(?,?,?)",
            (event.event_id, scope_key(event.scope), body)).lastrowid

    def append(self, event):
        with self.transaction() as db: return self.append_in(db, event)

    def replay(self, scope, after=0, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500 or type(after) is not int or after < 0:
            raise ValueError("Bounded replay required")
        with self.transaction() as db:
            rows = db.execute("SELECT seq,body FROM events WHERE scope=? AND seq>? ORDER BY seq LIMIT ?",
                (scope_key(scope), after, limit)).fetchall()
        return tuple((r["seq"], DomainEvent.model_validate_json(r["body"])) for r in rows)

    def consume(self, scope, name, handler, limit=100):
        """Handler must only project into this transaction; never execute orders."""
        if type(limit) is not int or not 1 <= limit <= 500: raise ValueError("Bounded consumer required")
        with self.transaction() as db:
            old = db.execute("SELECT seq FROM consumers WHERE scope=? AND name=?", (scope_key(scope), name)).fetchone()
            after = old[0] if old else 0
            rows = db.execute("SELECT seq,body FROM events WHERE scope=? AND seq>? ORDER BY seq LIMIT ?",
                (scope_key(scope), after, limit)).fetchall()
            for row in rows:
                handler(db, DomainEvent.model_validate_json(row["body"]))
            if rows:
                db.execute("INSERT INTO consumers VALUES(?,?,?) ON CONFLICT(scope,name) DO UPDATE SET seq=excluded.seq",
                    (scope_key(scope), name, rows[-1]["seq"]))
            return len(rows)

    def portfolio_in(self, db, scope):
        row = db.execute("SELECT body FROM portfolios WHERE scope=?", (scope_key(scope),)).fetchone()
        if not row: raise ValueError("Portfolio unavailable")
        return PortfolioSnapshot.model_validate_json(row[0])

    def portfolio(self, scope):
        with self.transaction() as db: return self.portfolio_in(db, scope)

    def publish_portfolio_in(self, db, snapshot, correlation_id, event_id=None):
        self.bind(db, snapshot.scope)
        row = db.execute("SELECT body FROM portfolios WHERE scope=?", (scope_key(snapshot.scope),)).fetchone()
        if row:
            previous = PortfolioSnapshot.model_validate_json(row[0])
            if previous == snapshot: return False
            if snapshot.revision <= previous.revision or snapshot.received_ms < previous.received_ms:
                raise ValueError("Out-of-order portfolio")
            if previous.exchange_ms is not None and snapshot.exchange_ms is not None and snapshot.exchange_ms < previous.exchange_ms:
                raise ValueError("Exchange watermark regression")
        event = DomainEvent(event_id=event_id or digest(snapshot), event_type="PORTFOLIO_SNAPSHOT", correlation_id=correlation_id,
            scope=snapshot.scope, event_ms=snapshot.exchange_ms if snapshot.exchange_ms is not None else snapshot.received_ms,
            received_ms=snapshot.received_ms, payload=snapshot)
        self.append_in(db, event)
        db.execute("INSERT INTO portfolios VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET body=excluded.body",
            (scope_key(snapshot.scope), encoded(snapshot)))
        return True

    def publish_portfolio(self, snapshot, correlation_id):
        with self.transaction() as db: return self.publish_portfolio_in(db, snapshot, correlation_id)


class ReplayReader:
    """Read-only dependency for future observers; no gateway or credential handle."""
    def __init__(self, store, scope):
        self.__store, self.__scope = store, scope

    def read(self, after=0, limit=100):
        return self.__store.replay(self.__scope, after, limit)
