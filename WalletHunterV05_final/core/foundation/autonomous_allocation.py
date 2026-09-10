"""Explicit source budget over canonical account evidence; no free-balance sizing."""
import math
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Annotated
from pydantic import Field
from .contracts import Contract, Scope, Name, Amount, Positive, Allocation, PortfolioSnapshot


class AutonomousAllocationPolicy(Contract):
    scope: Scope
    source: Name = 'intelligence'
    allocation_limit: Amount
    other_allocation_limits: tuple[Amount,...]
    entry_fraction: Annotated[float,Field(strict=True,gt=0,le=1,allow_inf_nan=False)]
    max_position_margin: Positive
    max_leverage: Annotated[int,Field(strict=True,ge=1,le=100)]
    policy_id: Name = 'autonomous-allocation-v1'


class AutonomousLedger:
    def __init__(self,portfolio,policy,reservations=()):
        self.portfolio=PortfolioSnapshot.model_validate_json(portfolio.model_dump_json())
        self.policy=AutonomousAllocationPolicy.model_validate_json(policy.model_dump_json())
        self.errors=[]; self.allocations={}; self.available_capacity=None
        p,c=self.portfolio,self.policy
        def require(ok,reason):
            if not ok: self.errors.append(reason)
        require(p.scope==c.scope,'SCOPE_MISMATCH')
        require(p.completeness=='COMPLETE' and p.evidence!='LEGACY_UNKNOWN','PORTFOLIO_UNKNOWN')
        require(p.sizing_capital is not None and c.allocation_limit+math.fsum(c.other_allocation_limits)<=p.sizing_capital,'ALLOCATION_OVERCOMMITTED')
        committed=reserved=capacity_reserved=0.
        try:
            for position in p.positions:
                require(position.evidence=='VERIFIED' and position.margin is not None,'ATTRIBUTION_UNKNOWN')
                total=math.fsum(x.notional for x in position.contributions)
                require(math.isclose(total,position.notional,rel_tol=1e-9,abs_tol=1e-9),'ATTRIBUTION_INCONSISTENT')
                if total<=0 or position.margin is None: continue
                share=math.fsum(x.notional for x in position.contributions if x.source==c.source)/total
                committed+=position.margin*share
            seen=set()
            for row in reservations:
                if row.intent_id in seen: raise ValueError('DUPLICATE_RESERVATION')
                seen.add(row.intent_id)
                if any(type(v) not in (float,int) or not math.isfinite(v) or v<0 for v in (row.margin,row.account_capacity)) or row.account_capacity<row.margin:
                    raise ValueError('RESERVATION_INVALID')
                if row.source==c.source: reserved+=row.margin
                capacity_reserved+=row.account_capacity
            require(all(math.isfinite(v) for v in (committed,reserved,capacity_reserved)),'CAPITAL_OVERFLOW')
            # Overcommitted open capital stays open/HOLD. It never creates more
            # available allocation and is surfaced for reconciliation.
            require(committed+reserved<=c.allocation_limit+1e-9,'SOURCE_OVERCOMMITTED')
            allocation=Allocation(scope=p.scope,source=c.source,limit=c.allocation_limit,committed=committed,reserved=reserved,
                available=max(0.,c.allocation_limit-committed-reserved),revision=p.revision,received_ms=p.received_ms)
            self.allocations[c.source]=allocation
            if not self.errors:
                self.available_capacity=max(0.,p.available_collateral-capacity_reserved)
        except (ValueError,TypeError,ArithmeticError):
            self.errors.append('CAPITAL_INVALID')
        if self.errors: self.available_capacity=None

    def allocation(self,source):
        if self.errors or source!=self.policy.source: raise ValueError('ALLOCATION_RECONCILIATION_REQUIRED')
        return self.allocations[source]

    def size(self,price,leverage,size_step,leader_confidence,consensus_confidence,min_notional=0.):
        """Floor a bounded incremental size; confidence cannot increase the cap.

        ``min_notional`` is the venue's smallest tradeable notional. Confidence
        scaling can put an otherwise valid signal underneath it, and the intent
        is then built only to die further down the pipeline with NOTIONAL_LIMIT.
        When the UNSCALED budget still covers the floor, size to the floor
        instead: the smallest expressible position is the honest way to express
        low confidence. The budget cap is never exceeded, so a signal the
        account genuinely cannot afford still returns zero.
        """
        values=(price,size_step,leader_confidence,consensus_confidence,min_notional)
        if any(type(v) not in (float,int) or not math.isfinite(v) for v in values): raise ValueError('SIZING_INVALID')
        if price<=0 or size_step<=0 or min_notional<0 or type(leverage) is not int or not 1<=leverage<=self.policy.max_leverage:
            raise ValueError('SIZING_INVALID')
        if not 0<=leader_confidence<=1 or not 0<=consensus_confidence<=1: raise ValueError('CONFIDENCE_INVALID')
        a=self.allocation(self.policy.source)
        budget=min(a.available,self.available_capacity,self.policy.max_position_margin,
            self.policy.allocation_limit*self.policy.entry_fraction)
        margin=budget*leader_confidence*consensus_confidence
        step=Decimal(str(size_step))
        raw=Decimal(str(margin))*leverage/Decimal(str(price))
        result=float((raw/step).to_integral_value(rounding=ROUND_DOWN)*step)
        if min_notional>0 and result*price<min_notional:
            floor=float((Decimal(str(min_notional))/Decimal(str(price))/step).to_integral_value(rounding=ROUND_UP)*step)
            result=floor if floor*price<=budget*leverage+1e-9 else 0.
        if not math.isfinite(result): raise ValueError('SIZING_OVERFLOW')
        return result
