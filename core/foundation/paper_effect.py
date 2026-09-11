"""Deterministic zero-fee PAPER accounting, independently checked at settlement.

This is a canonical adapter model, not a prediction of actual exchange fills.
"""
import math
from .contracts import Position,Contribution,PortfolioSnapshot


def effect(intent,before,fills,now):
    current=next((p for p in before.positions if p.instrument==intent.instrument),None)
    size=math.fsum(f.size for f in fills)
    if size>intent.size or not math.isfinite(size): raise ValueError('Fill size invalid')
    positions=before.positions; collateral=before.available_collateral; equity=before.equity
    if size:
        cost=math.fsum(f.size*f.price for f in fills); average=cost/size
        if intent.action in ('OPEN','ADD'):
            if intent.action=='OPEN' and current is not None: raise ValueError('Existing position')
            if intent.action=='ADD' and (current is None or (current.side=='LONG')!=(intent.side=='BUY')): raise ValueError('ADD scope')
            if current and (current.evidence!='VERIFIED' or current.leverage!=intent.leverage or
                    any(c.source!=intent.source for c in current.contributions)): raise ValueError('ADD ownership')
            total=size+(current.size if current else 0.)
            total_cost=cost+(current.size*current.entry_price if current else 0.)
            margin=total_cost/intent.leverage
            collateral-=margin-(current.margin if current else 0.)
            position=Position(instrument=intent.instrument,side='LONG' if intent.side=='BUY' else 'SHORT',size=total,
                entry_price=total_cost/total,notional=total_cost,margin=margin,leverage=intent.leverage,evidence='VERIFIED',
                order_ids=tuple(dict.fromkeys((current.order_ids if current else ())+tuple(f.order_id for f in fills))),
                contributions=(Contribution(source=intent.source,notional=total_cost),))
            positions=tuple(p for p in positions if p.instrument!=intent.instrument)+(position,)
        elif intent.action in ('REDUCE','CLOSE'):
            if (current is None or current.evidence!='VERIFIED' or current.margin is None or
                    any(c.source!=intent.source for c in current.contributions) or
                    (current.side=='LONG')==(intent.side=='BUY') or size>current.size): raise ValueError('Reduction scope')
            if intent.action=='CLOSE' and intent.size!=current.size: raise ValueError('Close size')
            pnl=(average-current.entry_price)*size*(1 if current.side=='LONG' else -1)
            released=current.margin*size/current.size
            collateral+=released+pnl; equity+=pnl
            positions=tuple(p for p in positions if p.instrument!=intent.instrument)
            remaining=current.size-size
            if remaining>0:
                notional=current.entry_price*remaining
                position=Position.model_validate(dict(current.model_dump(),size=remaining,notional=notional,
                    margin=current.margin-released,contributions=(Contribution(source=intent.source,notional=notional),)))
                positions+=(position,)
        else: raise ValueError('Unsupported PAPER action')
    return PortfolioSnapshot.model_validate(dict(before.model_dump(),positions=positions,equity=equity,
        available_collateral=collateral,revision=before.revision+1,received_ms=now,exchange_ms=now))
