"""Local, explainable AI shadow journal for one Telegram trading profile.

This module never submits orders.  It records observations and virtual signals
so their outcomes can be measured before any later user-confirmed execution.
"""
import json
import os
import sqlite3
import time
from contextlib import closing


class AiAssistant:
    def __init__(self, root: str):
        self.path = os.path.join(root, "data", "ai_shadow.sqlite3")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def _init_db(self):
        with closing(self._connect()) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
              id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signals (
              id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
              coin TEXT NOT NULL, side TEXT NOT NULL, action TEXT NOT NULL,
              entry_price REAL NOT NULL, confidence REAL NOT NULL, reason TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'SHADOW', outcome_pnl REAL,
              closed_ms INTEGER, meta TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS signals_user_created ON signals(user_id, created_ms DESC);
            """)
            db.commit()

    def observe(self, user_id: int, positions: list[dict]):
        """Keep a compact position snapshot every observer cycle."""
        payload = [{key: item.get(key) for key in ("coin", "side", "entry_price", "position_value", "unrealized_pnl", "leverage", "roe")}
                   for item in positions]
        with closing(self._connect()) as db:
            db.execute("INSERT INTO observations(user_id,created_ms,payload) VALUES(?,?,?)",
                       (str(user_id), int(time.time() * 1000), json.dumps(payload)))
            # Keep the lightweight store bounded: the newest 90 days of snapshots.
            db.execute("DELETE FROM observations WHERE created_ms < ?", (int((time.time() - 90 * 86400) * 1000),))
            db.commit()

    def shadow_signal(self, user_id: int, position: dict, action: str, confidence: float, reason: str):
        """Create one virtual signal only if the same actionable signal is not fresh."""
        now = int(time.time() * 1000)
        coin, side = str(position.get("coin") or ""), str(position.get("side") or "")
        with closing(self._connect()) as db:
            old = db.execute("SELECT id FROM signals WHERE user_id=? AND coin=? AND action=? AND status='SHADOW' AND created_ms>?",
                             (str(user_id), coin, action, now - 4 * 3600 * 1000)).fetchone()
            if old:
                return None
            cur = db.execute("""INSERT INTO signals(user_id,created_ms,coin,side,action,entry_price,confidence,reason,meta)
                                VALUES(?,?,?,?,?,?,?,?,?)""",
                             (str(user_id), now, coin, side, action, float(position.get("entry_price") or 0),
                              round(float(confidence), 1), reason, json.dumps({"shadow": True})))
            db.commit()
            return cur.lastrowid

    def evaluate_positions(self, user_id: int, positions: list[dict]):
        """Conservative shadow rules; measured outcomes become training data.

        The early system intentionally makes only risk-reduction suggestions.
        It does not manufacture new-entry signals until enough personal samples
        have been collected and validated.
        """
        # Retired rules assigned fixed 72/64 confidence without learning or
        # measuring forward outcomes. Preserve old records for audit only.
        # New studies are handled by AiReview/AiResearch, never fake confidence.
        return []

    def summary(self, user_id: int):
        with closing(self._connect()) as db:
            rows = db.execute("SELECT id,created_ms,coin,side,action,entry_price,confidence,reason,status,outcome_pnl,closed_ms FROM signals WHERE user_id=? ORDER BY created_ms DESC LIMIT 80", (str(user_id),)).fetchall()
            count = db.execute("SELECT COUNT(*) FROM observations WHERE user_id=?", (str(user_id),)).fetchone()[0]
        keys = ["id", "created_ms", "coin", "side", "action", "entry_price", "confidence", "reason", "status", "outcome_pnl", "closed_ms"]
        return {"observations": count, "signals": [dict(zip(keys, row)) for row in rows]}

    def global_training_stats(self):
        """Aggregate only counts/outcomes; no account identifiers or keys leave storage."""
        with closing(self._connect()) as db:
            observations = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            signals = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            closed = db.execute("SELECT COUNT(*) FROM signals WHERE outcome_pnl IS NOT NULL").fetchone()[0]
        return {"observations": observations, "signals": signals, "closed": closed}

    def readiness(self):
        """Return a conservative, reproducible gate for any future limited pilot.

        This is deliberately only an eligibility check.  It does not change a
        profile, submit an order, or turn an AI source on.  A future execution
        component must consume this gate explicitly and remain opt-in.
        """
        now = int(time.time() * 1000)
        with closing(self._connect()) as db:
            observations, first_seen = db.execute(
                "SELECT COUNT(*), MIN(created_ms) FROM observations"
            ).fetchone()
            closed, total_pnl, worst_pnl = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(outcome_pnl), 0), COALESCE(MIN(outcome_pnl), 0) "
                "FROM signals WHERE outcome_pnl IS NOT NULL"
            ).fetchone()
        days = 0 if not first_seen else (now - first_seen) / 86_400_000
        requirements = {
            "history_days": {"actual": round(days, 1), "minimum": 30, "ok": days >= 30},
            "observations": {"actual": observations, "minimum": 5000, "ok": observations >= 5000},
            "resolved_virtual": {"actual": closed, "minimum": 100, "ok": closed >= 100},
            "net_virtual_pnl": {"actual": round(float(total_pnl), 2), "minimum": 0, "ok": float(total_pnl) > 0},
            "worst_virtual_result": {"actual": round(float(worst_pnl), 2), "minimum": -25, "ok": float(worst_pnl) >= -25},
        }
        return {"ready": False, "requirements": requirements,
                "reason":"Legacy counts do not establish action-specific validated probability or live execution readiness"}
