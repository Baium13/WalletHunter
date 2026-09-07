"""Background PUBLIC-only preparation of individually confirmed AI entries.

This module cannot submit or confirm an order. Notification IDs are claimed
durably BEFORE delivery: at-most-once attempts survive a restart; a Telegram
delivery failure can lose that notice, but the form remains available in APP.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
import os
import re
import sqlite3
import time

from core.ai_review import account_guard


class AiEntryObserver:
    def __init__(self, root, storage, orders, public_factory, reader, learning):
        self.root = os.path.abspath(root)
        self.storage, self.orders = storage, orders
        self.public_factory, self.reader, self.learning = public_factory, reader, learning
        self.path = os.path.join(self.root, "data", "ai_entry_observer.sqlite3")
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        if os.path.islink(self.path):
            raise ValueError("Entry notification database cannot be a symlink")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        os.chmod(self.path, 0o600)
        with closing(self._connect()) as db:
            db.execute("CREATE TABLE IF NOT EXISTS entry_notice_claims("
                       "user_id TEXT NOT NULL,account TEXT NOT NULL,proposal_id TEXT NOT NULL,"
                       "claimed_ms INTEGER NOT NULL,PRIMARY KEY(user_id,account,proposal_id))")
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _eligible(profile, notice=False):
        if not isinstance(profile, dict):
            return False
        account = profile.get("account")
        address = account.get("address") if isinstance(account, dict) else None
        leaders = profile.get("leaders")
        return bool(isinstance(address, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", address)
                    and profile.get("ai_slot_selected") is True
                    and profile.get("ai_trader_enabled") is True
                    and isinstance(leaders, list) and len(leaders) <= 2
                    and profile.get("crypto_enabled", True) is True
                    and (not notice or profile.get("notifications", True) is True))

    @contextmanager
    def _current(self, uid, *, notice=False):
        uid = int(uid)
        with account_guard(self.root, f"telegram-profile:{uid}"):
            _, initial = self.storage.profile(uid)
            if not self._eligible(initial, notice):
                yield initial, None
                return
            account = deepcopy(initial["account"])
            with account_guard(self.root, account["address"]):
                _, fresh = self.storage.profile(uid)
                if fresh.get("account") != account or not self._eligible(fresh, notice):
                    yield fresh, None
                    return
                yield fresh, account["address"].lower()

    def prepare(self, uid):
        """Prepare a form only; even an eligible learned signal cannot execute."""
        with self._current(uid) as (profile, address):
            if address is None:
                return None
            public_client = self.public_factory(profile)
            now = int(time.time() * 1000)
            learned = self.learning.summary(now_ms=now)
            return self.orders.prepare(uid, profile, public_client, self.reader, learned, now)

    @staticmethod
    def _pending(summary, now):
        if not isinstance(summary, dict) or not isinstance(summary.get("pending"), list):
            raise ValueError("Entry form summary is unavailable")
        out = []
        for row in summary["pending"]:
            if not isinstance(row, dict):
                continue
            payload = row.get("payload")
            created, expires = row.get("created_ms"), row.get("expires_ms")
            if (row.get("status") != "PENDING" or not isinstance(row.get("id"), str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", row["id"]) is None
                    or type(created) is not int or type(expires) is not int or not 0 < created <= now < expires
                    or not isinstance(payload, dict) or payload.get("action") != "OPEN"
                    or payload.get("coin") not in ("BTC", "ETH")
                    or payload.get("direction") not in ("LONG", "SHORT")):
                continue
            out.append(deepcopy(row))
        return out

    def notices(self, uid):
        """Fresh unclaimed forms plus the current language/profile; no network."""
        with self._current(uid, notice=True) as (profile, address):
            if address is None:
                return [], profile
            now = int(time.time() * 1000)
            rows = self._pending(self.orders.summary(uid, profile, now), now)
            with closing(self._connect()) as db:
                claimed = {row[0] for row in db.execute(
                    "SELECT proposal_id FROM entry_notice_claims WHERE user_id=? AND account=?", (str(int(uid)), address))}
            return [row for row in rows if row["id"] not in claimed], profile

    def claim_notice(self, uid, proposal_id):
        """Recheck the form, then atomically reserve this single send attempt."""
        if not isinstance(proposal_id, str):
            return False
        with self._current(uid, notice=True) as (profile, address):
            if address is None:
                return False
            now = int(time.time() * 1000)
            rows = self._pending(self.orders.summary(uid, profile, now), now)
            if not any(row["id"] == proposal_id for row in rows):
                return False
            with closing(self._connect()) as db:
                changed = db.execute("INSERT OR IGNORE INTO entry_notice_claims VALUES(?,?,?,?)",
                                     (str(int(uid)), address, proposal_id, now)).rowcount
                db.commit()
            return changed == 1


def entry_notice(row, english=False):
    """A short notification without private balance/quantity or success claims."""
    payload = row.get("payload", {}) if isinstance(row, dict) else {}
    coin = payload.get("coin") if payload.get("coin") in ("BTC", "ETH") else "—"
    direction = payload.get("direction")
    side = ("Long" if english else "Лонг") if direction == "LONG" else (
        ("Short" if english else "Шорт") if direction == "SHORT" else "—")
    if english:
        return (f"🧠 AI ENTRY FORM · {coin} · {side}\n"
                "A new order form is ready. Nothing was executed.\n"
                "In APP → AI, check the amount, leverage and risks; confirm or decline.\n"
                "The research score is not a verified success probability. If expired, prepare a fresh form.")
    return (f"🧠 AI · ФОРМА ВХОДА · {coin} · {side}\n"
            "Подготовлена форма нового ордера. Ничего не исполнено.\n"
            "В APP → AI проверьте сумму, плечо и риски; подтвердите или отклоните.\n"
            "Оценка модели — не проверенная вероятность успеха. Если срок истёк, подготовьте свежую форму.")
