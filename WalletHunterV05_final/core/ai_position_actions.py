"""Single confirmed interventions, never autonomous position management.

Caller holds current profile + account guards. All preparation is public/read-
only. A signing client is created only after literal True, fresh validation and
durable intervention HOLD. These scenarios have UNKNOWN success probability.
The original source is preserved in the execution journal; a separate HOLD
prevents ordinary copying from immediately undoing a confirmed intervention.
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

from core.ai_user_orders import _address, _network, _number, _time, _json
from core.ai_review import analyse
from core.ai_rescue_policy import RESCUE_TRIGGER_ROE_PCT
from core.execution_journal import ExecutionJournal
from core.order_precision import normalize_perp_price, normalize_perp_size


BAR = 900000
COOLDOWN = 1800000
ACTIVE_HOLDS = ("SUBMITTING", "UNKNOWN", "FILLED", "PARTIAL")


def _key(position):
    coin, dex = position["coin"], position.get("dex") or ""
    if dex and ":" not in coin:
        coin = f"{dex}:{coin}"
    if not isinstance(coin, str) or not re.fullmatch(r"(?:xyz:)?[A-Za-z0-9_.-]+", coin):
        raise ValueError("unsupported_market")
    if (":" in coin and coin.split(":")[0] != dex) or dex not in ("", "xyz"):
        raise ValueError("market_identity_invalid")
    return f"{coin}|{dex}"


def _identity(position):
    return {"coin": _key(position).split("|")[0], "dex": position.get("dex") or "",
            "side": position["side"], "size": _number(position["size"], "size", 1e-15),
            "entry_price": _number(position["entry_price"], "entry", 1e-12),
            "leverage": _number(position["leverage"], "leverage", 1),
            "margin_mode": position.get("margin_mode")}


def _all_positions(client):
    result = client.positions(True, True)
    if not isinstance(result, list):
        raise ValueError("positions_unavailable")
    positions = {}
    for raw in result:
        if not isinstance(raw, dict):
            raise ValueError("position_invalid")
        if not _number(raw.get("size"), "position_size"):
            continue
        try:
            key = _key(raw)
        except ValueError:
            continue  # Other unsupported markets are never intervention targets.
        p = deepcopy(raw)
        p.update(_identity(p))
        if p["side"] not in ("LONG", "SHORT") or p["margin_mode"] not in ("cross", "isolated"):
            raise ValueError("position_mode_invalid")
        p["roe"] = _number(p.get("roe"), "roe", -math.inf)
        p["margin_used"] = _number(p.get("margin_used"), "position_margin", 1e-15)
        p["unrealized_pnl"] = _number(p.get("unrealized_pnl"), "position_pnl", -math.inf)
        if key in positions:
            raise ValueError("duplicate_position")
        positions[key] = p
    return positions


class AiPositionActions:
    def __init__(self, root, *, monotonic=time.monotonic):
        self.monotonic = monotonic
        self.journal = ExecutionJournal(root)
        self.path = os.path.join(root, "data", "ai_position_actions.sqlite3")
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        if os.path.islink(self.path):
            raise ValueError("Intervention database cannot be a symlink")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        os.chmod(self.path, 0o600)
        with closing(self._connect()) as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS position_proposals(
                id TEXT PRIMARY KEY,user_id TEXT NOT NULL,account TEXT NOT NULL,market TEXT NOT NULL,
                created_ms INTEGER NOT NULL,expires_ms INTEGER NOT NULL,status TEXT NOT NULL,
                payload TEXT NOT NULL,checksum TEXT NOT NULL,result TEXT,operation_id TEXT,
                updated_ms INTEGER NOT NULL,notified INTEGER NOT NULL DEFAULT 0,released_ms INTEGER);
              CREATE INDEX IF NOT EXISTS position_account ON position_proposals(account,market,created_ms);
              CREATE TABLE IF NOT EXISTS position_availability(
                user_id TEXT NOT NULL,account TEXT NOT NULL,payload TEXT NOT NULL,
                PRIMARY KEY(user_id,account));
            """)
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _decode(row):
        if hashlib.sha256(row["payload"].encode()).hexdigest() != row["checksum"]:
            raise ValueError("immutable_intervention_corrupt")
        payload = json.loads(row["payload"])
        _json(payload)
        return {"id": row["id"], "status": row["status"], "created_ms": row["created_ms"],
                "expires_ms": row["expires_ms"], "notified": bool(row["notified"]), "payload": payload,
                "operation_id": row["operation_id"], "released_ms": row["released_ms"],
                "result": json.loads(row["result"]) if row["result"] else None}

    def reserved_markets(self, uid, account):
        with closing(self._connect()) as db:
            sql = "SELECT * FROM position_proposals WHERE account=? AND released_ms IS NULL AND status IN ('SUBMITTING','UNKNOWN','FILLED','PARTIAL')"
            params = [account.lower()]
            if uid is not None:
                sql += " AND user_id=?"; params.append(str(uid))
            rows = db.execute(sql + " ORDER BY created_ms", params).fetchall()
        out = {}
        for row in rows:
            decoded = self._decode(row)
            # An unresolved earlier operation cannot be masked by a later fill.
            if out.get(row["market"], {}).get("status") in ("UNKNOWN", "SUBMITTING"):
                continue
            out[row["market"]] = {"proposal_id": row["id"], "status": row["status"],
                "operation_id": row["operation_id"], "source_wallet": decoded["payload"]["source_wallet"],
                "can_release": row["status"] in ("FILLED", "PARTIAL")}
        return out

    def summary(self, uid, profile, now_ms):
        now = _time(now_ms)
        try:
            address = _address(profile)
        except ValueError:
            return {"status": "UNAVAILABLE", "reason": "account_missing", "pending": [], "history": [], "holds": [], "availability": []}
        enabled = profile.get("ai_review_enabled", True) is True
        with closing(self._connect()) as db:
            db.execute("UPDATE position_proposals SET status='EXPIRED',updated_ms=? WHERE user_id=? AND account=? AND status='PENDING' AND expires_ms<=?", (now, str(uid), address, now))
            if not enabled:
                db.execute("UPDATE position_proposals SET status='INVALIDATED',updated_ms=? WHERE user_id=? AND account=? AND status='PENDING'", (now, str(uid), address))
            db.commit()
            rows = db.execute("SELECT * FROM position_proposals WHERE user_id=? AND account=? ORDER BY created_ms DESC,rowid DESC LIMIT 30", (str(uid), address)).fetchall()
            notes = db.execute("SELECT payload FROM position_availability WHERE user_id=? AND account=?", (str(uid), address)).fetchone()
        data = [self._decode(row) for row in rows]
        pending = [row for row in data if row["status"] == "PENDING"]
        return {"status": "UNAVAILABLE" if not enabled else ("PENDING" if pending else "IDLE"),
                "reason": "rescue_disabled" if not enabled else ("confirmation_required" if pending else "no_eligible_scenario"),
                "pending": pending, "history": [row for row in data if row["status"] != "PENDING"],
                "holds": [dict(market=key, **value) for key, value in self.reserved_markets(uid, address).items()],
                "availability": json.loads(notes[0]) if notes else [],
                "execution_mode": "USER_CONFIRMATION_ONLY", "automatic_execution": False}

    def mark_notified(self, uid, proposal_id):
        with closing(self._connect()) as db:
            db.execute("UPDATE position_proposals SET notified=1 WHERE id=? AND user_id=?", (proposal_id, str(uid)))
            db.commit()

    def pending_for_notification(self, uid, profile, now_ms):
        return [row for row in self.summary(uid, profile, now_ms)["pending"] if not row["notified"]]

    @staticmethod
    def _proof(reader, address, position, owned, now):
        if not owned.get("managed") or _identity(owned.get("position") or {}) != _identity(position):
            raise ValueError("ownership_snapshot_changed")
        verified = _time(owned.get("verified_at_ms"))
        if verified > now:
            raise ValueError("ownership_timestamp_invalid")
        fills = reader._info({"type": "userFillsByTime", "user": address, "startTime": verified, "endTime": now, "aggregateByTime": False})
        if not isinstance(fills, list) or len(fills) >= 2000:
            raise ValueError("ownership_history_incomplete")
        for fill in fills:
            if not isinstance(fill, dict) or not isinstance(fill.get("coin"), str):
                raise ValueError("ownership_history_invalid")
            stamp = _time(fill.get("time"))
            if stamp > now:
                raise ValueError("ownership_history_future")
            fill_coin = (f"{position['dex']}:{fill['coin']}" if position.get("dex") and ":" not in fill["coin"] else fill["coin"])
            if stamp >= verified and fill_coin == position["coin"]:
                raise ValueError("fills_after_verified_ownership")

    def _budget(self, profile, client, address, source, positions, owned):
        snapshot = client.capital_snapshot()
        mode = snapshot.get("mode") if isinstance(snapshot, dict) else getattr(snapshot, "mode", None)
        value = snapshot.get("sizing_base_usdc") if isinstance(snapshot, dict) else getattr(snapshot, "sizing_base_usdc", None)
        if mode not in ("unifiedAccount", "disabled"):
            raise ValueError("unsupported_capital_mode")
        slot = _number(value, "sizing_balance") / 3
        reserved = 0.
        for key, record in owned.items():
            if not record.get("managed"):
                continue
            sources = record.get("source_targets") or []
            if not any(str(row.get("wallet", "")).lower() == source for row in sources):
                continue
            if len(sources) != 1 or key not in positions or _identity(record.get("position") or {}) != _identity(positions[key]):
                raise ValueError("source_budget_ownership_ambiguous")
            reserved += max(_number(sources[0].get("margin", 0), "source_reserved"), positions[key]["margin_used"])
        spent = count = 0
        for key, usage in (profile.get("runtime", {}).get("ai_budget_usage") or {}).items():
            if not isinstance(usage, dict):
                raise ValueError("prior_budget_usage_invalid")
            if not usage.get("source_wallet") and (_number(usage.get("extra_margin_usdc", 0), "prior_extra") > 0 or _number(usage.get("additions", 0), "prior_additions") > 0):
                raise ValueError("prior_budget_usage_source_unknown")
            if str(usage.get("source_wallet", "")).lower() != source:
                continue
            spent += _number(usage.get("extra_margin_usdc", 0), "prior_extra")
            value = _number(usage.get("additions", 0), "prior_additions")
            if int(value) != value:
                raise ValueError("prior_budget_usage_invalid")
            count += int(value)
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM position_proposals WHERE account=? AND status IN ('FILLED','PARTIAL')", (address,)).fetchall()
        for row in rows:
            decoded = self._decode(row)
            if decoded["payload"]["source_wallet"] != source:
                continue
            result = decoded["result"] or {}
            spent = max(spent, _number(result.get("cumulative_source_extra_usdc", 0), "recorded_extra"))
            count = max(count, int(_number(result.get("cumulative_source_additions", 0), "recorded_additions")))
        remaining = max(0., min(slot * .5 - spent, slot - reserved)) if count < 4 else 0.
        return {"source_slot_usdc": slot, "source_reserved_usdc": reserved, "extra_used_usdc": spent,
                "extra_remaining_usdc": remaining, "additions_used": count, "additions_remaining": max(0, 4-count), "sizing_mode": mode}

    @staticmethod
    def _factors(reader, position, now):
        dex, coin = position.get("dex") or "", position["coin"]
        data = reader._info({"type": "metaAndAssetCtxs", "dex": dex})
        if not isinstance(data, list) or len(data) != 2 or not isinstance(data[0], dict):
            raise ValueError("market_context_unavailable")
        assets, contexts = data[0].get("universe"), data[1]
        if not isinstance(assets, list) or not isinstance(contexts, list) or len(assets) != len(contexts):
            raise ValueError("market_context_invalid")
        matched = [context for asset, context in zip(assets, contexts) if asset.get("name") == coin]
        if len(matched) != 1:
            raise ValueError("market_context_ambiguous")
        context = {"funding_bps_hour": _number(matched[0].get("funding"), "funding", -math.inf)*10000,
                   "open_interest": _number(matched[0].get("openInterest"), "open_interest")}
        rows = reader._info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "15m", "startTime": now-120*BAR, "endTime": now}})
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("candles_unavailable")
        closed = []
        for row in rows:
            opened, end = _time(row.get("t")), _time(row.get("T"))
            if opened % BAR or end-opened not in (BAR-1, BAR):
                raise ValueError("candle_interval_invalid")
            if end >= now:
                continue
            o, h, l, c = (_number(row.get(key), "ohlc", 1e-12) for key in ("o", "h", "l", "c"))
            if not l <= min(o, c) <= max(o, c) <= h:
                raise ValueError("candle_ohlc_invalid")
            closed.append(row)
        facts = analyse(closed, context, now)
        _number(facts["volume_ratio20"], "volume_ratio")
        return facts

    def _context(self, uid, profile, client, reader, key, now, *, factors=True, check_orders=True):
        address = _address(profile)
        if str(getattr(client, "address", "")).lower() != address:
            raise ValueError("account_mismatch")
        if self.journal.pending(address):
            raise ValueError("execution_pending")
        holds = self.reserved_markets(None, address)
        if any(row["status"] in ("UNKNOWN", "SUBMITTING") for row in holds.values()):
            raise ValueError("execution_pending")
        runtime = profile.get("runtime") or {}
        if key in (runtime.get("manual_hold_keys") or []) or key in (runtime.get("ai_user_order_holds") or {}) or key in (runtime.get("ai_hold_keys") or {}):
            raise ValueError("other_action_hold")
        if str((runtime.get("manual_actions", {}).get(key) or {}).get("status", "")).lower() in ("unknown", "submitting", "cleanup_required"):
            raise ValueError("other_action_hold")
        positions = _all_positions(client)
        position = positions.get(key)
        if not position:
            raise ValueError("position_missing")
        owned = self.journal.owned(address)
        record = owned.get(key) or {}
        self._proof(reader, address, position, record, now)
        sources = record.get("source_targets") or []
        if len(sources) != 1:
            raise ValueError("source_unknown_or_shared")
        source = str(sources[0].get("wallet", "")).lower()
        if not source or source not in [str(value).lower() for value in profile.get("leaders", [])]:
            raise ValueError("source_removed")
        dex, coin = position.get("dex") or "", position["coin"]
        orders = client.frontend_open_orders(dex)
        if not isinstance(orders, list) or any(not isinstance(row, dict) or not isinstance(row.get("coin"), str) for row in orders):
            raise ValueError("orders_unavailable")
        orders = [row for row in orders if (f"{dex}:{row['coin']}" if dex and ":" not in row["coin"] else row["coin"]) == coin]
        if orders and check_orders:
            raise ValueError("stop_review_required")
        meta = client.meta(dex)
        rows = [row for row in meta.get("universe", []) if row.get("name") == coin]
        if len(rows) != 1 or rows[0].get("isDelisted"):
            raise ValueError("metadata_unavailable")
        digits, maximum = rows[0].get("szDecimals"), rows[0].get("maxLeverage")
        if isinstance(digits, bool) or not isinstance(digits, int) or not 0 <= digits <= 6 or isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise ValueError("market_limits_invalid")
        return {"address": address, "position": position, "owned": record, "source": source,
                "budget": self._budget(profile, client, address, source, positions, owned),
                "price": _number(client.mid(coin, dex), "quote", 1e-12), "digits": digits, "max_leverage": maximum,
                "network": _network(client), "factors": self._factors(reader, position, now) if factors else None,
                "stop_impact": {"open_orders_count": len(orders), "stop_orders_count": sum(bool(o.get("isTrigger")) for o in orders),
                                "policy": "no_automatic_stop_changes", "reason": "open_orders_need_review" if orders else "no_orders"}}

    def _build(self, context, action, profile):
        p, b, price = context["position"], context["budget"], context["price"]
        side_sign = 1 if p["side"] == "LONG" else -1
        buy = (p["side"] == "SHORT") if action == "REDUCE" else p["side"] == "LONG"
        limit = normalize_perp_price(price * (1.005 if buy else .995), context["digits"])
        supportive = ((context["factors"]["trend_ema20_50"]["ema20"]-context["factors"]["trend_ema20_50"]["ema50"])*side_sign > 0
                      and context["factors"]["macd_hist"]*side_sign > 0
                      and 30 <= context["factors"]["rsi14"] <= 70)
        if action == "REDUCE":
            size = normalize_perp_size(p["size"] * .25, context["digits"])
            if not 0 < size < p["size"]:
                raise ValueError("reduction_size_unavailable")
            after_size = p["size"] - size
            after_entry = p["entry_price"]
            margin_change = -p["margin_used"] * size/p["size"]
            realized = (limit-p["entry_price"]) * size * side_sign
        else:
            if profile.get("crypto_enabled", True) is not True and not p["dex"]:
                raise ValueError("market_disabled_for_increase")
            if profile.get("stocks_enabled", True) is not True and p["dex"] == "xyz":
                raise ValueError("market_disabled_for_increase")
            if not supportive:
                raise ValueError("rebound_not_supported")
            ceiling = min(20, context["max_leverage"], int(profile.get("max_leverage") or 20))
            if p["leverage"] > ceiling:
                raise ValueError("averaging_leverage_limit")
            margin_cap = min(p["margin_used"] * .25, b["extra_remaining_usdc"] * .25)
            if margin_cap <= 0 or b["additions_remaining"] <= 0:
                raise ValueError("extra_budget_exhausted")
            sizing = max(price*1.005, limit)
            size = normalize_perp_size(margin_cap*p["leverage"]/sizing, context["digits"])
            after_size = p["size"] + size
            after_entry = (p["entry_price"]*p["size"]+limit*size)/after_size
            margin_change = size*sizing/p["leverage"]
            realized = 0.
        notional = size*price
        if size <= 0 or size*min(price, limit) < 10:
            raise ValueError("minimum_notional")
        fee = size*max(price*1.005, limit)*.0005
        payload = {"action": action, "coin": p["coin"], "dex": p["dex"], "direction": p["side"], "side": p["side"],
            "margin_mode": p["margin_mode"], "network": context["network"], "size": size, "size_text": format(Decimal(str(size)), "f"),
            "limit_price": limit, "limit_price_text": format(Decimal(str(limit)), "f"), "reference_price": price,
            "leverage": p["leverage"], "reduce_only": action == "REDUCE", "is_buy": buy, "order_type": "LIMIT_IOC",
            "sz_decimals": context["digits"],
            "position_before": deepcopy(p), "expected_after": {"size": after_size, "entry_price": after_entry,
                 "margin_estimate_usdc": max(0., p["margin_used"]+margin_change)},
            "action_notional_usdc": notional, "margin_change_estimate_usdc": margin_change,
            "realized_pnl_estimate_usdc": realized-fee, "estimated_fee_usdc": fee, "assumed_fee_bps": 5,
            "source_wallet": context["source"], **deepcopy(b), "factors": deepcopy(context["factors"]),
            "hypothesis": {"classification": "RISK_REDUCTION" if action == "REDUCE" else "EXPERIMENTAL_REBOUND",
                "reason": "reduce_exposure_realizes_part_of_loss" if action == "REDUCE" else "trend_and_macd_support_direction_but_reversal_can_fail",
                "probability": None, "probability_label": "success_not_guaranteed"},
            "alternatives": [{"action": action, "available": False, "reason": "cross_or_unimplemented"}
                             for action in ("LOWER_LEVERAGE", "ADD_MARGIN")],
            "stop_impact": context["stop_impact"], "cloid": "0x"+uuid.uuid4().hex,
            "policy": {"trigger_roe_pct": RESCUE_TRIGGER_ROE_PCT, "target_roe_pct": 3, "failure_roe_pct": -120,
                       "max_extra_source_fraction": .5, "max_additions": 4},
            "warnings": ["probability_unknown_not_guaranteed", "no_guaranteed_recovery_or_plus3",
                         "minus120_is_not_a_guaranteed_stop_liquidation_can_occur_earlier", "ioc_can_partially_fill",
                         "margin_and_fees_estimated", "copying_held_after_intervention_until_explicit_reconciliation",
                         "cross_margin_can_risk_other_account_funds", "averaging_increases_loss_if_price_continues"]}
        return payload

    def prepare(self, uid, profile, public_client, reader, now_ms, force_refresh=False):
        now = _time(now_ms); start = self.monotonic()
        initial = self.summary(uid, profile, now)
        if initial["status"] == "UNAVAILABLE":
            return initial
        address = _address(profile)
        notes = []
        try:
            positions = _all_positions(public_client)
        except Exception:
            return dict(initial, reason="positions_unavailable")
        for key, position in positions.items():
            if position["coin"] not in ("BTC", "ETH") and position["dex"] != "xyz":
                continue
            if position["roe"] > RESCUE_TRIGGER_ROE_PCT:
                continue
            try:
                context = self._context(uid, profile, public_client, reader, key, now)
                if context["position"]["roe"] > RESCUE_TRIGGER_ROE_PCT:
                    continue
            except Exception as error:
                notes.append({"market": key, "coin": position["coin"], "reason": str(error) if type(error) is ValueError else "analysis_unavailable"})
                continue
            for action in ("REDUCE", "AVERAGE"):
                try:
                    payload = self._build(context, action, profile)
                    if action == "AVERAGE" and payload["margin_change_estimate_usdc"]+payload["estimated_fee_usdc"] > _number(public_client.available_margin(position["dex"]), "free_capacity"):
                        raise ValueError("insufficient_capacity")
                except Exception as error:
                    notes.append({"market": key, "coin": position["coin"], "action": action, "available": False,
                                  "reason": str(error) if type(error) is ValueError else "scenario_unavailable"})
                    continue
                current = now+max(0, int((self.monotonic()-start)*1000))
                if current-now > 120000:
                    continue
                with closing(self._connect()) as db:
                    db.execute("BEGIN IMMEDIATE")
                    previous = db.execute("SELECT * FROM position_proposals WHERE user_id=? AND account=? AND market=? ORDER BY created_ms DESC", (str(uid), address, key)).fetchall()
                    blocked = False
                    for row in previous:
                        prior = self._decode(row)
                        if row["status"] in ("UNKNOWN", "SUBMITTING"):
                            blocked = True; break
                        if row["status"] in ("FILLED", "PARTIAL") and current-row["updated_ms"] < COOLDOWN:
                            blocked = True; break
                        if prior["payload"]["action"] != action:
                            continue
                        if row["status"] == "PENDING" and row["expires_ms"] > current:
                            blocked = True; break
                        significant = abs(position["roe"]-prior["payload"]["position_before"]["roe"]) >= 15
                        if current-row["created_ms"] < COOLDOWN and not significant and not (force_refresh and row["status"] in ("EXPIRED", "INVALIDATED")):
                            blocked = True; break
                    if blocked:
                        continue
                    body = _json(payload)
                    db.execute("INSERT INTO position_proposals(id,user_id,account,market,created_ms,expires_ms,status,payload,checksum,updated_ms) VALUES(?,?,?,?,?,?,'PENDING',?,?,?)",
                               (uuid.uuid4().hex, str(uid), address, key, current, current+90000, body, hashlib.sha256(body.encode()).hexdigest(), current))
                    db.commit()
        with closing(self._connect()) as db:
            db.execute("INSERT INTO position_availability VALUES(?,?,?) ON CONFLICT(user_id,account) DO UPDATE SET payload=excluded.payload", (str(uid), address, _json(notes)))
            db.commit()
        return self.summary(uid, profile, now+max(0,int((self.monotonic()-start)*1000)))

    def _get(self, uid, proposal_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM position_proposals WHERE id=? AND user_id=?", (proposal_id, str(uid))).fetchone()
        if row is None:
            raise ValueError("proposal_not_found")
        return row, self._decode(row)

    def _finish(self, proposal_id, status, result, now, operation=None, pending=False):
        with closing(self._connect()) as db:
            db.execute("UPDATE position_proposals SET status=?,result=?,updated_ms=?,operation_id=COALESCE(?,operation_id) WHERE id=?" + (" AND status='PENDING'" if pending else ""),
                       (status, _json(result), now, operation, proposal_id))
            db.commit()

    @staticmethod
    def _verify(client, payload, response, observed_now):
        statuses = (((response.get("response") or {}).get("data") or {}).get("statuses") or []) if isinstance(response, dict) and isinstance(response.get("response"), dict) else []
        fill = statuses[0].get("filled") if len(statuses) == 1 and isinstance(statuses[0], dict) else None
        if not isinstance(fill, dict) or response.get("status") != "ok":
            raise ValueError("fill_unconfirmed")
        amount, average = _number(fill.get("totalSz"), "fill_size", 1e-15), _number(fill.get("avgPx"), "fill_price", 1e-12)
        if isinstance(fill.get("oid"), bool) or not str(fill.get("oid", "")).isdigit():
            raise ValueError("fill_oid_unconfirmed")
        if (payload["is_buy"] and average > payload["limit_price"]+1e-10) or (not payload["is_buy"] and average < payload["limit_price"]-1e-10):
            raise ValueError("fill_beyond_limit")
        if amount > payload["size"]+1e-12:
            raise ValueError("fill_exceeds_quantity")
        if normalize_perp_size(amount, payload["sz_decimals"]) != amount:
            raise ValueError("fill_lot_invalid")
        before = payload["position_before"]
        snapshot_started_ms = observed_now()
        positions = _all_positions(client)
        after = positions.get(_key(before))
        expected_size = before["size"]+amount*(1 if payload["action"] == "AVERAGE" else -1)
        expected_entry = ((before["entry_price"]*before["size"]+average*amount)/expected_size) if payload["action"] == "AVERAGE" else before["entry_price"]
        if not after or abs(after["size"]-expected_size) > 1e-10 or after["side"] != before["side"] or after["leverage"] != before["leverage"] or after["margin_mode"] != before["margin_mode"] or abs(after["entry_price"]-expected_entry) > max(1e-8, expected_entry*1e-6):
            raise ValueError("position_after_mismatch")
        after["snapshot_started_ms"] = snapshot_started_ms
        orders = client.frontend_open_orders(before["dex"])
        if not isinstance(orders, list) or any((f"{before['dex']}:{row.get('coin')}" if before["dex"] and ":" not in str(row.get("coin")) else row.get("coin")) == before["coin"] for row in orders):
            raise ValueError("unexpected_resting_order")
        query = getattr(client, "query_order_by_cloid", None)
        if callable(query):
            proof = query(payload["cloid"])
            wrapper = proof.get("order") if isinstance(proof, dict) else None
            order = wrapper.get("order") if isinstance(wrapper, dict) else None
            if (not isinstance(order, dict) or proof.get("status") != "order" or wrapper.get("status") not in ("filled", "canceled", "iocCancel")
                or order.get("cloid") != payload["cloid"] or str(order.get("oid")) != str(fill["oid"])
                or (f"{before['dex']}:{order.get('coin')}" if before["dex"] and ":" not in str(order.get("coin")) else order.get("coin")) != before["coin"] or order.get("side") != ("B" if payload["is_buy"] else "A")
                or _number(order.get("origSz"), "original_size") != payload["size"] or _number(order.get("limitPx"), "original_limit") != payload["limit_price"]):
                raise ValueError("client_order_id_unconfirmed")
        return ("FILLED" if abs(amount-payload["size"]) < 1e-10 else "PARTIAL"), {"reason": "verified_intervention_fill",
            "filled_size": amount, "average_price": average, "oid": str(fill["oid"]), "position_before": before,
            "position_after": after, "copy_on_hold": True, "origin": "user_confirmed_position_intervention"}

    def decide(self, uid, proposal_id, confirm, profile, public_client, signing_factory, persist_runtime, now_ms, canonical_context=None):
        now = _time(now_ms); started = self.monotonic()
        if not isinstance(confirm, bool):
            raise ValueError("confirm_must_be_boolean")
        row, proposal = self._get(uid, proposal_id)
        if row["status"] != "PENDING":
            return proposal
        if now >= row["expires_ms"]:
            self._finish(proposal_id, "EXPIRED", {"reason": "expired"}, now, pending=True)
            return self._get(uid, proposal_id)[1]
        if now < row["created_ms"]:
            self._finish(proposal_id, "INVALIDATED", {"reason": "clock_before_proposal"}, now, pending=True)
            return self._get(uid, proposal_id)[1]
        if not confirm:
            self._finish(proposal_id, "DECLINED", {"reason": "user_declined"}, now, pending=True)
            return self._get(uid, proposal_id)[1]
        if not callable(signing_factory) or not callable(persist_runtime):
            raise ValueError("persistent_callbacks_required")
        payload, key, operation = proposal["payload"], row["market"], None
        # The reader is intentionally a PUBLIC-only interface supplied by client.
        reader = getattr(public_client, "reader", None)
        if reader is None:
            class Reader:
                def _info(self, request):
                    return public_client.info.post("/info", request)
            reader = Reader()
        try:
            if _address(profile) != row["account"] or not profile.get("ai_review_enabled", True):
                raise ValueError("profile_changed")
            context = self._context(uid, profile, public_client, reader, key, now)
            if _identity(context["position"]) != _identity(payload["position_before"]) or context["source"] != payload["source_wallet"] or context["position"]["roe"] > RESCUE_TRIGGER_ROE_PCT or context["network"] != payload["network"]:
                raise ValueError("position_or_source_changed")
            if abs(context["price"]/payload["reference_price"]-1) > .0025:
                raise ValueError("quote_changed")
            if payload["action"] == "AVERAGE" and (context["budget"]["additions_used"] != payload["additions_used"] or abs(context["budget"]["extra_used_usdc"]-payload["extra_used_usdc"]) > 1e-12):
                # Two instruments can share one source allocation. A form made
                # before another confirmed addition must not reset its ledger.
                raise ValueError("source_extra_usage_changed")
            rebuilt = self._build(context, payload["action"], profile)
            if payload["size"] > rebuilt["size"]+1e-12 or context["digits"] != payload["sz_decimals"]:
                raise ValueError("budget_or_limits_changed")
            if payload["action"] == "AVERAGE" and (rebuilt["margin_change_estimate_usdc"]+rebuilt["estimated_fee_usdc"] > _number(public_client.available_margin(payload["dex"]), "capacity")):
                raise ValueError("capacity_changed")
            if now+max(0,int((self.monotonic()-started)*1000)) >= row["expires_ms"]:
                raise ValueError("expired_during_checks")
        except Exception:
            self._finish(proposal_id, "INVALIDATED", {"reason": "fresh_position_or_budget_checks_failed"}, now, pending=True)
            return self._get(uid, proposal_id)[1]
        with closing(self._connect()) as db:
            changed = db.execute("UPDATE position_proposals SET status='SUBMITTING',updated_ms=? WHERE id=? AND status='PENDING'", (now, proposal_id)).rowcount
            db.commit()
        if not changed:
            return self._get(uid, proposal_id)[1]
        runtime = profile.setdefault("runtime", {})
        try:
            operation = self.journal.prepare(row["account"], key, {"action": "AI_POSITION_"+payload["action"], "proposal_id": proposal_id,
                "network": payload["network"],
                "source_wallet": payload["source_wallet"], "position_before": _identity(payload["position_before"]), "size": payload["size"], "cloid": payload["cloid"]})
            self._finish(proposal_id, "SUBMITTING", {"reason": "intent_persisted"}, now, operation)
            runtime.setdefault("ai_position_action_holds", {})[key] = {"proposal_id": proposal_id, "status": "SUBMITTING", "operation_id": operation}
            persist_runtime()
            if now+max(0,int((self.monotonic()-started)*1000)) >= row["expires_ms"]:
                raise ValueError("signer_mismatch_or_expiry")
            from core.confirmed_execution_adapter import confirmed_ai_position, execute_confirmed_ai
            if canonical_context is not None:
                route_context = canonical_context(payload) if callable(canonical_context) else canonical_context
                response = execute_confirmed_ai(route_context, coin=payload['coin'], dex=payload.get('dex',''),
                    side='BUY' if payload['is_buy'] else 'SELL', size=payload['size'], price=payload['limit_price'],
                    action='CLOSE' if payload.get('reduce_only') and payload['action']=='CLOSE' else 'REDUCE', source=payload.get('source_wallet','ai'))
                status = getattr(response, 'status', 'UNKNOWN')
                result = {'canonical': True, 'status': status, 'order_ids': list(getattr(response, 'order_ids', ())),
                    'filled_size': sum(getattr(fill, 'size', 0) for fill in getattr(response, 'fills', ())),
                    'average_price': (sum(fill.size*fill.price for fill in getattr(response, 'fills', ())) /
                        max(sum(fill.size for fill in getattr(response, 'fills', ())), 1e-12)) if getattr(response, 'fills', ()) else None}
            else:
                signer = signing_factory()
                if str(getattr(signer, "address", "")).lower() != row["account"] or _network(signer) != payload["network"]:
                    raise ValueError("signer_mismatch")
                if now+max(0,int((self.monotonic()-started)*1000)) >= row["expires_ms"]:
                    raise ValueError("proposal_expired_before_submission")
                response = confirmed_ai_position(signer, {**payload, 'expected_position': _identity(payload['position_before'])}, row['expires_ms'])
                status, result = self._verify(public_client, payload, response, lambda: now+max(0,int((self.monotonic()-started)*1000)))
        except Exception:
            status, result = "UNKNOWN", {"reason": "intervention_unconfirmed_no_retry", "copy_on_hold": True}
        completed = now+max(0,int((self.monotonic()-started)*1000))
        if status in ("FILLED", "PARTIAL"):
            added = result["filled_size"]*result["average_price"]/payload["leverage"] if payload["action"] == "AVERAGE" else 0
            result.update(actual_extra_margin_usdc=added,
                          cumulative_source_extra_usdc=context["budget"]["extra_used_usdc"]+added,
                          cumulative_source_additions=context["budget"]["additions_used"]+int(payload["action"] == "AVERAGE"))
        self._finish(proposal_id, status, result, completed, operation)
        if operation:
            ownership = None
            if status in ("FILLED", "PARTIAL"):
                ownership = deepcopy(context["owned"])
                after = deepcopy(result["position_after"])
                ownership.update(position=after, size=after["size"], side=after["side"], intervention_proposal_id=proposal_id)
                ratio = after["size"]/payload["position_before"]["size"]
                ownership["source_targets"][0]["margin"] = after["margin_used"]
                if "signed" in ownership["source_targets"][0]:
                    ownership["source_targets"][0]["signed"] = _number(ownership["source_targets"][0]["signed"], "source_signed", -math.inf)*ratio
            try:
                self.journal.finish(operation, {"ok": ownership is not None, "status": status, **result}, ownership)
            except Exception:
                pass  # Pending journal is a separate fail-closed exclusion.
        runtime.setdefault("ai_position_action_holds", {})[key] = {"proposal_id": proposal_id, "status": status, "operation_id": operation}
        runtime.setdefault("ai_position_action_results", {})[key] = {"proposal_id": proposal_id, "status": status, **deepcopy(result)}
        with closing(self._connect()) as db:
            db.execute("UPDATE position_proposals SET status='INVALIDATED',updated_ms=? WHERE account=? AND market=? AND id<>? AND status='PENDING'", (completed, row["account"], key, proposal_id))
            db.commit()
        try:
            persist_runtime()
        except Exception:
            pass
        return self._get(uid, proposal_id)[1]

    def release(self, uid, market, profile, client, persist_runtime, now_ms):
        now = _time(now_ms); address = _address(profile)
        record = self.reserved_markets(uid, address).get(market)
        if not record or record["status"] not in ("FILLED", "PARTIAL") or self.journal.pending(address):
            raise ValueError("intervention_reconciliation_required")
        all_held = self.reserved_markets(None, address).get(market)
        if not all_held or all_held["status"] not in ("FILLED", "PARTIAL"):
            raise ValueError("intervention_reconciliation_required")
        reader = getattr(client, "reader", None)
        if reader is None:
            class Reader:
                def _info(self, request): return client.info.post("/info", request)
            reader = Reader()
        context = self._context(uid, profile, client, reader, market, now, factors=False)
        _, proposal = self._get(uid, record["proposal_id"])
        result = proposal.get("result") or {}
        if _identity(context["position"]) != _identity(result.get("position_after") or {}) or context["source"] != proposal["payload"]["source_wallet"]:
            raise ValueError("intervention_reconciliation_required")
        # Persist JSON first, SQL still protects until the final commit succeeds.
        runtime = profile.setdefault("runtime", {})
        runtime.setdefault("ai_position_action_holds", {}).pop(market, None)
        persist_runtime()
        with closing(self._connect()) as db:
            db.execute("UPDATE position_proposals SET released_ms=? WHERE user_id=? AND account=? AND market=? AND status IN ('FILLED','PARTIAL') AND released_ms IS NULL",
                       (now, str(uid), address, market))
            db.commit()
        return {"released": True, "market": market, "reason": "verified_copy_resume_user_requested"}
