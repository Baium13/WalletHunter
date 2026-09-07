"""Manual Leader Copy policy and proportional target calculation.

This module is deliberately separate from autonomous intelligence allocation.
It only produces deterministic copy targets; execution remains owned by the
canonical risk/execution gateway.
"""
import math
import hashlib
from typing import Literal
from pydantic import Field
from core.foundation.contracts import Contract, Scope, Name, Amount, Millis


class ManualLeaderConfig(Contract):
    scope: Scope
    leader: Name
    alias: Name
    allocation_pct: float = Field(gt=0, le=100, allow_inf_nan=False)
    enabled: bool = False
    created_ms: Millis
    updated_ms: Millis
    strategy: Literal['MANUAL_LEADER_COPY'] = 'MANUAL_LEADER_COPY'

    def capital_limit(self, allocatable_capital: Amount) -> float:
        capital = float(allocatable_capital)
        if not math.isfinite(capital) or capital < 0:
            raise ValueError('Allocatable capital unavailable')
        return capital * self.allocation_pct / 100.0


class ManualLeaderCopy:
    def __init__(self, config: ManualLeaderConfig):
        self.config = ManualLeaderConfig.model_validate_json(config.model_dump_json())

    def start(self, now: int) -> ManualLeaderConfig:
        return self.config.model_copy(update={'enabled': True, 'updated_ms': now})

    def stop(self, now: int, *, close_positions: bool = False) -> tuple[ManualLeaderConfig, bool]:
        # False means retain existing positions as HOLD; caller must explicitly
        # route close_positions through Risk/Gateway when requested.
        return self.config.model_copy(update={'enabled': False, 'updated_ms': now}), bool(close_positions)

    def proportional_margin(self, *, leader_margin: Amount, leader_capital: Amount,
                            allocatable_capital: Amount, fee_reserve_pct: float = 0.0) -> float:
        values = [float(leader_margin), float(leader_capital), float(allocatable_capital), float(fee_reserve_pct)]
        if any(not math.isfinite(v) for v in values) or leader_margin < 0 or leader_capital <= 0 or allocatable_capital < 0 or fee_reserve_pct < 0 or fee_reserve_pct >= 100:
            raise ValueError('Invalid leader or allocation evidence')
        base = self.config.capital_limit(allocatable_capital)
        return base * (float(leader_margin) / float(leader_capital)) * (1.0 - fee_reserve_pct / 100.0)

    def target_margin(self, *, leader_margin: Amount, leader_capital: Amount,
                      allocatable_capital: Amount, fee_reserve_pct: float = 0.0,
                      max_margin: Amount | None = None) -> float:
        target = self.proportional_margin(leader_margin=leader_margin, leader_capital=leader_capital,
            allocatable_capital=allocatable_capital, fee_reserve_pct=fee_reserve_pct)
        if max_margin is not None:
            cap = float(max_margin)
            if not math.isfinite(cap) or cap < 0: raise ValueError('Invalid margin cap')
            target = min(target, cap)
        return target

    def plan_event(self, event_id: Name, action: Literal['OPEN','ADD','REDUCE','CLOSE','REVERSE'],
                   *, leader_margin: Amount, leader_capital: Amount,
                   allocatable_capital: Amount, current_margin: Amount = 0.0,
                   fee_reserve_pct: float = 0.0, max_margin: Amount | None = None) -> dict:
        """Return a proportional delta; this object never submits an order."""
        if not self.config.enabled:
            return {'event_id': event_id, 'status': 'STOPPED', 'target_margin': float(current_margin), 'delta_margin': 0.0}
        if not isinstance(event_id, str) or not event_id:
            raise ValueError('Leader event identity required')
        current = float(current_margin)
        if not math.isfinite(current) or current < 0:
            raise ValueError('Current attributable margin unavailable')
        target = 0.0 if action == 'CLOSE' else self.target_margin(
            leader_margin=leader_margin, leader_capital=leader_capital,
            allocatable_capital=allocatable_capital, fee_reserve_pct=fee_reserve_pct,
            max_margin=max_margin)
        if action == 'REDUCE': target = min(target, current)
        if action == 'REVERSE':
            target = self.target_margin(leader_margin=leader_margin, leader_capital=leader_capital,
                allocatable_capital=allocatable_capital, fee_reserve_pct=fee_reserve_pct, max_margin=max_margin)
        delta = target-current
        return {'event_id': event_id, 'status': 'READY' if abs(delta)>1e-12 else 'NO_CHANGE',
            'target_margin': target, 'delta_margin': delta, 'strategy': self.config.strategy,
            'leader': self.config.leader, 'allocation_pct': self.config.allocation_pct}


class ManualLeaderEventBook:
    """Persistent-safe event identity book; caller still routes execution through gateway."""
    def __init__(self):
        self._seen = set()

    def accept(self, event_id: str) -> bool:
        if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
            raise ValueError('Invalid leader event identity')
        key = hashlib.sha256(event_id.encode()).hexdigest()
        if key in self._seen:
            return False
        self._seen.add(key)
        return True
