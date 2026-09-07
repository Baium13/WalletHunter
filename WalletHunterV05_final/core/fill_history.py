"""Bounded, fail-closed Hyperliquid fill-history retrieval.

userFillsByTime returns ALL markets (there is no dex request field), at most
2000 fills per response, and retains only the latest 10000 fills. Time endpoints
are inclusive. Official contract:
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint

A successful request means no truncation was detected for the requested
trailing window under that endpoint contract. It is not a guarantee of 90 days
of history or an atomic blockchain snapshot. Historical-ended windows cannot
establish retention completeness: fetch to now, then filter locally instead.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import math
import time


RESPONSE_CAP = 2000
RETENTION_CAP = 10000
MAX_REQUESTS = 32


class HistoryIncomplete(RuntimeError):
    """Do not publish a full-window PnL/rating from the partial response."""


def _stamp(value, name):
    if isinstance(value, bool):
        raise ValueError(f"Invalid {name}")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {name}") from exc
    if result < 0 or str(result) != str(value):
        raise ValueError(f"Invalid integer {name}")
    return result


def _numeric(value, name):
    if isinstance(value, bool):
        raise HistoryIncomplete(f"Invalid fill {name}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HistoryIncomplete(f"Invalid fill {name}") from exc
    if not result.is_finite() or not math.isfinite(float(result)):
        raise HistoryIncomplete(f"Non-finite fill {name}")
    return result


def _key(fill, lo, hi):
    if not isinstance(fill, dict):
        raise HistoryIncomplete("Fill response contains a non-object")
    try:
        stamp = _stamp(fill["time"], "fill time")
    except (ValueError, KeyError) as exc:
        raise HistoryIncomplete("Missing or invalid fill timestamp") from exc
    if not lo <= stamp <= hi:
        raise HistoryIncomplete("Exchange returned fill outside requested inclusive window")
    coin = fill.get("coin")
    if not isinstance(coin, str) or not coin.strip() or fill.get("side") not in {"A", "B"}:
        raise HistoryIncomplete("Missing fill market or side")
    numbers = {}
    for name in ("px", "sz", "closedPnl", "fee"):
        if name not in fill:
            raise HistoryIncomplete(f"Missing fill {name}")
        numbers[name] = _numeric(fill[name], name)
    if numbers["px"] <= 0 or numbers["sz"] <= 0:
        raise HistoryIncomplete("Fill price and size must be positive")
    for name in ("startPosition", "builderFee"):
        if name in fill:
            numbers[name] = _numeric(fill[name], name)
    identifiers = {}
    for name in ("tid", "oid", "id"):
        if fill.get(name) is not None:
            try:
                identifiers[name] = str(_stamp(fill[name], name))
            except ValueError as exc:
                raise HistoryIncomplete(f"Invalid fill {name}") from exc
    fill_hash = fill.get("hash")
    if fill_hash is not None and not isinstance(fill_hash, str):
        raise HistoryIncomplete("Invalid fill transaction hash")
    if fill_hash:
        identifiers["hash"] = fill_hash.lower()
    # An order ID alone cannot distinguish identical partial fills. Nonzero
    # transaction hashes plus execution fields are accepted as an older-schema
    # fallback; zero-hash TWAP fills require a trade/fill ID.
    hash_nonzero = bool(fill_hash and fill_hash.lower().removeprefix("0x").strip("0"))
    if "tid" not in identifiers and "id" not in identifiers and not hash_nonzero:
        raise HistoryIncomplete("Missing stable fill identity")
    canonical = {"coin": coin, "time": stamp, "side": fill["side"],
                 "ids": identifiers, "numbers": {k: str(v.normalize()) for k, v in numbers.items()},
                 "dir": fill.get("dir"), "crossed": fill.get("crossed"), "feeToken": fill.get("feeToken")}
    try:
        return json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HistoryIncomplete("Invalid fill identity fields") from exc


def fetch_fills(info, user, start_ms, end_ms, *, now_ms=None, max_requests=MAX_REQUESTS):
    """Return validated, de-duplicated fills using inclusive binary time splits.

``info`` is a callable accepting the info-endpoint payload (e.g. reader._info).
Request aggregation is disabled so distinct partial fills retain stable IDs.
The caller must handle HistoryIncomplete as unavailable analytics, never as an
empty wallet or zero profit. No partial list is returned on any detected gap.
    """
    start, end = _stamp(start_ms, "start"), _stamp(end_ms, "end")
    now = _stamp(int(time.time()*1000) if now_ms is None else now_ms, "now")
    budget = _stamp(max_requests, "request budget")
    if start > end or not 1 <= budget <= MAX_REQUESTS:
        raise ValueError("Invalid history window or request budget")
    if not callable(info) or not isinstance(user, str) or not user.strip():
        raise ValueError("A public info callable and user address are required")
    if end > now or now-end > 60_000:
        raise HistoryIncomplete("Retention cannot be established for a non-current end; fetch through now and filter locally")
    work, found, identities, requests = [(start, end)], {}, {}, 0
    while work:
        if requests >= budget:
            raise HistoryIncomplete("Fill-history request budget exhausted; window is incomplete")
        lo, hi = work.pop()
        requests += 1
        try:
            rows = info({"type": "userFillsByTime", "user": user,
                         "startTime": lo, "endTime": hi, "aggregateByTime": False})
        except Exception as exc:
            raise HistoryIncomplete("Fill-history endpoint unavailable; window is incomplete") from exc
        if not isinstance(rows, list) or len(rows) > RESPONSE_CAP:
            raise HistoryIncomplete("Invalid or oversized fill-history response")
        validated = [(_key(row, lo, hi), row) for row in rows]
        if len(rows) >= RESPONSE_CAP:
            if lo == hi:
                raise HistoryIncomplete("At least 2000 fills share one millisecond; pagination cannot prove completeness")
            middle = (lo+hi)//2
            # Endpoints are inclusive. Non-overlapping halves still retain all
            # fills at the boundary; never advance by an observed timestamp +1.
            work.extend([(middle+1, hi), (lo, middle)])
            continue
        for key, row in validated:
            identity_field = "tid" if row.get("tid") is not None else "id" if row.get("id") is not None else None
            if identity_field:
                stable = (identity_field, str(row[identity_field]), row["coin"], row["side"])
                previous = identities.get(stable)
                if previous is not None and previous != key:
                    raise HistoryIncomplete("Conflicting payloads for the same stable fill ID")
                identities[stable] = key
            found.setdefault(key, deepcopy(row))
        if len(found) >= RETENTION_CAP:
            raise HistoryIncomplete("10000-fill retention boundary reached; requested history may be truncated")
    return sorted(found.values(), key=lambda row: (_stamp(row["time"], "fill time"), _key(row, start, end)))


def filter_perp_fills(fills, dex=None):
    """Filter only locally: None=all perps, ''=core perps, 'xyz'=XYZ perps.

Spot symbols are @pairIndex or the legacy PURR/USDC-style pair. No second
userFillsByTime request with an ignored dex field is ever necessary.
    """
    if dex is not None and not isinstance(dex, str):
        raise ValueError("dex must be a string or None")
    selected = None if dex is None else dex.strip().lower()
    result = []
    for fill in fills:
        coin = str(fill.get("coin") or "")
        if not coin or coin.startswith("@") or "/" in coin:
            continue
        market_dex = coin.split(":", 1)[0].lower() if ":" in coin else ""
        if selected is None or selected == market_dex:
            result.append(deepcopy(fill))
    return result
