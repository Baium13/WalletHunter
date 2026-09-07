"""Mode-aware USDC sizing capital, deliberately not total portfolio equity.

Unified USDC is held once in spot state; per-DEX margin summaries must not
be added to that balance. Funds reserved as holds remain part of the sizing
base but are not available for new orders. Standard mode has separate pools.
See Hyperliquid's official trading/account-abstraction-modes documentation.
"""
from dataclasses import dataclass
import math


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


def _mode(post, user):
    mode = post({"type": "userAbstraction", "user": user})
    if mode not in ("unifiedAccount", "disabled"):
        raise UnsupportedCapitalMode(mode)
    return mode


def _state(post, user, dex):
    state = post({"type": "clearinghouseState", "user": user, "dex": dex})
    if not isinstance(state, dict):
        raise ValueError("Invalid perp balance snapshot")
    return state


def read_capital_snapshot(post, user):
    """Fresh sizing base: unified USDC total OR supported standard perp equity."""
    mode = _mode(post, user)
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


def available_margin_usdc(post, user, dex=""):
    """Fresh available capacity; not an equity/portfolio balance calculation."""
    if dex not in ("", "xyz"):
        raise ValueError("Unsupported collateral DEX")
    mode = _mode(post, user)
    if mode == "unifiedAccount":
        return strict_spot_usdc(post({"type": "spotClearinghouseState", "user": user})).available
    return max(0.0, finite_amount(_state(post, user, dex).get("withdrawable"), "perp withdrawable"))
