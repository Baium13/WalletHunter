"""Restart-safe, offline counterfactual outcomes; never a trading/prediction API.

A scenario describes an explicitly proposed *post-action* position. Its ROE
denominator is the original, pre-intervention risk capital and must not increase
when adding collateral. Realised PnL and costs already paid belong in the frozen
snapshot so reductions cannot erase losses. Costs below are explicit modelling
assumptions, not actual exchange fills or funding settlements.

The caller supplies contiguous, CLOSED forward OHLC bars from ``signal_ms``.
For a signal inside an exchange candle, supply a forward-only partial first bar
(e.g. built from ticks). The encompassing historical candle is not usable: its
high/low might precede the signal. Missing coverage is never labelled a success.

No outcome, win-rate, indicator agreement or sample count is a calibrated
probability. This module deliberately exposes no permission to trade.
"""
import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass


TERMINAL = frozenset({"TARGET", "LOSS", "LIQUIDATION", "HORIZON", "AMBIGUOUS"})
METHOD = "fixed-risk-capital-ohlc-counterfactual-v1"


def _number(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def _integer(value, name, minimum=0):
    parsed = _number(value, name)
    if parsed != int(parsed) or parsed < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return int(parsed)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _gte(value, barrier):
    return value > barrier or math.isclose(value, barrier, rel_tol=1e-12, abs_tol=1e-9)


def _lte(value, barrier):
    return value < barrier or math.isclose(value, barrier, rel_tol=1e-12, abs_tol=1e-9)


@dataclass(frozen=True)
class OutcomePolicy:
    """All thresholds/time periods are explicit, versioned scenario inputs."""
    target_roe_pct: float
    loss_roe_pct: float
    horizon_ms: int
    bar_interval_ms: int
    max_stale_ms: int

    def checked(self):
        target = _number(self.target_roe_pct, "target_roe_pct")
        loss = _number(self.loss_roe_pct, "loss_roe_pct")
        if target <= 0 or loss >= 0 or loss >= target:
            raise ValueError("ROE target must be positive and loss threshold negative")
        horizon = _integer(self.horizon_ms, "horizon_ms", 1)
        interval = _integer(self.bar_interval_ms, "bar_interval_ms", 1)
        stale = _integer(self.max_stale_ms, "max_stale_ms", 1)
        if horizon < interval:
            raise ValueError("Horizon must cover at least one complete bar")
        return {"target_roe_pct": target, "loss_roe_pct": loss,
                "horizon_ms": horizon, "bar_interval_ms": interval,
                "max_stale_ms": stale}


@dataclass(frozen=True)
class OutcomeCosts:
    """USDC entry cost; exit costs in bps; signed estimated funding USDC/hour.

    Positive funding is a payment, negative funding is a credit. Entry costs
    include all proposed immediate-action fees AND slippage. ``paid_costs_usdc``
    in the snapshot must exclude these new costs to avoid double counting.
    """
    entry_cost_usdc: float
    exit_fee_bps: float
    exit_slippage_bps: float
    funding_usdc_hour: float

    def checked(self):
        values = {name: _number(value, name) for name, value in asdict(self).items()}
        if min(values[k] for k in ("entry_cost_usdc", "exit_fee_bps", "exit_slippage_bps")) < 0:
            raise ValueError("Fees and slippage must not be negative")
        if values["exit_fee_bps"] + values["exit_slippage_bps"] >= 10000:
            raise ValueError("Exit costs must total less than 100%")
        return values


def _snapshot(raw, policy):
    # Round-trip makes a deep copy and rejects non-JSON data/NaN even in features.
    s = json.loads(_canonical(raw))
    for name in ("owner_id", "source_review_id", "source_wallet", "market", "action"):
        if not isinstance(s.get(name), str) or not s[name].strip():
            raise ValueError(f"Missing snapshot {name}")
    if s.get("side") not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    s["signal_ms"] = _integer(s["signal_ms"], "signal_ms")
    for name in ("features_asof_ms", "quote_asof_ms"):
        s[name] = _integer(s[name], name)
        age = s["signal_ms"] - s[name]
        if age < 0 or age > policy["max_stale_ms"]:
            raise ValueError(f"{name} is future or stale at signal time")
    if not isinstance(s.get("features"), dict):
        raise ValueError("features must be an immutable at-signal object")
    if s.get("risk_basis") != "original_pre_intervention":
        raise ValueError("Use original_pre_intervention risk basis, not post-action margin")
    for name in ("size", "entry_price", "reference_price", "risk_capital_usdc"):
        s[name] = _number(s[name], name)
        if s[name] <= 0:
            raise ValueError(f"{name} must be positive")
    s["realized_pnl_usdc"] = _number(s["realized_pnl_usdc"], "realized_pnl_usdc")
    s["paid_costs_usdc"] = _number(s["paid_costs_usdc"], "paid_costs_usdc")
    if s["paid_costs_usdc"] < 0:
        raise ValueError("paid_costs_usdc must be nonnegative; put funding credits in realised PnL")
    liq = s.get("liquidation_price")
    if liq is not None:
        s["liquidation_price"] = _number(liq, "liquidation_price")
        if s["liquidation_price"] <= 0:
            raise ValueError("liquidation_price must be positive or null")
        if (s["side"] == "LONG" and s["liquidation_price"] >= s["reference_price"]) or (s["side"] == "SHORT" and s["liquidation_price"] <= s["reference_price"]):
            raise ValueError("Liquidation price is already crossed at the signal")
    return s


def _net(s, costs, price, at_ms):
    direction = 1 if s["side"] == "LONG" else -1
    hours = (at_ms - s["signal_ms"]) / 3600000
    exit_cost = s["size"] * price * (costs["exit_fee_bps"] + costs["exit_slippage_bps"]) / 10000
    funding = hours * costs["funding_usdc_hour"]
    pnl = (s["realized_pnl_usdc"] + direction * s["size"] * (price - s["entry_price"])
           - s["paid_costs_usdc"] - costs["entry_cost_usdc"] - exit_cost - funding)
    return {"net_pnl_usdc": _number(pnl, "calculated PnL"),
            "roe_pct": _number(pnl / s["risk_capital_usdc"] * 100, "calculated ROE"),
            "exit_cost_usdc": _number(exit_cost, "calculated exit costs"),
            "funding_estimate_usdc": _number(funding, "calculated funding")}


def _barrier_price(s, costs, roe, at_ms):
    direction = 1 if s["side"] == "LONG" else -1
    rate = (costs["exit_fee_bps"] + costs["exit_slippage_bps"]) / 10000
    fixed = (s["realized_pnl_usdc"] - s["paid_costs_usdc"] - costs["entry_cost_usdc"]
             - direction * s["size"] * s["entry_price"]
             - costs["funding_usdc_hour"] * (at_ms - s["signal_ms"]) / 3600000)
    return _number((s["risk_capital_usdc"] * roe / 100 - fixed) / (s["size"] * (direction-rate)),
                   "calculated barrier price")


def evaluate_snapshot(snapshot, policy, costs, bars, now_ms):
    """Pure simulator. No snapshots, policies, bars or live accounts are changed.

    Every evaluation replays the full observed path. Callers must backfill all
    bars since signal time, not just bars since the last polling cycle. A final
    result can be reached before ``now_ms`` only with continuous coverage first.
    """
    p = policy.checked() if isinstance(policy, OutcomePolicy) else OutcomePolicy(**policy).checked()
    c = costs.checked() if isinstance(costs, OutcomeCosts) else OutcomeCosts(**costs).checked()
    s = _snapshot(snapshot, p)
    now = _integer(now_ms, "now_ms")
    if now < s["signal_ms"]:
        raise ValueError("Evaluation time predates signal")
    deadline = s["signal_ms"] + p["horizon_ms"]
    observed_until = s["signal_ms"]
    result = {"method": METHOD, "simulated": True, "probability": None,
              "status": "OPEN", "terminal": False, "reason": "waiting_for_closed_bars",
              "signal_ms": s["signal_ms"], "deadline_ms": deadline,
              "observed_until_ms": observed_until, "risk_basis": s["risk_basis"],
              "risk_capital_usdc": s["risk_capital_usdc"], "success": None,
              "calibration_eligible": False, "net_pnl_usdc": None, "roe_pct": None,
              "cost_model": "frozen_entry_exit_costs_and_constant_funding_estimate",
              "liquidation_model": "frozen_snapshot_estimate" if s.get("liquidation_price") else "unavailable"}

    def pending(reason, status="DATA_GAP"):
        return dict(result, status=status, reason=reason, observed_until_ms=observed_until)

    def finish(status, reason, price=None, at_ms=None, **extra):
        measured = _net(s, c, price, at_ms) if price is not None else {}
        # Simulated, non-ambiguous samples can be input to a *separate* validated
        # study. They are NOT themselves a calibrated success probability.
        return dict(result, **measured, **extra, status=status, terminal=True,
                    reason=reason, observed_until_ms=at_ms or observed_until,
                    success=(status == "TARGET") if status != "AMBIGUOUS" else None,
                    calibration_eligible=status != "AMBIGUOUS" and s.get("liquidation_price") is not None,
                    exit_price=price)

    rows = {}
    for raw in bars:
        start, end = _integer(raw["t"], "bar start"), _integer(raw["T"], "bar close")
        if end < start:
            raise ValueError("Negative candle duration")
        # Never use a historical high/low from before the signal, an unclosed
        # candle, or a candle extending beyond the evaluation horizon.
        if start < s["signal_ms"] or end >= now or end >= deadline:
            continue
        row = {"t": start, "T": end, **{k: _number(raw[k], f"bar {k}") for k in ("o", "h", "l", "c")}}
        if min(row[k] for k in ("o", "h", "l", "c")) <= 0 or not row["l"] <= min(row["o"], row["c"]) <= max(row["o"], row["c"]) <= row["h"]:
            raise ValueError("Invalid OHLC range")
        if start in rows and rows[start] != row:
            return pending("conflicting_duplicate_candles")
        rows[start] = row
    rows = sorted(rows.values(), key=lambda row: row["t"])
    result["market_data_fingerprint"] = hashlib.sha256(_canonical(rows).encode()).hexdigest()
    result["closed_bars_received"] = len(rows)
    last = None
    for row in rows:
        if row["t"] != observed_until:
            return pending("initial_partial_candle_unobservable" if last is None else "missing_or_overlapping_candles")
        duration = row["T"] - row["t"] + 1
        # A forward partial first/final candle is permitted; ordinary candles
        # must match the declared bar interval. No arbitrary internal shortcuts.
        if duration > p["bar_interval_ms"] or (last is not None and duration != p["bar_interval_ms"] and row["T"] != deadline-1):
            return pending("unexpected_candle_duration")
        start, end = row["t"], row["T"]+1
        liq = s.get("liquidation_price")
        direction = 1 if s["side"] == "LONG" else -1
        opened = _net(s, c, row["o"], start)
        liq_open = liq is not None and (row["o"] - liq) * direction <= 0
        if liq_open:
            return finish("LIQUIDATION", "liquidation_gap_at_open", row["o"], start)
        if _lte(opened["roe_pct"], p["loss_roe_pct"]):
            return finish("LOSS", "loss_gap_at_open", row["o"], start)
        if _gte(opened["roe_pct"], p["target_roe_pct"]):
            # A favourable opening gap is priced at the target, not granted
            # extra theoretical profit that might be impossible to execute.
            return finish("TARGET", "target_at_open", _barrier_price(s, c, p["target_roe_pct"], start), start)
        favourable, adverse = (row["h"], row["l"]) if direction == 1 else (row["l"], row["h"])
        favourable_roes = [_net(s, c, favourable, when)["roe_pct"] for when in (start, end)]
        adverse_roes = [_net(s, c, adverse, when)["roe_pct"] for when in (start, end)]
        target_possible = _gte(max(favourable_roes), p["target_roe_pct"])
        target_certain = _gte(min(favourable_roes), p["target_roe_pct"])
        loss_possible = _lte(min(adverse_roes), p["loss_roe_pct"])
        loss_certain = _lte(max(adverse_roes), p["loss_roe_pct"])
        liq_possible = liq is not None and (adverse-liq)*direction <= 0
        if target_possible and (loss_possible or liq_possible):
            return finish("AMBIGUOUS", "both_barriers_in_same_candle", at_ms=end,
                          conservative_terminal="LIQUIDATION" if liq_possible else "LOSS",
                          event_time_bounds_ms=[start, end],
                          roe_bounds_pct=[min(adverse_roes), max(favourable_roes)])
        if target_possible != target_certain or loss_possible != loss_certain:
            return finish("AMBIGUOUS", "unknown_intrabar_funding_timing", at_ms=end,
                          conservative_terminal="LOSS" if loss_possible else "NO_TARGET",
                          event_time_bounds_ms=[start, end],
                          roe_bounds_pct=[min(adverse_roes), max(favourable_roes)])
        if liq_possible or loss_certain:
            loss_price = _barrier_price(s, c, p["loss_roe_pct"], end)
            # If the loss barrier is between the opening price and liquidation,
            # it would be encountered first on a continuous intrabar path.
            loss_before_liq = loss_certain and (liq is None or (loss_price-liq)*direction > 0)
            return (finish("LOSS", "loss_barrier", loss_price, end, event_time_bounds_ms=[start, end]) if loss_before_liq
                    else finish("LIQUIDATION", "liquidation_barrier_before_loss", liq, end,
                                event_time_bounds_ms=[start, end]))
        if target_certain:
            return finish("TARGET", "target_barrier", _barrier_price(s, c, p["target_roe_pct"], end), end,
                          event_time_bounds_ms=[start, end])
        observed_until = end
        last = row
        if end == deadline:
            return finish("HORIZON", "horizon_without_target", row["c"], end)
    if observed_until < deadline and now >= deadline:
        return pending("missing_coverage_at_horizon")
    if now - observed_until > p["max_stale_ms"]:
        return pending("stale_forward_market_data", "STALE")
    if last:
        return dict(result, reason="horizon_not_reached", observed_until_ms=observed_until,
                    mark_to_market=_net(s, c, last["c"], observed_until))
    return result


class AiOutcomes:
    """Append-only scenario snapshots plus restart-safe evaluation results."""
    def __init__(self, root):
        self.path = os.path.join(root, "data", "ai_outcomes.sqlite3")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scenarios(
                  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
                  snapshot TEXT NOT NULL, policy TEXT NOT NULL, costs TEXT NOT NULL,
                  fingerprint TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evaluations(
                  scenario_id TEXT PRIMARY KEY REFERENCES scenarios(id),
                  checked_ms INTEGER NOT NULL, terminal INTEGER NOT NULL, result TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON scenarios
                  BEGIN SELECT RAISE(ABORT, 'Scenario snapshots are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON scenarios
                  BEGIN SELECT RAISE(ABORT, 'Scenario snapshots are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS terminal_no_update BEFORE UPDATE ON evaluations
                  WHEN OLD.terminal = 1 BEGIN SELECT RAISE(ABORT, 'Terminal outcomes are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS terminal_no_delete BEFORE DELETE ON evaluations
                  WHEN OLD.terminal = 1 BEGIN SELECT RAISE(ABORT, 'Terminal outcomes are immutable'); END;
                CREATE INDEX IF NOT EXISTS outcomes_owner ON scenarios(owner_id,created_ms);
            """)
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def create(self, scenario_id, snapshot, policy, costs):
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise ValueError("An idempotent scenario ID is required")
        p = policy.checked()
        c = costs.checked()
        s = _snapshot(snapshot, p)
        encoded = [_canonical(value) for value in (s, p, c)]
        digest = hashlib.sha256("\n".join(encoded).encode()).hexdigest()
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT fingerprint FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError("Scenario ID already exists with a different immutable snapshot")
                return scenario_id
            db.execute("INSERT INTO scenarios VALUES(?,?,?,?,?,?,?)",
                       (scenario_id, s["owner_id"], s["signal_ms"], *encoded, digest))
            db.commit()
        return scenario_id

    def get(self, owner_id, scenario_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT s.*,e.result FROM scenarios s LEFT JOIN evaluations e ON s.id=e.scenario_id "
                             "WHERE s.id=? AND s.owner_id=?", (scenario_id, str(owner_id))).fetchone()
        if not row:
            raise ValueError("Scenario not found")
        data = dict(row)
        for key in ("snapshot", "policy", "costs", "result"):
            data[key] = json.loads(data[key]) if data[key] is not None else None
        return data

    def evaluate(self, owner_id, scenario_id, bars, now_ms):
        scenario = self.get(owner_id, scenario_id)
        if scenario["result"] and scenario["result"]["terminal"]:
            return scenario["result"]
        result = evaluate_snapshot(scenario["snapshot"], scenario["policy"], scenario["costs"], bars, now_ms)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT checked_ms,terminal,result FROM evaluations WHERE scenario_id=?", (scenario_id,)).fetchone()
            # A concurrent worker or restart may already have reached a later
            # observation. Never regress or replace a frozen terminal outcome.
            if old and (old["terminal"] or old["checked_ms"] >= now_ms):
                return json.loads(old["result"])
            db.execute("INSERT INTO evaluations VALUES(?,?,?,?) ON CONFLICT(scenario_id) DO UPDATE SET "
                       "checked_ms=excluded.checked_ms,terminal=excluded.terminal,result=excluded.result",
                       (scenario_id, now_ms, int(result["terminal"]), _canonical(result)))
            db.commit()
        return result

    def pending(self, owner_id, limit=100):
        """Return only this owner's unfinished snapshots for a polling worker."""
        limit = _integer(limit, "limit", 1)
        if limit > 1000:
            raise ValueError("limit must not exceed 1000")
        with closing(self.connect()) as db:
            ids = db.execute("SELECT s.id FROM scenarios s LEFT JOIN evaluations e ON e.scenario_id=s.id "
                             "WHERE s.owner_id=? AND COALESCE(e.terminal,0)=0 ORDER BY s.created_ms LIMIT ?",
                             (str(owner_id), limit)).fetchall()
        return [self.get(owner_id, row[0]) for row in ids]

    def summary(self, owner_id=None):
        query = "SELECT e.result FROM scenarios s LEFT JOIN evaluations e ON e.scenario_id=s.id"
        args = ()
        if owner_id is not None:
            query += " WHERE s.owner_id=?"
            args = (str(owner_id),)
        with closing(self.connect()) as db:
            rows = db.execute(query, args).fetchall()
        counts = {}
        for row in rows:
            status = json.loads(row[0])["status"] if row[0] else "UNEVALUATED"
            counts[status] = counts.get(status, 0)+1
        # Pool counts only. No cross-account positions/features are returned.
        return {"scenarios": len(rows), "status_counts": counts, "simulated": True,
                "probability": None, "ready_for_live_trading": False,
                "warning": "Counterfactual outcomes are not calibrated probabilities or actual fills."}
