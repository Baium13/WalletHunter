"""Delayed-start HOLD research adapter. Never submits orders or predicts odds.

Register an immutable intent when a review is produced. Its study begins at the
NEXT 15m candle open, not at review time. Bind only that future OPEN price once
the bar closes; every feature, position and cost assumption remains frozen at
review time. Results explicitly exclude the unobserved waiting interval.

Missing ownership, original capital or explicit research-cost assumptions is
recorded as unavailable, never filled with manufactured values. This study is
not an execution simulation at notification time, a calibrated model or an AI
intervention. Its samples must not be mixed with such data without adjustment.
"""
import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing

from core.ai_outcomes import AiOutcomes, OutcomeCosts, OutcomePolicy
from core.ai_rescue_policy import RESCUE_TRIGGER_ROE_PCT


METHOD = "delayed-next-15m-open-HOLD-v1"
INTERVAL_MS = 900000
# Explicit user research request; this is not permission or a trading default.
USER_RESEARCH_POLICY = OutcomePolicy(3, -120, 86400000, INTERVAL_MS, 2100000)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value, name, positive=False):
    if isinstance(value, bool): raise ValueError(f"invalid_{name}")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0): raise ValueError(f"invalid_{name}")
    return result


def _stamp(value, name):
    stamp = _number(value, name)
    if stamp < 0 or stamp != int(stamp): raise ValueError(f"invalid_{name}")
    return int(stamp)


def _market(position):
    coin, dex = str(position.get("coin") or ""), str(position.get("dex") or "")
    if not coin: raise ValueError("missing_market")
    if ":" in coin:
        prefix = coin.split(":", 1)[0]
        if dex and dex != prefix: raise ValueError("conflicting_market_dex")
        dex = prefix
    elif dex: coin = f"{dex}:{coin}"
    return f"{coin}|{dex}"


