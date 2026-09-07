"""Immutable, explicitly user-confirmed single IOC entry forms.

The caller MUST hold the profile and exchange-account guards and supply a fresh
profile. prepare/summary/decline never construct a signing client. Only decide
with the literal boolean True may call signing_factory, after fresh validation
and durable intent/hold persistence. No background execution or retry exists.

Public client contract: address, capital_snapshot(), available_margin(dex),
positions(True,True), frontend_open_orders(dex), meta(), mid(coin,dex).
Signing client: submit_user_ioc(coin,is_buy,size,limit_price,leverage,cloid).
That wrapper must verify the leverage acknowledgement before submitting IOC.
persist_runtime is a zero-argument closure committing profile['runtime'].
"""
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid

from core.execution_journal import ExecutionJournal
from core.ai_entry_policy import ENTRY_MARGIN_FRACTION, MAX_ENTRY_LEVERAGE, ENTRY_POLICY_VERSION
from core.order_precision import normalize_perp_price, normalize_perp_size


ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
RESERVED = ("SUBMITTING", "FILLED", "PARTIAL", "UNKNOWN")
TERMINAL = ("DECLINED", "EXPIRED", "INVALIDATED", "FILLED", "PARTIAL", "UNKNOWN", "REJECTED")


def _number(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"invalid_{name}")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"invalid_{name}")
    return result


def _time(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("invalid_time")
    return value


def _json(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _address(profile):
    account = profile.get("account") if isinstance(profile, dict) else None
    address = account.get("address") if isinstance(account, dict) else None
    if not isinstance(address, str) or not ADDRESS.fullmatch(address):
        raise ValueError("account_missing")
    return address.lower()


def _profile_gate(profile):
    address = _address(profile)
    if profile.get("ai_slot_selected") is not True:
        raise ValueError("ai_slot_unavailable")
    if profile.get("crypto_enabled", True) is not True:
        raise ValueError("crypto_disabled")
    leaders = profile.get("leaders")
    if not isinstance(leaders, list) or len(leaders) > 2:
        raise ValueError("too_many_wallets")
    runtime = profile.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("invalid_runtime")
    return address


def _snapshot(client, address):
    if str(getattr(client, "address", "")).lower() != address:
        raise ValueError("public_account_mismatch")
    snapshot = client.capital_snapshot()
    mode = snapshot.get("mode") if isinstance(snapshot, dict) else getattr(snapshot, "mode", None)
    capital = snapshot.get("sizing_base_usdc") if isinstance(snapshot, dict) else getattr(snapshot, "sizing_base_usdc", None)
    if mode not in ("unifiedAccount", "disabled"):
        raise ValueError("unsupported_capital_mode")
    return mode, _number(capital, "sizing_capital")


def _network(client):
    mode = getattr(client, "mode", None)
    if mode in ("MAINNET", "TESTNET"):
        return mode
    base = str(getattr(client, "base", "")).rstrip("/")
    if base == "https://api.hyperliquid.xyz":
        return "MAINNET"
    if base == "https://api.hyperliquid-testnet.xyz":
        return "TESTNET"
    raise ValueError("network_unavailable")


def _slot_fingerprint(profile):
    fields = ("leaders", "ai_slot_selected", "crypto_enabled", "max_leverage", "risk_multiplier",
              "risk_profile", "strategy_mode", "risk_mode", "execution_mode", "mode")
    return hashlib.sha256(_json({field: profile.get(field) for field in fields}).encode()).hexdigest()


def _profile_leverage(profile, market_limit):
    configured = profile.get("max_leverage")
    if configured is None:
        return market_limit
    number = _number(configured, "profile_leverage", 1)
    if int(number) != number:
        raise ValueError("invalid_profile_leverage")
    return min(market_limit, int(number))


def _metadata(client, coin):
    meta = client.meta()
    if not isinstance(meta, dict) or not isinstance(meta.get("universe"), list):
        raise ValueError("market_metadata_unavailable")
    rows = [row for row in meta["universe"] if isinstance(row, dict) and row.get("name") == coin]
    if len(rows) != 1:
        raise ValueError("market_metadata_ambiguous")
    row = rows[0]
    if row.get("isDelisted") or row.get("onlyIsolated") or row.get("marginMode") in ("noCross", "strictIsolated"):
        raise ValueError("market_mode_unsupported")
    digits, leverage = row.get("szDecimals"), row.get("maxLeverage")
    if isinstance(digits, bool) or not isinstance(digits, int) or not 0 <= digits <= 6:
        raise ValueError("market_precision_invalid")
    if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage < 1:
        raise ValueError("market_leverage_invalid")
    return digits, min(MAX_ENTRY_LEVERAGE, leverage)


def _positions(client, coin):
    rows = client.positions(True, True)
    if not isinstance(rows, list):
        raise ValueError("position_snapshot_unavailable")
    matching = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("coin"), str):
            raise ValueError("position_snapshot_invalid")
        size = _number(row.get("size"), "position_size")
        if row["coin"] == coin and not row.get("dex") and size > 0:
            matching.append(row)
    if len(matching) > 1:
        raise ValueError("position_snapshot_ambiguous")
    return matching


def _orders(client, coin):
    rows = client.frontend_open_orders("")
    if not isinstance(rows, list):
        raise ValueError("open_orders_unavailable")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("coin"), str):
            raise ValueError("open_orders_invalid")
    return [row for row in rows if row["coin"] == coin]


