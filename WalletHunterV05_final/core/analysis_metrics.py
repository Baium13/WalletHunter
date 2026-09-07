"""JSON-safe representations of analytical ratios, never trading amounts."""
import math


def persisted_profit_factor(value):
    """No negative cashflows means an unbounded PF, not an invalid balance."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Profit factor must be a non-negative numeric ratio")
    if value == math.inf:
        return "Infinity"
    if not math.isfinite(value) or value < 0:
        raise ValueError("Profit factor must be a non-negative numeric ratio")
    return value
