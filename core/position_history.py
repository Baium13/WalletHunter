"""Read-only, fail-closed reconstruction of a perpetual position since flat.

This module does not infer who opened a position, historical leverage, or a
profit probability. It uses raw (not aggregated or pre-filtered) exchange fills
and funding plus an explicit completeness attestation for their bounded query.
Only an unchanged, single-fill position may receive a separately frozen,
explicitly labelled pre-intervention margin denominator. That is an observed
pre-intervention margin, NOT reconstructed margin at the original entry.

Official schemas/limits:
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
The fill ``fee`` includes builderFee; negative fee means a maker rebate.
Funding delta.usdc is signed account cashflow, unlike cumFunding.sinceOpen.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json


METHOD = "raw-fills-funding-since-flat-v1"
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


class HistoryUnavailable(ValueError):
    """A stable diagnostic code, not permission to guess missing history."""


def _number(value, field, positive=False):
    if value is None or isinstance(value, bool): raise HistoryUnavailable(f"missing_or_invalid_{field}")
    try: result = Decimal(str(value))
    except (InvalidOperation, ValueError): raise HistoryUnavailable(f"missing_or_invalid_{field}") from None
    if not result.is_finite() or (positive and result <= 0):
        raise HistoryUnavailable(f"missing_or_invalid_{field}")
    return result


def _stamp(value, field):
    result = _number(value, field)
    if result < 0 or result != int(result): raise HistoryUnavailable(f"invalid_{field}")
    return int(result)


def _market(row):
    if not isinstance(row, dict): raise HistoryUnavailable("invalid_market_record")
    coin, dex = row.get("coin"), row.get("dex") or ""
    if not isinstance(coin, str) or not coin or coin != coin.strip() or not isinstance(dex, str):
        raise HistoryUnavailable("invalid_market")
    if ":" in coin:
        prefix, symbol = coin.split(":", 1)
        if not prefix or not symbol or (dex and dex != prefix): raise HistoryUnavailable("conflicting_market_dex")
        dex = prefix
    elif dex:
        coin = f"{dex}:{coin}"
    return f"{coin}|{dex}"


def _serial(value):
    try: return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError): raise HistoryUnavailable("non_json_history_record") from None


def _metadata(fills, funding, completeness):
    """Require raw one-page responses below both row and distinct-block caps.

    Required schema: start_ms/end_ms/position_asof_ms plus fills/funding dicts
    with complete=True, truncated=False, aggregate_by_time=False (fills only),
    row_count, row_cap and block_cap. This conservative contract intentionally
    does not accept concatenated pages or guesses about saturated responses.
    """
    if not isinstance(completeness, dict): raise HistoryUnavailable("completeness_metadata_required")
    start, end = (_stamp(completeness.get(key), key) for key in ("start_ms", "end_ms"))
    if start >= end: raise HistoryUnavailable("invalid_query_interval")
    if _stamp(completeness.get("position_asof_ms"), "position_asof_ms") != end:
        raise HistoryUnavailable("position_history_time_mismatch")
    result = {"start_ms": start, "end_ms": end, "position_asof_ms": end, "scope": "current_episode_within_query_interval"}
    for name, rows, maximum in (("fills", fills, 2000), ("funding", funding, 500)):
        meta = completeness.get(name)
        if not isinstance(rows, list) or not isinstance(meta, dict): raise HistoryUnavailable(f"invalid_{name}_response")
        if meta.get("complete") is not True or meta.get("truncated") is not False:
            raise HistoryUnavailable(f"{name}_completeness_unverified")
        if name == "fills" and meta.get("aggregate_by_time") is not False:
            raise HistoryUnavailable("raw_unaggregated_fills_required")
        count = _stamp(meta.get("row_count"), f"{name}_row_count")
        cap = _stamp(meta.get("row_cap"), f"{name}_row_cap")
        block_cap = _stamp(meta.get("block_cap"), f"{name}_block_cap")
        if count != len(rows) or not 1 <= cap <= maximum or not 1 <= block_cap <= 500:
            raise HistoryUnavailable(f"invalid_{name}_cap_evidence")
        if len(rows) >= cap: raise HistoryUnavailable(f"{name}_response_at_cap")
        times, seen = set(), set()
        for row in rows:
            if not isinstance(row, dict): raise HistoryUnavailable(f"invalid_{name}_record")
            stamp = _stamp(row.get("time"), f"{name}_time")
            if not start <= stamp <= end: raise HistoryUnavailable(f"{name}_outside_query_interval")
            encoded = _serial(row)
            if encoded in seen: raise HistoryUnavailable(f"duplicate_{name}_record")
            seen.add(encoded); times.add(stamp)
        if len(times) >= block_cap: raise HistoryUnavailable(f"{name}_response_at_block_cap")
        result[name] = {"complete": True, "row_count": len(rows), "row_cap": cap,
                        "distinct_timestamps": len(times), "block_cap": block_cap}
    return result


def _current(position):
    market = _market(position)
    side = position.get("side")
    raw_signed = position.get("signed_size", position.get("szi"))
    if raw_signed is not None:
        signed = _number(raw_signed, "current_signed_size")
        if signed == 0: raise HistoryUnavailable("current_position_is_flat")
        expected_side = "LONG" if signed > 0 else "SHORT"
        if side is not None and side != expected_side: raise HistoryUnavailable("current_side_size_mismatch")
        if "size" in position and _number(position["size"], "current_size", True) != abs(signed):
            raise HistoryUnavailable("current_side_size_mismatch")
    else:
        if side not in ("LONG", "SHORT"): raise HistoryUnavailable("invalid_current_side")
        signed = _number(position.get("size"), "current_size", True) * (1 if side == "LONG" else -1)
    entry = _number(position.get("entry_price", position.get("entryPx")), "current_entry", True)
    return market, signed, entry


def _ordered_episode(rows):
    openings = [row for row in rows if _number(row.get("startPosition"), "fill_start_position") == 0]
    if not openings: raise HistoryUnavailable("opening_from_flat_not_in_query")
    opening_time = max(_stamp(row.get("time"), "fill_time") for row in openings)
    if sum(row["time"] == opening_time for row in openings) != 1:
        raise HistoryUnavailable("ambiguous_opening_timestamp")
    remaining = [row for row in rows if row["time"] >= opening_time]
    expected, ordered = Decimal(0), []
    for stamp in sorted({row["time"] for row in remaining}):
        group = [row for row in remaining if row["time"] == stamp]
        while group:
            matching = [row for row in group if _number(row.get("startPosition"), "fill_start_position") == expected]
            if len(matching) != 1: raise HistoryUnavailable("fill_continuity_or_order_ambiguous")
            row = matching[0]
            if row.get("side") not in ("A", "B"): raise HistoryUnavailable("invalid_fill_side")
            size = _number(row.get("sz"), "fill_size", True)
            expected += size * (1 if row["side"] == "B" else -1)
            ordered.append(row); group.remove(row)
    return ordered, expected


def reconstruct_position_episode(position, fills, funding, *, completeness,
                                 frozen_pre_intervention_margin=None, intervention_history=None):
    """Return verified accounting or raise HistoryUnavailable, never guessed zero.

    A reversal without going flat remains part of the same continuous episode:
    its realised loss and costs are retained. Such an episode cannot receive
    original_pre_intervention risk basis from this helper.

    Optional margin schema: {basis:'pre_intervention_exchange_margin',
    margin_usdc:positive, asof_ms:timestamp, evidence:nonempty string}.
    ``intervention_history=[]`` must explicitly attest no interventions; None
    means unknown. It does not attest a learned model or wallet attribution.
    """
    proof = _metadata(fills, funding, completeness)
    market, current_signed, current_entry = _current(position)
    market_fills = [row for row in fills if _market(row) == market]
    episode, remaining = _ordered_episode(market_fills)
    if remaining == 0 or remaining != current_signed: raise HistoryUnavailable("current_size_does_not_match_history")
    opened = episode[0]["time"]
    entry = Decimal(0)
    closed_pnl = fee_debits = fee_rebates = Decimal(0)
    reversals = 0
    evidence_fills = []
    for row in episode:
        before, size = _number(row["startPosition"], "fill_start_position"), _number(row["sz"], "fill_size", True)
        delta = size * (1 if row["side"] == "B" else -1)
        after, price = before + delta, _number(row.get("px"), "fill_price", True)
        if before == 0:
            entry = price
            expected_dir = "Open Long" if delta > 0 else "Open Short"
        elif before * delta > 0:
            entry = (abs(before) * entry + size * price) / abs(after)
            expected_dir = "Open Long" if before > 0 else "Open Short"
        elif before * after < 0:
            entry = price; reversals += 1
            expected_dir = "Long > Short" if before > 0 else "Short > Long"
        else:
            expected_dir = "Close Long" if before > 0 else "Close Short"
        if "dir" in row and row["dir"] != expected_dir: raise HistoryUnavailable("fill_direction_mismatch")
        pnl = _number(row.get("closedPnl"), "fill_closed_pnl")
        # Adding/opening cannot have realised PnL under this supported model.
        if (before == 0 or before * delta > 0) and pnl != 0:
            raise HistoryUnavailable("unexpected_open_fill_realized_pnl")
        closed_pnl += pnl
        if row.get("feeToken") != "USDC": raise HistoryUnavailable("non_usdc_or_unknown_fee_token")
        fee = _number(row.get("fee"), "fill_fee")
        if fee >= 0: fee_debits += fee
        else: fee_rebates -= fee
        # builderFee is already part of fee; adding it again double counts.
        if row.get("builderFee") is not None: _number(row["builderFee"], "builder_fee")
        evidence_fills.append({key: row.get(key) for key in ("time", "oid", "tid", "side", "sz", "px", "startPosition")})
    tolerance = max(Decimal("0.00000001"), abs(current_entry) * Decimal("0.00000001"))
    if abs(entry - current_entry) > tolerance: raise HistoryUnavailable("current_entry_does_not_match_history")

    settlements = []
    for row in funding:
        delta = row.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "funding":
            raise HistoryUnavailable("invalid_funding_delta")
        if _market(delta) == market and row["time"] >= opened: settlements.append(row)
    expected_hours = set(range(opened // HOUR_MS + 1, proof["end_ms"] // HOUR_MS + 1))
    observed_hours = [row["time"] // HOUR_MS for row in settlements]
    if len(observed_hours) != len(set(observed_hours)): raise HistoryUnavailable("duplicate_funding_hour")
    if set(observed_hours) != expected_hours: raise HistoryUnavailable("funding_hour_coverage_incomplete")
    funding_debits = funding_credits = Decimal(0)
    for row in settlements:
        delta = row["delta"]
        cashflow = _number(delta.get("usdc"), "funding_usdc")
        funded_size = _number(delta.get("szi"), "funding_signed_size")
        before_funding = [fill for fill in episode if fill["time"] < row["time"]]
        if not before_funding: raise HistoryUnavailable("funding_before_opening")
        last = before_funding[-1]
        expected_size = _number(last["startPosition"], "fill_start_position") + _number(last["sz"], "fill_size") * (1 if last["side"] == "B" else -1)
        if funded_size != expected_size: raise HistoryUnavailable("funding_position_size_mismatch")
        if cashflow >= 0: funding_credits += cashflow
        else: funding_debits -= cashflow

    proof["funding"].update(expected_episode_hours=len(expected_hours), observed_episode_hours=len(settlements), missing_hours=[])
    evidence = {"method": METHOD, "market": market, "opening_start_position": "0",
                "quantity_matches_current": True, "entry_matches_current": True,
                "entry_tolerance": str(tolerance), "fills": evidence_fills,
                "funding_sign": "delta.usdc is additive cashflow; do not use cumFunding sign",
                "fees": "USDC fill fee includes builderFee; negative fee is a rebate"}
    result = {
        "status": "VERIFIED", "method": METHOD, "market": market, "asof_ms": proof["end_ms"],
        "episode": {"opening_ms": opened, "last_fill_ms": episode[-1]["time"], "fill_count": len(episode),
                    "remaining_signed_size": str(remaining), "entry_price": str(entry), "reversal_count": reversals,
                    "unchanged_single_opening": len(episode) == 1, "historical_leverage": None},
        "accounting": {"realized_closed_pnl_usdc": str(closed_pnl), "fee_net_usdc": str(fee_debits-fee_rebates),
                       "fee_debits_usdc": str(fee_debits), "fee_rebates_usdc": str(fee_rebates),
                       "funding_cashflow_usdc": str(funding_credits-funding_debits),
                       "funding_credits_usdc": str(funding_credits), "funding_debits_usdc": str(funding_debits),
                       "historical_net_usdc": str(closed_pnl-fee_debits+fee_rebates+funding_credits-funding_debits)},
        "completeness": proof, "evidence": evidence, "risk_basis": None,
        "risk_basis_unavailable_reason": "explicit_frozen_pre_intervention_margin_required"
    }
    if len(episode) != 1:
        result["risk_basis_unavailable_reason"] = "position_changed_since_flat_opening"
    elif intervention_history is None:
        result["risk_basis_unavailable_reason"] = "intervention_history_unknown"
    elif not isinstance(intervention_history, list) or intervention_history:
        result["risk_basis_unavailable_reason"] = "interventions_present_or_history_invalid"
    elif frozen_pre_intervention_margin is not None:
        margin = frozen_pre_intervention_margin
        if not isinstance(margin, dict) or margin.get("basis") != "pre_intervention_exchange_margin" or not isinstance(margin.get("evidence"), str) or not margin["evidence"].strip():
            raise HistoryUnavailable("unlabelled_frozen_margin")
        frozen_at = _stamp(margin.get("asof_ms"), "frozen_margin_time")
        if not opened <= frozen_at <= proof["end_ms"]: raise HistoryUnavailable("frozen_margin_outside_episode")
        capital = _number(margin.get("margin_usdc"), "frozen_margin_usdc", True)
        result["risk_basis"] = {
            "basis": "original_pre_intervention", "risk_capital_usdc": str(capital),
            "realized_pnl_usdc": str(closed_pnl+fee_rebates+funding_credits),
            "paid_costs_usdc": str(fee_debits+funding_debits),
            "frozen_at_ms": frozen_at, "capital_origin": "explicit_observed_pre_intervention_margin_not_historical_entry_margin",
            "evidence": _serial({"history": evidence, "margin": deepcopy(margin), "no_interventions": True})}
        result["risk_basis_unavailable_reason"] = None
    return result


def read_position_episode(reader, user, position, *, now_ms, position_asof_ms,
                          lookback_days=7, frozen_pre_intervention_margin=None, intervention_history=None):
    """Two read-only info requests; saturated responses remain unavailable.

    Caller must supply a position snapshot corresponding to now_ms (and hold
    its account's read/decision lock when using the result for research). This
    helper never obtains or uses signing credentials. It deliberately performs
    no pagination when completeness is uncertain and never creates orders.
    """
    now = _stamp(now_ms, "now_ms")
    asof = _stamp(position_asof_ms, "position_asof_ms")
    if asof != now: raise HistoryUnavailable("position_history_time_mismatch")
    days = _stamp(lookback_days, "lookback_days")
    if not 1 <= days <= 7: raise HistoryUnavailable("lookback_days_must_be_1_to_7")
    start = max(0, now-days*DAY_MS)
    if not isinstance(user, str) or not user: raise HistoryUnavailable("user_address_required")
    fills = reader._info({"type": "userFillsByTime", "user": user, "startTime": start, "endTime": now, "aggregateByTime": False})
    funding = reader._info({"type": "userFunding", "user": user, "startTime": start, "endTime": now})
    if not isinstance(fills, list) or not isinstance(funding, list): raise HistoryUnavailable("invalid_history_response")
    completeness = {"start_ms": start, "end_ms": now, "position_asof_ms": asof}
    for name, rows, cap in (("fills", fills, 2000), ("funding", funding, 500)):
        completeness[name] = {"complete": True, "truncated": False, "row_count": len(rows),
                              "row_cap": cap, "block_cap": 500, "aggregate_by_time": False}
    return reconstruct_position_episode(position, fills, funding, completeness=completeness,
        frozen_pre_intervention_margin=frozen_pre_intervention_margin, intervention_history=intervention_history)