class AiUserOrders:
    def __init__(self, root, *, monotonic=time.monotonic):
        self.monotonic = monotonic
        self.journal = ExecutionJournal(root)
        self.path = os.path.join(root, "data", "ai_user_orders.sqlite3")
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        if os.path.islink(self.path):
            raise ValueError("Order form database cannot be a symlink")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        os.chmod(self.path, 0o600)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS user_order_proposals(
                    id TEXT PRIMARY KEY,user_id TEXT NOT NULL,account TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,created_ms INTEGER NOT NULL,expires_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,payload TEXT NOT NULL,payload_hash TEXT NOT NULL,
                    operation_id TEXT,result TEXT,updated_ms INTEGER NOT NULL,
                    UNIQUE(user_id,account,fingerprint));
                CREATE INDEX IF NOT EXISTS user_order_account ON user_order_proposals(user_id,account,created_ms);
                CREATE TABLE IF NOT EXISTS user_order_status(
                    user_id TEXT NOT NULL,account TEXT NOT NULL,reason TEXT NOT NULL,
                    updated_ms INTEGER NOT NULL,PRIMARY KEY(user_id,account));
            """)
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _row(row):
        payload = row["payload"]
        if hashlib.sha256(payload.encode()).hexdigest() != row["payload_hash"]:
            raise ValueError("immutable_proposal_corrupt")
        decoded = json.loads(payload)
        _json(decoded)
        return {"id": row["id"], "status": row["status"], "created_ms": row["created_ms"],
                "expires_ms": row["expires_ms"], "payload": decoded,
                "operation_id": row["operation_id"],
                "result": json.loads(row["result"]) if row["result"] else None}

    def _remember(self, uid, account, reason, now):
        with closing(self._connect()) as db:
            db.execute("INSERT INTO user_order_status VALUES(?,?,?,?) ON CONFLICT(user_id,account) "
                       "DO UPDATE SET reason=excluded.reason,updated_ms=excluded.updated_ms",
                       (str(uid), account, reason, now))
            db.commit()

    def summary(self, uid, profile, now_ms):
        now = _time(now_ms)
        try:
            address = _address(profile)
        except ValueError:
            return {"status": "UNAVAILABLE", "reason": "account_missing", "pending": [], "history": [],
                    "execution_mode": "USER_CONFIRMATION_ONLY", "automatic_execution": False}
        blocked = None
        try:
            _profile_gate(profile)
        except ValueError as error:
            blocked = str(error)
        with closing(self._connect()) as db:
            if blocked:
                db.execute("UPDATE user_order_proposals SET status='INVALIDATED',updated_ms=? WHERE user_id=? AND account=? AND status='PENDING'",
                           (now, str(uid), address))
            db.execute("UPDATE user_order_proposals SET status='EXPIRED',updated_ms=? WHERE user_id=? AND account=? "
                       "AND status='PENDING' AND expires_ms<=?", (now, str(uid), address, now))
            db.commit()
            rows = db.execute("SELECT * FROM user_order_proposals WHERE user_id=? AND account=? ORDER BY created_ms DESC LIMIT 20",
                              (str(uid), address)).fetchall()
            remembered = db.execute("SELECT reason FROM user_order_status WHERE user_id=? AND account=?", (str(uid), address)).fetchone()
        data = [self._row(row) for row in rows]
        pending = [row for row in data if row["status"] == "PENDING"]
        return {"status": "UNAVAILABLE" if blocked else ("PENDING" if pending else "IDLE"),
                "reason": blocked or ("confirmation_required" if pending else (remembered[0] if remembered else "no_signal")),
                "pending": pending, "history": [row for row in data if row["status"] != "PENDING"],
                "execution_mode": "USER_CONFIRMATION_ONLY", "automatic_execution": False}

    def reserved_markets(self, uid, account):
        """Durable engine exclusion even if a runtime JSON write was interrupted."""
        with closing(self._connect()) as db:
            if uid is None:
                rows = db.execute("SELECT * FROM user_order_proposals WHERE account=? "
                                  "AND status IN ('SUBMITTING','FILLED','PARTIAL','UNKNOWN') ORDER BY created_ms", (account.lower(),)).fetchall()
            else:
                rows = db.execute("SELECT * FROM user_order_proposals WHERE user_id=? AND account=? "
                                  "AND status IN ('SUBMITTING','FILLED','PARTIAL','UNKNOWN') ORDER BY created_ms",
                                  (str(uid), account.lower())).fetchall()
        return {f"{self._row(row)['payload']['coin']}|": {"proposal_id": row["id"], "status": row["status"],
                "operation_id": row["operation_id"]} for row in rows}

    def reconcile_closed(self, uid, profile, client, persist_runtime, now_ms):
        """Caller holds profile/account guard. Release only proven flat entries.

        SUBMITTING/UNKNOWN never qualify. A SQL-first release is idempotent:
        subsequent runs repair only this proposal's stale JSON holds.
        """
        address = _address(profile)
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM user_order_proposals WHERE user_id=? AND account=? AND status IN ('FILLED','PARTIAL','RELEASED')",
                              (str(uid), address)).fetchall()
        for raw in rows:
            row = self._row(raw)
            payload, key = row["payload"], f"{row['payload']['coin']}|"
            if payload.get("network") != _network(client):
                continue
            if row["status"] != "RELEASED":
                try:
                    if key in self.journal.pending(address) or _positions(client, payload["coin"]) or _orders(client, payload["coin"]):
                        continue
                    verify = getattr(client, "verify_ai_closed", None)
                    if not callable(verify): continue
                    evidence = verify(dict(payload, created_ms=row["created_ms"]), row["result"] or {})
                    if not isinstance(evidence, dict) or evidence.get("status") != "CLOSED_RECONCILED":
                        continue
                    result = dict(row["result"] or {}, closure=evidence, released_ms=now_ms)
                    with closing(self._connect()) as db:
                        db.execute("UPDATE user_order_proposals SET status='RELEASED',result=?,updated_ms=? WHERE id=? AND user_id=? AND account=? AND status IN ('FILLED','PARTIAL')",
                                   (_json(result), now_ms, row["id"], str(uid), address))
                        db.commit()
                except Exception:
                    self._remember(uid, address, "ai_position_reconciliation_required", now_ms)
                    continue
            runtime = profile.setdefault("runtime", {})
            holds = runtime.get("ai_user_order_holds", {})
            if isinstance(holds, dict) and (holds.get(key) or {}).get("proposal_id") == row["id"]:
                holds.pop(key)
                marker = runtime.get("ai_user_order_positions", {}).get(key)
                if isinstance(marker, dict) and marker.get("proposal_id") == row["id"]:
                    marker["status"] = "RELEASED"
                persist_runtime()

    def _clear_market(self, uid, profile, client, address, coin, *, own_id=None):
        key = f"{coin}|"
        all_reserved = self.reserved_markets(None, address)
        if self.journal.pending(address) or any(item["status"] in ("UNKNOWN", "SUBMITTING") for item in all_reserved.values()):
            raise ValueError("execution_pending")
        if _positions(client, coin):
            raise ValueError("position_conflict")
        if _orders(client, coin):
            raise ValueError("open_order_conflict")
        runtime = profile.get("runtime", {})
        if (key in runtime.get("managed", []) or key in runtime.get("manual_hold", {}) or key in runtime.get("manual_holds", {})
            or key in runtime.get("manual_hold_keys", {}) or key in runtime.get("ai_hold_keys", {})
            or key in runtime.get("recovery_required", []) or key in runtime.get("detached_keys", [])):
            raise ValueError("instrument_owned_or_held")
        paused = runtime.get("paused_source_markets") or {}
        if not isinstance(paused, dict) or any(key in keys for keys in paused.values()):
            raise ValueError("instrument_owned_or_held")
        if str((runtime.get("manual_actions", {}).get(key) or {}).get("status", "")).lower() in ("submitting", "unknown"):
            raise ValueError("instrument_owned_or_held")
        if key in (runtime.get("ai_user_order_holds") or {}):
            raise ValueError("instrument_owned_or_held")
        from core.ai_position_actions import AiPositionActions
        if (key in (runtime.get("ai_position_action_holds") or {})
                or key in AiPositionActions(os.path.dirname(os.path.dirname(self.path))).reserved_markets(None, address)):
            raise ValueError("instrument_owned_or_held")
        owned = self.journal.owned(address).get(key) or {}
        if owned.get("managed"):
            raise ValueError("instrument_owned_or_held")
        reserved = all_reserved.get(key)
        if reserved and reserved.get("proposal_id") != own_id:
            raise ValueError("instrument_owned_or_held")

    def _reserved_margin(self, address, client):
        reserved = 0.0
        for key, record in self.reserved_markets(None, address).items():
            if record["status"] not in ("FILLED", "PARTIAL"):
                raise ValueError("execution_pending")
            rows = _positions(client, key.split("|", 1)[0])
            if len(rows) != 1:
                raise ValueError("ai_position_reconciliation_required")
            position = rows[0]
            reserved += _number(position.get("margin_used"), "existing_ai_margin")
        return _number(reserved, "total_ai_reserved")

    @staticmethod
    def _candidate(prediction, now):
        if not isinstance(prediction, dict) or prediction.get("coin") not in ("BTC", "ETH") or prediction.get("direction") not in ("LONG", "SHORT"):
            return None
        if prediction.get("status") != "PENDING":
            return None
        try:
            created, decision, deadline = (_time(prediction.get(key)) for key in ("created_ms", "decision_ms", "deadline_ms"))
            entry = _time(prediction.get("entry_ms"))
            probability = _number(prediction.get("probability_positive_net"), "model_score")
            version = prediction.get("model_version")
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                return None
            if not .6 <= probability <= 1 or not 0 <= now-created <= 120000 or not decision < created <= now < deadline:
                return None
            if now-decision > 3600000:
                return None
            if (decision + 1) % 3600000 or entry != decision + 900001 or deadline != decision + 4500000 or created >= entry:
                return None
        except (ValueError, TypeError):
            return None
        return dict(prediction, probability_positive_net=probability)

    def prepare(self, uid, profile, public_client, reader, learning_summary, now_ms):
        """Read-only preparation; reader is retained for the caller's API shape."""
        now = _time(now_ms)
        started = self.monotonic()
        try:
            address = _profile_gate(profile)
        except ValueError as exc:
            result = self.summary(uid, profile, now)
            result.update(status="UNAVAILABLE", reason=str(exc))
            return result
        existing = self.summary(uid, profile, now)
        if existing["pending"]:
            return existing
        if not isinstance(learning_summary, dict) or learning_summary.get("status") != "RESEARCH_MODEL" or not learning_summary.get("model"):
            self._remember(uid, address, "model_unavailable", now)
            return self.summary(uid, profile, now)
        collector = learning_summary.get("collector")
        if collector is not None and (not isinstance(collector, dict) or collector.get("status") != "OK"):
            self._remember(uid, address, "market_data_unavailable", now)
            return self.summary(uid, profile, now)
        predictions = learning_summary.get("predictions", [])
        if not isinstance(predictions, list) or len(predictions) > 100:
            self._remember(uid, address, "model_unavailable", now)
            return self.summary(uid, profile, now)
        candidates = [candidate for row in predictions
                      if (candidate := self._candidate(row, now)) is not None]
        candidates.sort(key=lambda row: (-row["probability_positive_net"], row["coin"], row["direction"]))
        reason = "no_signal"
        for candidate in candidates:
            coin, side = candidate["coin"], candidate["direction"]
            fingerprint = hashlib.sha256(_json({"coin": coin, "side": side, "decision_ms": candidate["decision_ms"]}).encode()).hexdigest()
            with closing(self._connect()) as db:
                if db.execute("SELECT 1 FROM user_order_proposals WHERE user_id=? AND account=? AND fingerprint=?",
                              (str(uid), address, fingerprint)).fetchone():
                    reason = "signal_already_handled"
                    continue
            try:
                mode, balance = _snapshot(public_client, address)
                network = _network(public_client)
                self._clear_market(uid, profile, public_client, address, coin)
                digits, leverage = _metadata(public_client, coin)
                leverage = _profile_leverage(profile, leverage)
                reference = _number(public_client.mid(coin, ""), "reference_price", 1e-12)
                direction = 1 if side == "LONG" else -1
                limit = normalize_perp_price(reference * (1 + direction * .005), digits)
                sizing_price = max(reference * 1.005, limit)
                share = balance / 3
                already_reserved = self._reserved_margin(address, public_client)
                margin_cap = share * ENTRY_MARGIN_FRACTION
                size = normalize_perp_size(margin_cap * leverage / sizing_price, digits)
                notional = size * reference
                maximum_expected_notional = size * sizing_price
                margin = notional / leverage
                expected_margin_cap = maximum_expected_notional / leverage
                fee = maximum_expected_notional * .0005
                if size <= 0 or min(size * limit, notional) < 10:
                    raise ValueError("minimum_notional")
                capacity = _number(public_client.available_margin(""), "available_margin")
                if expected_margin_cap + fee > capacity:
                    raise ValueError("insufficient_capacity")
                if already_reserved + expected_margin_cap + fee > share:
                    raise ValueError("ai_slot_budget_exhausted")
                payload = {"coin": coin, "dex": "", "direction": side, "side": side, "action": "OPEN", "order_type": "LIMIT_IOC",
                    "size": size, "limit_price": limit, "reference_price": reference, "sizing_price": sizing_price,
                    "size_text": format(Decimal(str(size)), "f"), "limit_price_text": format(Decimal(str(limit)), "f"),
                    "sz_decimals": digits, "leverage": leverage, "margin_mode": "cross", "sizing_mode": mode,
                    "network": network, "slot_fingerprint": _slot_fingerprint(profile),
                    "balance_usdc": balance, "share_usdc": share, "share_fraction": 1/3,
                    "entry_pct_of_share": ENTRY_MARGIN_FRACTION * 100, "entry_policy_version": ENTRY_POLICY_VERSION,
                    "margin_estimate_usdc": margin, "margin_cap_usdc": margin_cap,
                    "maximum_expected_margin_usdc": expected_margin_cap, "notional_usdc": notional,
                    "estimated_fee_usdc": fee, "assumed_fee_bps": 5, "limit_slippage_pct": .5,
                    "ai_reserved_margin_usdc": already_reserved,
                    "cloid": "0x" + uuid.uuid4().hex,
                    "source": {"model_version": candidate["model_version"], "decision_ms": candidate["decision_ms"],
                               "prediction_created_ms": candidate["created_ms"], "prediction_deadline_ms": candidate["deadline_ms"],
                               "probability_positive_net": candidate["probability_positive_net"], "calibrated": False},
                    "warnings": ["research_score_not_a_verified_success_probability", "immediate_ioc_differs_from_research_delayed_entry",
                                 "ioc_can_fill_partially_or_not_fill", "margin_and_fees_are_estimates", "cross_margin_can_risk_other_account_funds"]}
                payload_text = _json(payload)
                created_now = now + max(0, int((self.monotonic() - started) * 1000))
                if self._candidate(candidate, created_now) is None:
                    raise ValueError("signal_expired_during_preparation")
                expires = min(created_now + 90000, candidate["deadline_ms"])
                with closing(self._connect()) as db:
                    db.execute("BEGIN IMMEDIATE")
                    # Expired forms from a former Telegram binding must not
                    # permanently occupy this account's pending-form slot.
                    db.execute("UPDATE user_order_proposals SET status='EXPIRED',updated_ms=? WHERE account=? AND status='PENDING' AND expires_ms<=?",
                               (created_now, address, created_now))
                    if db.execute("SELECT 1 FROM user_order_proposals WHERE account=? AND status='PENDING'", (address,)).fetchone():
                        db.commit()
                        return self.summary(uid, profile, now)
                    db.execute("INSERT INTO user_order_proposals VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,?)",
                               (uuid.uuid4().hex, str(uid), address, fingerprint, created_now, expires, "PENDING", payload_text,
                                hashlib.sha256(payload_text.encode()).hexdigest(), created_now))
                    db.commit()
                self._remember(uid, address, "confirmation_required", now)
                return self.summary(uid, profile, now)
            except Exception as error:
                # Only fixed, local error codes are exposed; no raw API response.
                reason = str(error) if type(error) is ValueError and str(error) in {
                    "execution_pending", "position_conflict", "open_order_conflict", "instrument_owned_or_held",
                    "minimum_notional", "insufficient_capacity", "unsupported_capital_mode", "market_mode_unsupported",
                    "ai_slot_budget_exhausted", "ai_position_reconciliation_required"} else "market_data_unavailable"
        self._remember(uid, address, reason, now)
        return self.summary(uid, profile, now)

    proposeprepare = prepare

    def _write_result(self, proposal_id, status, result, now, operation_id=None, only_pending=False):
        with closing(self._connect()) as db:
            db.execute("UPDATE user_order_proposals SET status=?,result=?,updated_ms=?,operation_id=COALESCE(?,operation_id) WHERE id=?" +
                       (" AND status='PENDING'" if only_pending else ""),
                       (status, _json(result), now, operation_id, proposal_id))
            db.commit()

    def _get(self, uid, account, proposal_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM user_order_proposals WHERE id=? AND user_id=? AND account=?",
                             (proposal_id, str(uid), account)).fetchone()
        if row is None:
            raise ValueError("proposal_not_found")
        return self._row(row)

    def _validate_confirm(self, uid, profile, client, proposal, address, now):
        if _profile_gate(profile) != address or now >= proposal["expires_ms"] or now < proposal["created_ms"]:
            raise ValueError("proposal_expired_or_changed")
        payload = proposal["payload"]
        if (payload.get("entry_policy_version") != ENTRY_POLICY_VERSION
                or payload.get("entry_pct_of_share") != ENTRY_MARGIN_FRACTION * 100):
            raise ValueError("entry_policy_changed")
        mode, balance = _snapshot(client, address)
        if mode != payload["sizing_mode"]:
            raise ValueError("sizing_mode_changed")
        if _network(client) != payload["network"] or _slot_fingerprint(profile) != payload["slot_fingerprint"]:
            raise ValueError("network_or_slot_changed")
        self._clear_market(uid, profile, client, address, payload["coin"], own_id=proposal["id"])
        digits, leverage = _metadata(client, payload["coin"])
        leverage = _profile_leverage(profile, leverage)
        if digits != payload["sz_decimals"] or leverage != payload["leverage"]:
            raise ValueError("market_limits_changed")
        price = _number(client.mid(payload["coin"], ""), "reference_price", 1e-12)
        if abs(price / payload["reference_price"] - 1) > .0025:
            raise ValueError("price_changed")
        expected = payload["size"] * max(price * 1.005, payload["limit_price"])
        if expected / payload["leverage"] > balance / 3 * ENTRY_MARGIN_FRACTION + 1e-12:
            raise ValueError("budget_changed")
        if payload["size"] * min(price, payload["limit_price"]) < 10:
            raise ValueError("minimum_notional")
        if normalize_perp_size(payload["size"], digits) != payload["size"]:
            raise ValueError("size_invalid")
        capacity = _number(client.available_margin(""), "available_margin")
        if expected / payload["leverage"] + expected * .0005 > capacity:
            raise ValueError("insufficient_capacity")
        if self._reserved_margin(address, client) + expected / payload["leverage"] + expected * .0005 > balance / 3:
            raise ValueError("ai_slot_budget_exhausted")

    @staticmethod
    def _verify(client, payload, response):
        positions = _positions(client, payload["coin"])
        orders = _orders(client, payload["coin"])
        if not isinstance(response, dict):
            return "UNKNOWN", {"reason": "invalid_exchange_response"}
        data = response.get("response")
        statuses = (data.get("data") or {}).get("statuses") if isinstance(data, dict) else None
        rejected = response.get("status") == "err" or (isinstance(statuses, list) and len(statuses) == 1 and isinstance(statuses[0], dict) and "error" in statuses[0])
        if rejected and not positions and not orders:
            return "REJECTED", {"reason": "exchange_rejected_verified_flat"}
        if response.get("status") != "ok" or not isinstance(statuses, list) or len(statuses) != 1 or orders:
            return "UNKNOWN", {"reason": "exchange_outcome_unconfirmed"}
        fill = statuses[0].get("filled") if isinstance(statuses[0], dict) else None
        if not isinstance(fill, dict) or len(positions) != 1:
            return "UNKNOWN", {"reason": "fill_not_verified"}
        try:
            quantity = _number(fill.get("totalSz"), "filled_size", 1e-15)
            average = _number(fill.get("avgPx"), "average_price", 1e-12)
            oid = fill.get("oid")
            if isinstance(oid, bool) or not isinstance(oid, (int, str)) or not str(oid).isdigit():
                raise ValueError("missing_order_id")
            position = positions[0]
            actual = _number(position.get("size"), "actual_size", 1e-15)
            lev = _number(position.get("leverage"), "actual_leverage", 1)
            entry = _number(position.get("entry_price"), "actual_entry", 1e-12)
            tolerance = 10 ** (-payload["sz_decimals"]) * .1
            if normalize_perp_size(quantity, payload["sz_decimals"]) != quantity:
                raise ValueError("fill_size_precision_invalid")
            if (payload["direction"] == "LONG" and average > payload["limit_price"] + 1e-10) or (
                payload["direction"] == "SHORT" and average < payload["limit_price"] - 1e-10):
                raise ValueError("fill_outside_ioc_limit")
            if (quantity > payload["size"] + tolerance or abs(actual-quantity) > tolerance
                or position.get("side") != payload["direction"] or lev != payload["leverage"]
                or position.get("margin_mode") != "cross" or abs(entry-average) > max(1e-8, average*1e-6)):
                raise ValueError("position_does_not_match_fill")
            query = getattr(client, "query_order_by_cloid", None)
            if callable(query):
                proof = query(payload["cloid"])
                wrapper = proof.get("order") if isinstance(proof, dict) else None
                order = wrapper.get("order") if isinstance(wrapper, dict) else None
                if (not isinstance(proof, dict) or proof.get("status") != "order" or not isinstance(order, dict)
                    or wrapper.get("status") not in ("filled", "canceled", "iocCancel")
                    or str(order.get("oid")) != str(oid) or order.get("coin") != payload["coin"]
                    or order.get("cloid") != payload["cloid"]
                    or order.get("side") != ("B" if payload["direction"] == "LONG" else "A")
                    or abs(_number(order.get("origSz"), "original_order_size")-payload["size"]) > tolerance
                    or _number(order.get("limitPx"), "original_limit_price") != payload["limit_price"]):
                    raise ValueError("client_order_id_not_verified")
            status = "FILLED" if abs(quantity-payload["size"]) <= tolerance else "PARTIAL"
            return status, {"reason": "verified_exchange_fill", "oid": str(oid), "filled_size": quantity,
                            "average_price": average, "leverage": lev, "position": deepcopy(position),
                            "notional_usdc": actual * average, "margin_estimate_usdc": actual * average / lev}
        except (TypeError, ValueError):
            return "UNKNOWN", {"reason": "fill_position_mismatch"}

    def decide(self, uid, proposal_id, confirm, profile, public_client, signing_factory, persist_runtime, now_ms):
        now = _time(now_ms)
        started = self.monotonic()
        if not isinstance(confirm, bool):
            raise ValueError("confirm_must_be_boolean")
        if confirm is False:
            with closing(self._connect()) as db:
                owned = db.execute("SELECT account FROM user_order_proposals WHERE id=? AND user_id=?", (proposal_id, str(uid))).fetchone()
            if owned is None:
                raise ValueError("proposal_not_found")
            address = owned["account"]  # Decline remains available after disconnect.
        else:
            address = _address(profile)
        proposal = self._get(uid, address, proposal_id)
        if proposal["status"] != "PENDING":
            return proposal  # Includes unresolved SUBMITTING; never replay it.
        if now >= proposal["expires_ms"]:
            self._write_result(proposal_id, "EXPIRED", {"reason": "proposal_expired"}, now, only_pending=True)
            return self._get(uid, address, proposal_id)
        if confirm is False:
            with closing(self._connect()) as db:
                db.execute("UPDATE user_order_proposals SET status='DECLINED',updated_ms=? WHERE id=? AND status='PENDING'", (now, proposal_id))
                db.commit()
            return self._get(uid, address, proposal_id)
        if not callable(signing_factory) or not callable(persist_runtime):
            raise ValueError("persistent_confirmation_callbacks_required")
        try:
            self._validate_confirm(uid, profile, public_client, proposal, address, now)
            if now + max(0, int((self.monotonic()-started)*1000)) >= proposal["expires_ms"]:
                raise ValueError("proposal_expired_during_checks")
        except Exception:
            self._write_result(proposal_id, "INVALIDATED", {"reason": "fresh_checks_failed_prepare_new_form"}, now, only_pending=True)
            return self._get(uid, address, proposal_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE user_order_proposals SET status='SUBMITTING',updated_ms=? WHERE id=? AND status='PENDING'", (now, proposal_id)).rowcount
            db.commit()
        if not changed:
            return self._get(uid, address, proposal_id)
        payload, operation = proposal["payload"], None
        key = f"{payload['coin']}|"
        runtime = profile.setdefault("runtime", {})
        try:
            operation = self.journal.prepare(address, key, {"action": "AI_USER_OPEN", "proposal_id": proposal_id,
                "network": payload["network"],
                "coin": payload["coin"], "direction": payload["direction"], "size": payload["size"],
                "limit_price": payload["limit_price"], "leverage": payload["leverage"], "cloid": payload["cloid"]})
            self._write_result(proposal_id, "SUBMITTING", {"reason": "intent_persisted"}, now, operation)
            runtime.setdefault("ai_user_order_holds", {})[key] = {"proposal_id": proposal_id, "status": "SUBMITTING", "operation_id": operation}
            runtime.setdefault("ai_user_order_positions", {})[key] = {"proposal_id": proposal_id, "status": "SUBMITTING",
                "coin": payload["coin"], "direction": payload["direction"], "origin": "user_confirmed_ai_form", "operation_id": operation}
            persist_runtime()  # MUST succeed before constructing a signing client.
            signing_client = signing_factory()
            if str(getattr(signing_client, "address", "")).lower() != address or _network(signing_client) != payload["network"]:
                raise ValueError("signing_account_or_network_mismatch")
            if now + max(0, int((self.monotonic()-started)*1000)) >= proposal["expires_ms"]:
                raise ValueError("proposal_expired_before_submission")
            from core.confirmed_execution_adapter import confirmed_ai_order
            response = confirmed_ai_order(signing_client, payload, proposal["expires_ms"])
            status, result = self._verify(public_client, payload, response)
        except Exception:
            status, result = "UNKNOWN", {"reason": "submission_or_verification_unconfirmed_no_retry"}
        # SQL result first: JSON persistence cannot erase evidence of a fill.
        self._write_result(proposal_id, status, result, now, operation)
        if operation:
            try:
                self.journal.finish(operation, {"ok": status in ("FILLED", "PARTIAL", "REJECTED"),
                    "status": status, "proposal_id": proposal_id, **result})  # No ordinary copy ownership.
            except Exception:
                # Existing journal PREPARED/UNKNOWN remains a conservative hold.
                pass
        marker = {"proposal_id": proposal_id, "status": status, "operation_id": operation}
        runtime.setdefault("ai_user_order_positions", {})[key] = {**marker, "coin": payload["coin"],
            "origin": "user_confirmed_ai_form", **deepcopy(result)}
        if status == "REJECTED":
            runtime.setdefault("ai_user_order_holds", {}).pop(key, None)
        else:
            runtime.setdefault("ai_user_order_holds", {})[key] = marker
        try:
            persist_runtime()
        except Exception:
            pass  # reserved_markets + SQL result/journal restore the exclusion.
        return self._get(uid, address, proposal_id)
