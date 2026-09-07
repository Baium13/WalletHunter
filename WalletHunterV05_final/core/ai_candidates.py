"""Pure, OFFLINE candidate accounting. No SDK calls, orders or probability model.

Prices/fees are explicit assumptions, not promised fills. Every output is
non-executable; changing exposure or collateral invalidates the frozen exchange
liquidation estimate. A separate account-aware risk model and fresh human
confirmation would be necessary before any future execution implementation.
"""
import hashlib
import json
import math
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext

from core.order_precision import normalize_perp_size


METHOD = "offline-fixed-capital-candidate-accounting-v1"


def _d(value, name, minimum=None):
    try:
        if isinstance(value, bool): raise ValueError()
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {name}") from exc
    if not result.is_finite() or (minimum is not None and result < Decimal(str(minimum))):
        raise ValueError(f"Invalid {name}")
    return result


def _float(value):
    result = float(value)
    if not math.isfinite(result): raise ValueError("Calculated value exceeds supported numeric range")
    return result


def _serialize(value):
    if isinstance(value, Decimal): return _float(value)
    if isinstance(value, dict): return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)): return [_serialize(v) for v in value]
    return value


def build_candidates(position, lifecycle, source, assumptions, *, sz_decimals,
                     average_fractions=(.25, .5, 1), lower_leverages=(1, 2, 5),
                     margin_fractions=(.25, .5, 1), max_extra_fraction=.5, max_injections=4):
    """Build explicitly modelled alternative states, never a sequence of trades.

    Required ``source``: verified=True, wallet, owner_count=1, slot_budget_usdc,
    free_budget_usdc, additional_spent_usdc, injections_count. Injections counts
    all positive capital additions, not just averaging, so margin adjustments
    cannot bypass the four-step limit. Each candidate uses the same frozen base;
    their individual budgets MUST NOT be added together or executed as a batch.

    Required ``lifecycle``: basis='original_pre_intervention', risk_capital_usdc,
    realized_pnl_usdc, paid_costs_usdc, source_wallet, evidence. The original ROE
    denominator remains fixed even after adding margin or reducing leverage.

    Required assumptions: entry_fee_bps, exit_fee_bps, slippage_bps, label.
    Averaging/margin fractions refer to the remaining allowed own-slot funds.
    Every assumed price improvement/worsening is explicitly marked as modelled.
    """
    with localcontext() as context:
        context.prec = 50
        return _build(position, lifecycle, source, assumptions, sz_decimals, average_fractions,
                      lower_leverages, margin_fractions, max_extra_fraction, max_injections)


