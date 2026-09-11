"""Mode-aware USDC sizing capital, deliberately not total portfolio equity.

Unified USDC is held once in spot state; per-DEX margin summaries must not
be added to that balance. Funds reserved as holds remain part of the sizing
base but are not available for new orders. Standard mode has separate pools.
See Hyperliquid's official trading/account-abstraction-modes documentation.
"""
from dataclasses import dataclass
import math
import os
import threading
import time


class UnsupportedCapitalMode(ValueError):
    def __init__(self, mode):
        self.mode = mode
        super().__init__(f"Unsupported Hyperliquid capital mode: {mode!r}")


def finite_amount(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"Invalid {label}")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"Invalid {label}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Nonfinite {label}")
    return result


@dataclass(frozen=True)
class SpotUSDC:
    total: float
    hold: float
    available_after_maintenance: float | None

    @property
    def available(self):
        free = max(0.0, self.total - self.hold)
        if self.available_after_maintenance is not None:
            free = min(free, max(0.0, self.available_after_maintenance))
        return free


@dataclass(frozen=True)
class CapitalSnapshot:
    mode: str
    sizing_base_usdc: float
    basis: str


def strict_spot_usdc(state):
    """Read USDC token 0 without silently dropping invalid/duplicate balances."""
    if not isinstance(state, dict) or not isinstance(state.get("balances"), list):
        raise ValueError("Invalid spot balance snapshot")
    usdc = None
    for row in state["balances"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid spot balance row")
        coin, token = row.get("coin"), row.get("token")
        if not isinstance(coin, str) or not coin or isinstance(token, bool) or not isinstance(token, int) or token < 0:
            raise ValueError("Invalid spot token identity")
        total = finite_amount(row.get("total"), "spot total")
        hold = finite_amount(row.get("hold"), "spot hold")
        if hold < 0:
            raise ValueError("Negative spot hold")
        if coin.upper() == "USDC" or token == 0:
            if coin.upper() != "USDC" or token != 0 or usdc is not None:
                raise ValueError("Ambiguous or duplicate USDC balance")
            usdc = (total, hold)

    maintenance = None
    if "tokenToAvailableAfterMaintenance" in state:
        rows = state["tokenToAvailableAfterMaintenance"]
        if not isinstance(rows, list):
            raise ValueError("Invalid maintenance availability snapshot")
        seen = set()
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise ValueError("Invalid maintenance availability row")
            token, value = row
            if isinstance(token, bool) or not isinstance(token, int) or token < 0 or token in seen:
                raise ValueError("Ambiguous maintenance token")
            seen.add(token)
            amount = finite_amount(value, "maintenance availability")
            if token == 0:
                maintenance = amount
        if usdc is not None and 0 not in seen:
            raise ValueError("Missing USDC maintenance availability")
    total, hold = usdc if usdc is not None else (0.0, 0.0)
    return SpotUSDC(total, hold, maintenance)


# ``userAbstraction`` answers which collateral model the account owner selected
# in the exchange UI.  It is an account setting, not financial state: it cannot
# change as a result of trading, only by a deliberate action of the owner.  The
# endpoint is nevertheless charged 20 weighted units, ten times a clearinghouse
# read, and the account watcher asked for it on every cycle - about 210 weighted
# units a minute for a constant.  Cache the answer for a bounded interval, per
# venue and account, so the balances themselves stay read fresh on every call.
# Set HL_ACCOUNT_MODE_TTL=0 to disable the cache and restore per-call reads.
try:
    _MODE_TTL_SECONDS = min(300.0, max(0.0, float(os.getenv("HL_ACCOUNT_MODE_TTL", "30") or 30)))
except (TypeError, ValueError):
    _MODE_TTL_SECONDS = 30.0
_MODE_CACHE_LIMIT = 512
_mode_cache = {}
_mode_lock = threading.Lock()


def forget_account_mode(venue=None, user=None):
    """Drop cached modes; a caller that suspects a switch re-reads immediately."""
    with _mode_lock:
        if venue is None and user is None:
            _mode_cache.clear()
            return
        for key in [k for k in _mode_cache
                    if (venue is None or k[0] == venue) and (user is None or k[1] == user)]:
            _mode_cache.pop(key, None)


def account_mode(post, user, venue=None):
    """Validated account abstraction mode, cached per venue for a bounded TTL.

    ``venue`` namespaces the cache (API base URL or network).  Callers that do
    not supply one keep the previous behaviour of reading the mode every time,
    so no existing caller is silently given cached data.  A rejected mode is
    never cached: an unsupported account is re-read on the next attempt.
    """
    key = (venue, user)
    cacheable = venue is not None and _MODE_TTL_SECONDS > 0
    if cacheable:
        with _mode_lock:
            entry = _mode_cache.get(key)
        if entry is not None and 0.0 <= time.monotonic() - entry[0] < _MODE_TTL_SECONDS:
            return entry[1]
    mode = post({"type": "userAbstraction", "user": user})
    if mode not in ("unifiedAccount", "disabled"):
        raise UnsupportedCapitalMode(mode)
    if cacheable:
        with _mode_lock:
            _mode_cache[key] = (time.monotonic(), mode)
            if len(_mode_cache) > _MODE_CACHE_LIMIT:
                stale = sorted(_mode_cache, key=lambda k: _mode_cache[k][0])[:_MODE_CACHE_LIMIT // 2]
                for k in stale:
                    _mode_cache.pop(k, None)
    return mode


def _mode(post, user, venue=None):
    return account_mode(post, user, venue)


def _state(post, user, dex):
    state = post({"type": "clearinghouseState", "user": user, "dex": dex})
    if not isinstance(state, dict):
        raise ValueError("Invalid perp balance snapshot")
    return state


def read_capital_snapshot(post, user, venue=None):
    """Fresh sizing base: unified USDC total OR supported standard perp equity."""
    mode = _mode(post, user, venue)
    if mode == "unifiedAccount":
        spot = strict_spot_usdc(post({"type": "spotClearinghouseState", "user": user}))
        return CapitalSnapshot(mode, spot.total, "unified_usdc_total")
    values = []
    for dex in ("", "xyz"):
        summary = _state(post, user, dex).get("marginSummary")
        if not isinstance(summary, dict):
            raise ValueError("Missing perp margin summary")
        values.append(finite_amount(summary.get("accountValue"), "perp account value"))
    total = finite_amount(math.fsum(values), "supported perp account value")
    return CapitalSnapshot(mode, total, "supported_perp_equity")


def available_margin_usdc(post, user, dex="", venue=None):
    """Fresh available capacity; not an equity/portfolio balance calculation."""
    if dex not in ("", "xyz"):
        raise ValueError("Unsupported collateral DEX")
    mode = _mode(post, user, venue)
    if mode == "unifiedAccount":
        return strict_spot_usdc(post({"type": "spotClearinghouseState", "user": user})).available
    return max(0.0, finite_amount(_state(post, user, dex).get("withdrawable"), "perp withdrawable"))
