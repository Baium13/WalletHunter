"""Hyperliquid perpetual tick/lot normalization without binary rounding drift.

Official rules: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size
Perp prices: five significant figures, at most 6-szDecimals decimal places;
integer prices are exempt from the significant-figure restriction.
"""
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP, localcontext
import math


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid numeric value") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("Value must be finite and nonnegative")
    return number


def _decimals(value):
    number = int(value)
    if number != value or not 0 <= number <= 6:
        raise ValueError("Invalid perpetual szDecimals")
    return number


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Value exceeds supported numeric range")
    return result


def normalize_perp_size(size, sz_decimals):
    """Truncate lots: rounding must never spend more than requested budget."""
    value, digits = _decimal(size), _decimals(sz_decimals)
    with localcontext() as context:
        context.prec = max(32, len(value.as_tuple().digits) + abs(value.adjusted()) + digits + 2)
        return _float(value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_DOWN))


def normalize_perp_price(price, sz_decimals, *, rounding=ROUND_HALF_UP):
    value, digits = _decimal(price), _decimals(sz_decimals)
    if value <= 0:
        raise ValueError("Price must be positive")
    # Integers remain exact even when they have more than five significant digits.
    places = 0 if value == value.to_integral_value() else max(0, min(6 - digits, 4 - value.adjusted()))
    with localcontext() as context:
        context.prec = max(32, len(value.as_tuple().digits) + abs(value.adjusted()) + places + 2)
        result = value.quantize(Decimal(1).scaleb(-places), rounding=rounding)
    if result <= 0:
        raise ValueError("Price rounds to zero at this market's tick size")
    return _float(result)


def normalize_copy_limit(bound, size_step, *, buy):
    """Round toward the approved interval, never back to the unmarketable mid.

    Perpetual lot steps are powers of ten. BUY floors the maximum price;
    SELL ceils the minimum price. Neither operation widens approved slippage.
    """
    step = _decimal(size_step)
    if not step or type(buy) is not bool:
        raise ValueError('Explicit lot step and side required')
    digits = -step.adjusted()
    if step != Decimal(1).scaleb(-digits):
        raise ValueError('Unsupported perpetual lot step')
    return normalize_perp_price(bound, digits, rounding=ROUND_DOWN if buy else ROUND_UP)
