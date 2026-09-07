"""Action-specific empirical research gate, NOT an intelligent trading model.

Input records must be verified, immutable forward studies. This module does not
turn HOLD trajectories into labels for averaging, invent odds from indicators,
fit an LLM or authorize orders. Even a passed gate is only provisional evidence
for the exact action variant/risk bucket/cost policy studied.

Split time and policy must be registered before held-out outcomes exist. A full
24-hour label purge is enforced, as is disjoint account+market grouping across
train/test. Only the earliest observation per group is counted, independent of
its outcome. Binomial independence is still an assumption: market-wide regime
correlation cannot be eliminated by these elementary checks.

Wilson interval reference:
https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm
"""
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from statistics import NormalDist


DAY_MS = 86_400_000
METHOD = "purged-grouped-constant-rate-baseline-v1"
ACTIONS = frozenset({"HOLD", "AVERAGE", "REDUCE", "ADD_MARGIN", "LOWER_LEVERAGE"})


def _num(value, name):
    if isinstance(value, bool):
        raise ValueError(f"invalid_{name}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"invalid_{name}")
    return result


def _stamp(value, name):
    value = _num(value, name)
    if value < 0 or value != int(value):
        raise ValueError(f"invalid_{name}")
    return int(value)


@dataclass(frozen=True)
class ProbabilityPolicy:
    action: str
    action_variant: str
    risk_bucket: str
    scenario_id: str
    cost_model_id: str
    registered_ms: int
    split_ms: int
    registration_evidence: str
    min_train_groups: int = 50
    min_test_groups: int = 50
    purge_ms: int = DAY_MS
    horizon_ms: int = DAY_MS
    target_roe_pct: float = 3.0
    loss_roe_pct: float = -120.0
    min_probability: float = .60
    confidence: float = .95
    max_calibration_gap: float = .10

    def checked(self):
        p = asdict(self)
        if p["action"] not in ACTIONS:
            raise ValueError("unsupported_action")
        for field in ("action_variant", "risk_bucket", "scenario_id", "cost_model_id", "registration_evidence"):
            if not isinstance(p[field], str) or not p[field].strip():
                raise ValueError(f"missing_{field}")
        for field in ("registered_ms", "split_ms", "min_train_groups", "min_test_groups", "purge_ms", "horizon_ms"):
            p[field] = _stamp(p[field], field)
        if p["registered_ms"] >= p["split_ms"]:
            raise ValueError("split_must_be_registered_before_holdout")
        if p["purge_ms"] < DAY_MS or p["horizon_ms"] < DAY_MS:
            raise ValueError("full_24h_horizon_and_purge_required")
        if min(p["min_train_groups"], p["min_test_groups"]) < 30:
            raise ValueError("at_least_30_groups_per_partition_required")
        for field in ("target_roe_pct", "loss_roe_pct", "min_probability", "confidence", "max_calibration_gap"):
            p[field] = _num(p[field], field)
        if not 0 < p["target_roe_pct"] or not p["loss_roe_pct"] < 0:
            raise ValueError("invalid_target_or_loss_policy")
        if not .60 <= p["min_probability"] < 1 or not .95 <= p["confidence"] < 1:
            raise ValueError("probability_or_confidence_below_safety_policy")
        if not 0 <= p["max_calibration_gap"] <= .10:
            raise ValueError("invalid_calibration_gap_policy")
        return p


def wilson_interval(successes, count, confidence=.95):
    n, wins = _stamp(count, "count"), _stamp(successes, "successes")
    confidence = _num(confidence, "confidence")
    if wins > n or n == 0 or not 0 < confidence < 1:
        raise ValueError("invalid_binomial_sample")
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    rate = wins / n
    denominator = 1 + z*z/n
    center = (rate + z*z/(2*n)) / denominator
    half = z * math.sqrt(rate*(1-rate)/n + z*z/(4*n*n)) / denominator
    return max(0., center-half), min(1., center+half)


def _sample(raw, p, now):
    """Validate a complete compatible record; do not infer missing evidence."""
    for key in ("id", "account", "market", "evidence_id"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValueError(f"missing_{key}")
    if raw.get("calibration_eligible") is not True or raw.get("costs_included") is not True:
        raise ValueError("ineligible_or_missing_cost_evidence")
    if raw.get("risk_basis") != "original_pre_intervention":
        raise ValueError("invalid_risk_basis")
    times = {k: _stamp(raw[k], k) for k in ("signal_ms", "features_asof_ms", "created_ms",
                                          "deadline_ms", "observed_until_ms", "labelled_ms")}
    signal, deadline = times["signal_ms"], times["deadline_ms"]
    if times["created_ms"] > signal or times["features_asof_ms"] > signal:
        raise ValueError("future_features_or_retrospective_scenario")
    if deadline != signal+p["horizon_ms"]:
        raise ValueError("incompatible_horizon")
    # An early TARGET without full subsequent horizon coverage is not enough:
    # it must not be selectively admitted while slower failures are unfinished.
    if now < deadline or times["observed_until_ms"] < deadline or times["labelled_ms"] < deadline:
        raise ValueError("incomplete_forward_horizon")
    if times["observed_until_ms"] > times["labelled_ms"] or times["labelled_ms"] > now:
        raise ValueError("future_or_inconsistent_label")
    for field in ("target_roe_pct", "loss_roe_pct"):
        if not math.isclose(_num(raw[field], field), p[field], rel_tol=0, abs_tol=1e-12):
            raise ValueError("incompatible_target_or_loss")
    if raw.get("status") not in {"TARGET", "LOSS", "LIQUIDATION", "HORIZON"}:
        raise ValueError("ambiguous_or_unfinished_outcome")
    success = raw.get("success")
    if not isinstance(success, bool) or success != (raw["status"] == "TARGET"):
        raise ValueError("inconsistent_success_label")
    gross, net, capital = (_num(raw[k], k) for k in ("gross_pnl_usdc", "net_pnl_usdc", "risk_capital_usdc"))
    if capital <= 0:
        raise ValueError("invalid_risk_capital")
    costs = raw["costs"]
    fees, slippage, funding = (_num(costs[k], k) for k in ("fees_usdc", "slippage_usdc", "funding_usdc"))
    if fees < 0 or slippage < 0:
        raise ValueError("negative_fee_or_slippage")
    expected_net = gross-fees-slippage-funding
    if not math.isclose(net, expected_net, rel_tol=1e-9, abs_tol=1e-8):
        raise ValueError("net_outcome_does_not_include_declared_costs")
    roe = net/capital*100
    if success and roe < p["target_roe_pct"]-1e-9:
        raise ValueError("positive_label_below_net_target")
    return {"id": raw["id"], "group": (raw["account"].strip().lower(), raw["market"].strip().lower()),
            "signal_ms": signal, "deadline_ms": deadline, "labelled_ms": times["labelled_ms"],
            "success": success, "net_roe_pct": roe}


def evaluate_probability(records, policy, *, now_ms):
    """Evaluate one preregistered empirical study without writing any state.

Required row fields are validated in ``_sample``. Compatibility fields must
exactly match policy: action, action_variant, risk_bucket, scenario_id and
cost_model_id. The adapter must provide these from immutable at-signal inputs,
not retroactively assign a favourable bucket. No adapter from today's HOLD-only
outcomes is supplied, intentionally: those cannot validate intervention actions.
    """
    p = policy.checked() if isinstance(policy, ProbabilityPolicy) else ProbabilityPolicy(**policy).checked()
    now = _stamp(now_ms, "now_ms")
    if now < p["registered_ms"]:
        raise ValueError("study_registration_is_in_future")
    result = {"method": METHOD, "eligible": False, "ready_for_live_trading": False,
              "probability": None, "lower_bound": None, "upper_bound": None,
              "training_baseline_probability": None, "calibration_gap": None,
              "heldout_brier": None, "mean_net_roe_pct": None,
              "train_samples": 0, "test_samples": 0, "train_successes": 0, "test_successes": 0,
              "reason": "unavailable", "rejected": {}, "policy": p,
              "study_fingerprint": hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest(),
              "warning": "Empirical constant-rate research baseline, not a calibrated intelligent model or live trading permission. Regime dependence and correlated markets remain."}
    rejected, eligible, seen = Counter(), [], {}
    matching = 0
    for raw in records:
        if not isinstance(raw, dict):
            rejected["invalid_record"] += 1; continue
        if any(raw.get(k) != p[k] for k in ("action", "action_variant", "risk_bucket", "scenario_id", "cost_model_id")):
            rejected["incompatible_action_risk_or_policy"] += 1; continue
        matching += 1
        try:
            sample = _sample(raw, p, now)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            rejected[str(exc)] += 1; continue
        if sample["id"] in seen:
            if seen[sample["id"]] != sample:
                result.update(reason="conflicting_duplicate_evidence", rejected=dict(rejected))
                return result
            rejected["duplicate_id"] += 1; continue
        seen[sample["id"]] = sample
        eligible.append(sample)
    eligible.sort(key=lambda r: (r["signal_ms"], r["id"]))
    train, test = [], []
    train_groups, test_groups = set(), set()
    for row in eligible:
        if row["deadline_ms"] <= p["split_ms"]-p["purge_ms"] and row["labelled_ms"] < p["split_ms"]:
            if row["group"] in train_groups:
                rejected["repeated_training_group"] += 1
            else:
                train.append(row); train_groups.add(row["group"])
        elif row["signal_ms"] < p["split_ms"]:
            rejected["24h_purge_or_unavailable_training_label"] += 1
    for row in eligible:
        if row["signal_ms"] < p["split_ms"]:
            continue
        if row["group"] in train_groups:
            rejected["train_test_group_leakage"] += 1
        elif row["group"] in test_groups:
            rejected["repeated_test_group"] += 1
        else:
            test.append(row); test_groups.add(row["group"])
    result.update(train_samples=len(train), test_samples=len(test),
                  train_successes=sum(r["success"] for r in train), test_successes=sum(r["success"] for r in test),
                  rejected=dict(rejected),
                  latest_training_deadline_ms=max((r["deadline_ms"] for r in train), default=None),
                  earliest_test_signal_ms=min((r["signal_ms"] for r in test), default=None))
    if not matching:
        result["reason"] = "no_matching_action_risk_samples"; return result
    if len(train) < p["min_train_groups"] or len(test) < p["min_test_groups"]:
        result["reason"] = "insufficient_independent_matured_groups"; return result
    baseline = result["train_successes"] / len(train)
    probability = result["test_successes"] / len(test)
    lower, upper = wilson_interval(result["test_successes"], len(test), p["confidence"])
    gap = abs(probability-baseline)
    mean_net_roe = sum(r["net_roe_pct"] for r in test) / len(test)
    result.update(probability=probability, lower_bound=lower, upper_bound=upper,
                  training_baseline_probability=baseline, calibration_gap=gap,
                  heldout_brier=sum((float(r["success"])-baseline)**2 for r in test)/len(test),
                  mean_net_roe_pct=mean_net_roe)
    if gap > p["max_calibration_gap"] + 1e-12:
        result["reason"] = "training_holdout_rate_drift"
    elif lower < p["min_probability"]:
        result["reason"] = "heldout_lower_bound_below_required_probability"
    elif mean_net_roe <= 0:
        result["reason"] = "nonpositive_heldout_net_expectancy"
    else:
        result.update(eligible=True, reason="empirical_evidence_gate_passed")
    return result
