"""An exit that does not depend on the leader ever acting again.

Every exit in this system is caused by a leader CLOSE arriving on a feed whose
stall is indistinguishable from "healthy and idle". There is no stop loss, no
maximum hold time and no liquidation-distance check anywhere in the tree, so a
position opened at 03:00 on a feed that dies at 03:01 stays open until a person
looks at it.

This module answers one question - is this position due to be closed on policy
alone - and does nothing else. It places no orders, reads no exchange, holds no
state and can never increase exposure. Both thresholds are off by default, so a
deployment that does not configure them behaves exactly as it did before.
"""
from typing import Annotated
from pydantic import Field
from core.foundation.contracts import Contract, Name, Scope


class PositionProtectionPolicy(Contract):
    scope: Scope
    # 0 disables. A position the leader never closed is not a position we chose
    # to keep; it is one that nothing has decided about since it was opened.
    max_hold_ms: Annotated[int, Field(strict=True, ge=0, le=30*24*3600*1000)] = 0
    # Close when the mark is within this percentage of the liquidation price.
    # 0 disables. An absent liquidation price is never read as "far away".
    liquidation_buffer_pct: Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)] = 0.
    policy_id: Name = 'protection-v1'

    @property
    def enabled(self):
        return bool(self.max_hold_ms or self.liquidation_buffer_pct)


def protection_reasons(position, mark, opened_ms, now, policy):
    """Why this position must be closed now, on policy alone. Empty means no.

    Nothing here infers. A missing mark or a missing liquidation price disables
    the distance check for this call rather than passing it: the point of the
    check is to act on evidence of danger, and absent evidence is not evidence
    of safety - it just means this particular stop cannot speak.
    """
    reasons = []
    if policy.max_hold_ms and type(opened_ms) is int and type(now) is int and now-opened_ms >= policy.max_hold_ms:
        reasons.append('MAX_HOLD_EXCEEDED')
    if policy.liquidation_buffer_pct and position.liquidation_price is not None:
        if isinstance(mark, (int, float)) and not isinstance(mark, bool) and mark > 0:
            liquidation = position.liquidation_price
            # Past the liquidation price is not a distance from it.
            crossed = liquidation >= mark if position.side == 'LONG' else liquidation <= mark
            if crossed or abs(mark-liquidation)/mark*100 <= policy.liquidation_buffer_pct:
                reasons.append('LIQUIDATION_PROXIMITY')
    return tuple(reasons)
