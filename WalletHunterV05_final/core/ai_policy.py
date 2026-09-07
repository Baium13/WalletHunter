"""Explicit user-confirmed scenario constraints; never a live-trading switch."""
from dataclasses import dataclass, asdict
import math
from core.ai_rescue_policy import RESCUE_TRIGGER_ROE_PCT


@dataclass(frozen=True)
class ReviewPolicy:
    trigger_roe_pct: float = RESCUE_TRIGGER_ROE_PCT
    target_roe_pct: float = 3.0
    failure_roe_pct: float = -120.0
    horizon_hours: int = 24
    minimum_probability: float = .60
    max_extra_slot_fraction: float = .50
    max_additions: int = 4
    require_confirmation: bool = True

    def as_dict(self):
        return asdict(self)


def source_budget(profile, position, total_equity, ownership):
    """Conservative single-source collateral ledger. No cross-slot borrowing.

    Opposing or multiple contributors are ambiguous for an intervention and
    intentionally unavailable until allocation is resolved explicitly.
    """
    from core.ai_review import market_key
    key = market_key(position)
    if not math.isfinite(float(total_equity)) or float(total_equity) <= 0:
        return {"available": False, "reason": "equity_unavailable"}
    record = ownership.get(key) or {}
    sources = record.get("source_targets") or []
    if not record.get("managed") or len(sources) != 1:
        return {"available": False, "reason": "source_unknown_or_shared"}
    wallet = sources[0].get("wallet")
    saved = record.get("position") or {}
    if saved.get("side") != position.get("side") or abs(float(saved.get("size",0))-float(position.get("size",0))) > 1e-10:
        return {"available": False, "reason": "ownership_snapshot_changed"}
    if wallet not in profile.get("leaders", []):
        return {"available": False, "reason": "source_removed"}
    slot = max(0., float(total_equity)) / 3
    reserved = 0.
    for value in ownership.values():
        if not value.get("managed"): continue
        for source in value.get("source_targets") or []:
            if source.get("wallet") == wallet:
                reserved += max(0., float(source.get("margin", 0)))
    policy = ReviewPolicy()
    usage = profile.get("runtime", {}).get("ai_budget_usage", {})
    spent, count = 0., 0
    for usage_key, additions in usage.items():
        attributed = additions.get("source_wallet")
        if not attributed:
            old_sources = (ownership.get(usage_key) or {}).get("source_targets") or []
            attributed = old_sources[0].get("wallet") if len(old_sources) == 1 else None
        if attributed is None:
            return {"available": False, "reason": "prior_budget_usage_source_unknown"}
        if attributed == wallet:
            amount = float(additions.get("extra_margin_usdc", 0))
            number = float(additions.get("additions", 0))
            if not math.isfinite(amount) or amount < 0 or not math.isfinite(number) or number < 0 or not number.is_integer():
                return {"available": False, "reason": "prior_budget_usage_invalid"}
            spent += amount
            count += int(number)
    allowance = min(max(0., slot-reserved), max(0., slot*policy.max_extra_slot_fraction-spent))
    if count >= policy.max_additions: allowance = 0.
    return {"available": True, "source": wallet, "slot_usdc": slot,
            "reserved_usdc": reserved, "remaining_extra_usdc": allowance,
            "extra_spent_usdc": spent, "additions_count": count,
            "additions_remaining": max(0, policy.max_additions-count)}