class AiResearch:
    def __init__(self, root, policy=USER_RESEARCH_POLICY):
        self.path = os.path.join(root, "data", "ai_research.sqlite3")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.policy = policy.checked()
        if self.policy["bar_interval_ms"] != INTERVAL_MS:
            raise ValueError("Delayed adapter uses 15m exchange candles")
        self.outcomes = AiOutcomes(root)
        self._cache = {}
        with closing(self.connect()) as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS research_intents(
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, account TEXT NOT NULL,
                review_id TEXT NOT NULL, market TEXT NOT NULL, created_ms INTEGER NOT NULL,
                start_ms INTEGER NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS research_progress(
                id TEXT PRIMARY KEY REFERENCES research_intents(id), status TEXT NOT NULL,
                reason TEXT NOT NULL, scenario_id TEXT, next_poll_ms INTEGER NOT NULL,
                checked_ms INTEGER NOT NULL DEFAULT 0);
              CREATE TRIGGER IF NOT EXISTS research_intent_no_update BEFORE UPDATE ON research_intents
                BEGIN SELECT RAISE(ABORT,'Research intents are immutable'); END;
              CREATE TRIGGER IF NOT EXISTS research_intent_no_delete BEFORE DELETE ON research_intents
                BEGIN SELECT RAISE(ABORT,'Research intents are immutable'); END;
              CREATE INDEX IF NOT EXISTS research_owner ON research_intents(owner_id,created_ms);
            """)
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def register(self, owner_id, account, review_id, position, review, journal, *,
                 risk_basis=None, costs=None, costs_source=None, now_ms):
        """Freeze an accepted review; invalid/missing research inputs stay visible.

        ``risk_basis`` must contain basis='original_pre_intervention',
        risk_capital_usdc, realized_pnl_usdc, paid_costs_usdc, and nonempty evidence.
        For a first intervention the caller may explicitly freeze its verified
        pre-intervention exchange margin; do not recalculate it after additions.
        An unknown historical realised PnL/fee must NOT be passed as known zero.
        """
        now = _stamp(now_ms, "review_time")
        uid, account, review_id = str(owner_id), str(account).lower(), str(review_id)
        if not uid or not account or not review_id: raise ValueError("Missing research identity")
        identity = f"{uid}|{account}|{review_id}|{METHOD}"
        research_id = hashlib.sha256(identity.encode()).hexdigest()
        start = (now//INTERVAL_MS+1)*INTERVAL_MS
        status, reason, market = "WAITING_START", "waiting_for_first_closed_forward_bar", ""
        payload = {"method": METHOD, "review_ms": now, "study_start_ms": start,
                   "study_scope": "HOLD from next 15m open; excludes review-to-start interval",
                   "probability": None, "policy": self.policy, "account": account,
                   "owner_id": uid, "source_review_id": review_id,
                   "cost_assumptions_are_actual_fills": False}
        try:
            market = _market(position)
            p = {k: position.get(k) for k in ("coin", "dex", "side", "size", "entry_price", "roe",
                                              "margin_used", "liquidation_price")}
            if p["side"] not in ("LONG", "SHORT"): raise ValueError("invalid_side")
            for k in ("size", "entry_price", "margin_used"):
                p[k] = _number(p[k], k, positive=True)
            if _number(p["roe"], "roe") > RESCUE_TRIGGER_ROE_PCT: raise ValueError("roe_above_review_trigger")
            liq = _number(p["liquidation_price"], "liquidation_price", positive=True)
            p["liquidation_price"] = liq
            price = _number(review["price"], "review_price", positive=True)
            factors = json.loads(_json(review["factors"]))
            features_at = _stamp(factors["asof_ms"], "feature_time")
            closed_at = _stamp(factors["candle_close_ms"], "feature_candle_time")
            if features_at > now or closed_at >= now: raise ValueError("future_or_unclosed_features")
            if now-min(features_at, closed_at) > self.policy["max_stale_ms"]:
                raise ValueError("stale_review_features")
            # Journal existence is mandatory. Current live position alone is
            # not proof that a copied wallet owns a share of this net exposure.
            owned = journal.owned(account).get(market)
            if not owned or not owned.get("managed"): raise ValueError("source_ownership_unavailable")
            if owned.get("side") != p["side"] or not math.isclose(
                    _number(owned.get("size"), "owned_size"), p["size"], rel_tol=1e-8, abs_tol=1e-12):
                raise ValueError("ownership_position_mismatch")
            sources = owned.get("source_targets") or []
            wallets = {str(s.get("wallet") or "").lower() for s in sources}
            if len(wallets) != 1 or "" in wallets: raise ValueError("mixed_or_missing_source_attribution")
            direction = 1 if p["side"] == "LONG" else -1
            if any(_number(s.get("signed_notional"), "source_notional")*direction <= 0 for s in sources):
                raise ValueError("source_direction_conflict")
            slot_budgets = [_number(s.get("slot_budget"), "source_slot_budget", positive=True) for s in sources]
            if any(not math.isclose(v, slot_budgets[0], rel_tol=1e-8) for v in slot_budgets):
                raise ValueError("inconsistent_source_slot_budget")
            if not risk_basis or risk_basis.get("basis") != "original_pre_intervention" or not risk_basis.get("evidence"):
                raise ValueError("original_risk_basis_unavailable")
            risk = {"basis": "original_pre_intervention", "evidence": str(risk_basis["evidence"]),
                    "risk_capital_usdc": _number(risk_basis["risk_capital_usdc"], "risk_capital_usdc", positive=True),
                    "realized_pnl_usdc": _number(risk_basis["realized_pnl_usdc"], "realized_pnl_usdc"),
                    "paid_costs_usdc": _number(risk_basis["paid_costs_usdc"], "paid_costs_usdc")}
            if risk["paid_costs_usdc"] < 0: raise ValueError("invalid_paid_costs_usdc")
            if costs is None or not costs_source: raise ValueError("explicit_cost_model_unavailable")
            model_costs = costs.checked() if isinstance(costs, OutcomeCosts) else OutcomeCosts(**costs).checked()
            payload.update(position=p, review_price=price, features=factors, features_asof_ms=features_at,
                           source_wallet=next(iter(wallets)), source_slot_budget_usdc=slot_budgets[0],
                           source_ownership=owned, risk=risk, costs=model_costs,
                           costs_source=str(costs_source))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            status, reason = "UNAVAILABLE", str(exc) or type(exc).__name__
        payload["validation"] = {"status": status, "reason": reason}
        encoded = _json(payload)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT fingerprint FROM research_intents WHERE id=?", (research_id,)).fetchone()
            if old:
                if old[0] != digest: raise ValueError("Research review already frozen with different inputs")
                return self.get(uid, research_id)
            db.execute("INSERT INTO research_intents VALUES(?,?,?,?,?,?,?,?,?)",
                       (research_id, uid, account, review_id, market, now, start, encoded, digest))
            db.execute("INSERT INTO research_progress(id,status,reason,next_poll_ms) VALUES(?,?,?,?)",
                       (research_id, status, reason, start+INTERVAL_MS))
            db.commit()
        return self.get(uid, research_id)

    def get(self, owner_id, research_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT i.*,p.status,p.reason,p.scenario_id,p.next_poll_ms,p.checked_ms "
                             "FROM research_intents i JOIN research_progress p USING(id) WHERE i.id=? AND i.owner_id=?",
                             (research_id, str(owner_id))).fetchone()
        if row is None: raise ValueError("Research intent not found")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _progress(self, rid, status, reason, scenario, now):
        with closing(self.connect()) as db:
            db.execute("UPDATE research_progress SET status=?,reason=?,scenario_id=?,next_poll_ms=?,checked_ms=? "
                       "WHERE id=? AND status NOT IN ('COMPLETE','UNAVAILABLE') AND checked_ms<=?",
                       (status, reason, scenario, (now//INTERVAL_MS+1)*INTERVAL_MS+5000, now, rid, now))
            db.commit()

    def _candles(self, reader, market, start, end, now):
        key = (market, start, end//INTERVAL_MS)
        cache = self._cache.get(key)
        if cache and now-cache[0] < 60000: return cache[1]
        rows = reader._info({"type": "candleSnapshot", "req": {
            "coin": market.split("|")[0], "interval": "15m", "startTime": start, "endTime": end}})
        if not isinstance(rows, list): raise ValueError("Candle response is not a list")
        self._cache = {k: v for k, v in self._cache.items() if now-v[0] < 60000}
        self._cache[key] = (now, rows)
        return rows

    def poll(self, owner_id, reader, now_ms, limit=40):
        """At most one request per market/window; no API secrets or orders used."""
        now = _stamp(now_ms, "poll_time")
        limit = int(limit)
        if not 1 <= limit <= 100: raise ValueError("limit must be between 1 and 100")
        with closing(self.connect()) as db:
            due = db.execute("SELECT i.id FROM research_intents i JOIN research_progress p USING(id) "
                             "WHERE i.owner_id=? AND p.status NOT IN ('COMPLETE','UNAVAILABLE') "
                             "AND p.next_poll_ms<=? ORDER BY i.start_ms LIMIT ?", (str(owner_id), now, limit)).fetchall()
        studies = [self.get(owner_id, row[0]) for row in due]
        groups = {}
        # Bucket long outages to keep each public request below 2048 candles.
        for row in studies:
            bucket = row["start_ms"]//(1024*INTERVAL_MS)
            groups.setdefault((row["market"], bucket), []).append(row)
        completed = []
        for (market, _), group in groups.items():
            first = min(r["start_ms"] for r in group)
            end = min(now, max(r["start_ms"]+r["payload"]["policy"]["horizon_ms"] for r in group))
            try:
                rows = self._candles(reader, market, first, end, now)
            except Exception:
                for row in group: self._progress(row["id"], "WAITING_DATA", "public_candle_request_failed", row["scenario_id"], now)
                continue
            for row in group:
                try:
                    result = self._observe(row, rows, now)
                    completed.append(result)
                except (ValueError, KeyError, TypeError, OverflowError) as exc:
                    self._progress(row["id"], "UNAVAILABLE", f"invalid_research_data: {exc}", row["scenario_id"], now)
        return completed

    def _observe(self, row, bars, now):
        p = row["payload"]
        start, deadline = row["start_ms"], row["start_ms"]+p["policy"]["horizon_ms"]
        usable = [b for b in bars if int(b["t"]) >= start and int(b["T"]) < now and int(b["T"]) < deadline]
        opening = [b for b in usable if int(b["t"]) == start]
        if not opening:
            # Permit later backfill, but do not repeatedly poll permanently lost
            # market history beyond an additional 24h recovery window.
            status = "UNAVAILABLE" if now > deadline+86400000 else "WAITING_DATA"
            self._progress(row["id"], status, "first_forward_open_unavailable", row["scenario_id"], now)
            return {"id": row["id"], "status": status, "reason": "first_forward_open_unavailable"}
        if any(_json(b) != _json(opening[0]) for b in opening): raise ValueError("Conflicting first-bar observations")
        price = _number(opening[0]["o"], "delayed_open", positive=True)
        pos, risk = p["position"], p["risk"]
        direction = 1 if pos["side"] == "LONG" else -1
        costs = p["costs"]
        net_start = (risk["realized_pnl_usdc"]+direction*pos["size"]*(price-pos["entry_price"])
                     -risk["paid_costs_usdc"]-costs["entry_cost_usdc"]
                     -pos["size"]*price*(costs["exit_fee_bps"]+costs["exit_slippage_bps"])/10000)
        initial_roe = net_start/risk["risk_capital_usdc"]*100
        if (not math.isfinite(initial_roe) or initial_roe >= p["policy"]["target_roe_pct"]
                or initial_roe <= p["policy"]["loss_roe_pct"]
                or (price-pos["liquidation_price"])*direction <= 0):
            self._progress(row["id"], "UNAVAILABLE", "already_outside_barriers_at_delayed_start", None, now)
            return {"id": row["id"], "status": "UNAVAILABLE", "reason": "already_outside_barriers_at_delayed_start"}
        scenario_id = "delayed-HOLD:"+row["id"]
        snapshot = {
            "owner_id": row["owner_id"], "source_review_id": row["review_id"], "source_wallet": p["source_wallet"],
            "market": row["market"], "action": "HOLD", "side": pos["side"], "size": pos["size"],
            "entry_price": pos["entry_price"], "reference_price": price, "signal_ms": start,
            "features_asof_ms": p["features_asof_ms"], "quote_asof_ms": start, "features": p["features"],
            "risk_basis": risk["basis"], "risk_capital_usdc": risk["risk_capital_usdc"],
            "realized_pnl_usdc": risk["realized_pnl_usdc"], "paid_costs_usdc": risk["paid_costs_usdc"],
            "liquidation_price": pos["liquidation_price"], "research_method": METHOD,
            "original_review_ms": row["created_ms"], "delayed_start_ms": start-row["created_ms"],
            "initial_study_roe_pct": initial_roe, "costs_source": p["costs_source"],
            "warning": "HOLD research from next15m OPEN, not suggestion-time performance or actual execution."}
        self.outcomes.create(scenario_id, snapshot, OutcomePolicy(**p["policy"]), OutcomeCosts(**costs))
        result = self.outcomes.evaluate(row["owner_id"], scenario_id, usable, now)
        status = "COMPLETE" if result["terminal"] else "OBSERVING"
        if not result["terminal"] and now > deadline+86400000:
            status = "UNAVAILABLE"
        self._progress(row["id"], status, result["reason"], scenario_id, now)
        return {"id": row["id"], "status": status, "outcome": result}

    def summary(self, owner_id):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT p.status,p.reason,COUNT(*) AS n FROM research_intents i "
                              "JOIN research_progress p USING(id) WHERE i.owner_id=? GROUP BY p.status,p.reason",
                              (str(owner_id),)).fetchall()
        return {"method": METHOD, "simulated": True, "probability": None, "ready_for_live_trading": False,
                "studies": sum(r["n"] for r in rows), "groups": [dict(r) for r in rows],
                "outcomes": self.outcomes.summary(owner_id),
                "warning": "Delayed HOLD studies do not measure notification-time performance or AI intervention."}