def _build(position, lifecycle, source, assumptions, digits, average_fractions,
           lower_leverages, margin_fractions, max_extra_fraction, max_injections):
    # Validate lot precision through the project's existing exchange normalizer.
    normalize_perp_size(0, digits)
    if source.get("verified") is not True or source.get("owner_count") != 1 or not source.get("wallet"):
        raise ValueError("Verified single-source ownership is required")
    if (lifecycle.get("basis") != "original_pre_intervention" or not lifecycle.get("evidence")
            or str(lifecycle.get("source_wallet", "")).lower() != str(source["wallet"]).lower()):
        raise ValueError("Verified original source risk basis is required")
    if not assumptions.get("label"): raise ValueError("Explicit model cost label is required")
    risk = _d(lifecycle["risk_capital_usdc"], "risk capital")
    if risk <= 0: raise ValueError("Risk capital must be positive")
    realized = _d(lifecycle["realized_pnl_usdc"], "realized PnL")
    paid = _d(lifecycle["paid_costs_usdc"], "paid costs", 0)
    slot = _d(source["slot_budget_usdc"], "slot budget", 0)
    free = _d(source["free_budget_usdc"], "free budget", 0)
    spent = _d(source["additional_spent_usdc"], "spent budget", 0)
    if slot <= 0 or free > slot: raise ValueError("Own-slot free budget is inconsistent")
    cap_fraction = _d(max_extra_fraction, "additional capital fraction", 0)
    if cap_fraction > Decimal("0.5"): raise ValueError("Extra capital cap exceeds user maximum 50%")
    injections = _d(source["injections_count"], "injection count", 0)
    maximum = _d(max_injections, "maximum injections", 0)
    if injections != int(injections) or maximum != int(maximum) or maximum > 4:
        raise ValueError("Capital-injection limit must be an integer not exceeding four")
    cap_remaining = max(Decimal(0), slot*cap_fraction-spent)
    available = min(free, cap_remaining)
    fee_in = _d(assumptions["entry_fee_bps"], "entry fee", 0)/10000
    fee_out = _d(assumptions["exit_fee_bps"], "exit fee", 0)/10000
    slip = _d(assumptions["slippage_bps"], "slippage", 0)/10000
    if fee_in >= 1 or fee_out >= 1 or slip >= 1: raise ValueError("Costs must be less than 100%")
    side = position.get("side")
    if side not in ("LONG", "SHORT"): raise ValueError("Invalid position direction")
    direction = Decimal(1 if side == "LONG" else -1)
    p = {k: position.get(k) for k in ("coin", "dex", "side", "margin_mode")}
    if not p["coin"]: raise ValueError("Position market is required")
    p["margin_mode"] = str(p["margin_mode"] or "").lower()
    for name in ("size", "entry_price", "mark_price", "leverage", "margin_used"):
        p[name] = _d(position[name], name)
        if p[name] <= 0: raise ValueError(f"{name} must be positive")
    if p["leverage"] != int(p["leverage"]) or p["leverage"] < 1:
        raise ValueError("Leverage must be a positive integer")
    liq = position.get("liquidation_price")
    p["liquidation_price"] = _d(liq, "liquidation price", 0) if liq is not None else None
    if p["liquidation_price"] == 0: p["liquidation_price"] = None
    if p["liquidation_price"] is not None and (p["mark_price"]-p["liquidation_price"])*direction <= 0:
        raise ValueError("Position mark has crossed the supplied liquidation estimate")
    if Decimal(str(normalize_perp_size(p["size"], digits))) != p["size"]:
        raise ValueError("Existing position size is not aligned with market lots")

    outputs = []
    def candidate(action, variant, *, reason=None, post=None, post_realized=realized,
                  post_paid=paid, quantity=Decimal(0), fill=None, notional=Decimal(0),
                  margin_change=Decimal(0), fee=Decimal(0), slippage=Decimal(0), reserve=Decimal(0)):
        changed = action != "HOLD"
        final = dict(p if post is None else post)
        if changed: final["liquidation_price"] = None
        pnl = post_realized+direction*final["size"]*(p["mark_price"]-final["entry_price"])-post_paid
        row = {"method": METHOD, "action": action, "variant": str(variant),
               "available_for_research": reason is None, "reason": reason,
               "executable": False, "probability": None,
               "source_wallet": source["wallet"], "source_slot_budget_usdc": slot,
               "model_cost_label": assumptions["label"], "all_values_are_modelled": True,
               "quantity": quantity, "assumed_fill_price": fill, "order_notional_usdc": notional,
               "modelled_fee_usdc": fee, "modelled_slippage_usdc": slippage,
               "modelled_margin_change_usdc": margin_change,
               "reserved_source_budget_usdc": reserve, "remaining_extra_cap_usdc": cap_remaining-reserve,
               "remaining_source_free_usdc": free-reserve,
               "next_injections_count": int(injections)+(1 if reserve > 0 else 0),
               "post_source_ledger": {"additional_spent_usdc": spent+reserve,
                                      "free_budget_usdc": free-reserve,
                                      "injections_count": int(injections)+(1 if reserve > 0 else 0)},
               "post_position": final,
               "post_lifecycle": {"basis": lifecycle["basis"], "risk_capital_usdc": risk,
                                  "realized_pnl_usdc": post_realized, "paid_costs_usdc": post_paid,
                                  "source_wallet": lifecycle["source_wallet"], "evidence": lifecycle["evidence"]},
               "fixed_basis_mark_pnl_usdc": pnl, "fixed_basis_roe_pct": pnl/risk*100,
               "new_liquidation_verified": not changed and final["liquidation_price"] is not None,
               "immediate_costs_already_in_post_snapshot": True,
               "permission_blockers": ["offline_only", "calibrated_probability_unavailable"]+
                    (["post_action_liquidation_unknown"] if changed else []),
               "warning": "Alternative model, not executable order. No guaranteed recovery; costs and liquidation require verification."}
        row = _serialize(row)
        row["id"] = hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False).encode()).hexdigest()[:24]
        outputs.append(row)

    def cash_reason(reserve):
        if reserve > 0 and injections >= maximum: return "capital_injection_limit_reached"
        if reserve <= 0: return "no_extra_source_budget"
        if reserve > available: return "insufficient_own_source_allowance"
        return None

    candidate("HOLD", "unchanged")
    quantity = Decimal(str(normalize_perp_size(p["size"]*Decimal(".25"), digits)))
    fill = p["mark_price"]*(1-direction*slip)
    notional = quantity*fill
    if quantity <= 0 or quantity >= p["size"] or notional < 10:
        candidate("REDUCE", "25pct", reason="below_minimum_or_lot_size")
    else:
        fee = notional*fee_out
        post = dict(p, size=p["size"]-quantity,
                    margin_used=p["margin_used"]*(1-quantity/p["size"]))
        candidate("REDUCE", "25pct", post=post, post_realized=realized+direction*quantity*(fill-p["entry_price"]),
                  post_paid=paid+fee, quantity=quantity, fill=fill, notional=notional,
                  margin_change=post["margin_used"]-p["margin_used"], fee=fee,
                  slippage=quantity*abs(fill-p["mark_price"]))

    for raw_fraction in average_fractions:
        fraction = _d(raw_fraction, "average fraction", 0)
        if not 0 < fraction <= 1: raise ValueError("Average fraction must be in (0,1]")
        requested = available*fraction
        reason = cash_reason(requested)
        if reason:
            candidate("AVERAGE", str(fraction), reason=reason)
            continue
        fill = p["mark_price"]*(1+direction*slip)
        # Reserve initial margin AND fee AND a slippage cushion; price slippage
        # is embedded in weighted entry, never also debited from realised PnL.
        unit_slip = abs(fill-p["mark_price"])
        unit_cost = fill/p["leverage"]+fill*fee_in+unit_slip
        quantity = Decimal(str(normalize_perp_size(requested/unit_cost, digits)))
        notional, fee, slippage = quantity*fill, quantity*fill*fee_in, quantity*unit_slip
        margin = notional/p["leverage"]
        reserve = margin+fee+slippage
        reason = cash_reason(reserve)
        if quantity <= 0 or notional < 10: reason = "below_minimum_or_lot_size"
        if reason:
            candidate("AVERAGE", str(fraction), reason=reason)
            continue
        new_size = p["size"]+quantity
        post = dict(p, size=new_size, entry_price=(p["size"]*p["entry_price"]+quantity*fill)/new_size,
                    margin_used=p["margin_used"]+margin)
        candidate("AVERAGE", str(fraction), post=post, post_paid=paid+fee, quantity=quantity,
                  fill=fill, notional=notional, margin_change=margin, fee=fee, slippage=slippage, reserve=reserve)

    for raw_target in lower_leverages:
        target = _d(raw_target, "lower leverage", 1)
        if target != int(target): raise ValueError("Leverage target must be an integer")
        if target >= p["leverage"]: continue
        if p["margin_mode"] != "isolated":
            candidate("LOWER_LEVERAGE", str(target), reason="isolated_margin_required_for_source_accounting")
            continue
        required = p["size"]*p["mark_price"]/target
        margin = max(Decimal(0), required-p["margin_used"])
        # Required collateral rounds UP to a USDC micro-unit; then the usual
        # budget check rejects any amount the source cannot actually reserve.
        margin = (margin/Decimal(".000001")).to_integral_value(rounding=ROUND_CEILING)*Decimal(".000001")
        reason = cash_reason(margin) if margin else None
        if reason:
            candidate("LOWER_LEVERAGE", str(target), reason=reason)
            continue
        post = dict(p, leverage=target, margin_used=p["margin_used"]+margin)
        candidate("LOWER_LEVERAGE", str(target), post=post, margin_change=margin, reserve=margin)

    for raw_fraction in margin_fractions:
        fraction = _d(raw_fraction, "margin fraction", 0)
        if not 0 < fraction <= 1: raise ValueError("Margin fraction must be in (0,1]")
        if p["margin_mode"] != "isolated":
            candidate("ADD_MARGIN", str(fraction), reason="isolated_margin_required_for_source_accounting")
            continue
        # USDC model values use six decimals; this is not an SDK transfer call.
        margin = (available*fraction//Decimal(".000001"))*Decimal(".000001")
        reason = cash_reason(margin)
        if reason:
            candidate("ADD_MARGIN", str(fraction), reason=reason)
            continue
        candidate("ADD_MARGIN", str(fraction), post=dict(p, margin_used=p["margin_used"]+margin),
                  margin_change=margin, reserve=margin)
    return list({row["id"]: row for row in outputs}.values())
