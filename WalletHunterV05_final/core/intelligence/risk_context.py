"""Read-only advisory checks. This module cannot authorize or execute orders."""
import math
from .models import AgentResult, RiskContextEvidence


def analyze(event, agents, evidence, now, policy):
    blocked=[]; cautions=[]
    def check(ok,reason):
        if not ok: blocked.append(reason)
    check(0<=now-event.exchange_ms<=policy.max_signal_age_ms,'SIGNAL_STALE')
    by_id={a.agent_id:a for a in agents}
    for key in ('liquidity','volatility'):
        a=by_id.get(key)
        check(a is not None and a.instrument==event.instrument and a.freshness=='FRESH'
            and 0<=now-a.created_ms<=policy.max_market_age_ms and a.direction not in ('WAIT','BLOCK'),key.upper()+'_UNAVAILABLE')
        if a and a.direction=='CAUTION': cautions.append(key.upper()+'_CAUTION')
    try:
        e=RiskContextEvidence.model_validate_json(evidence.model_dump_json())
        p,m,a=e.portfolio,e.market,e.allocation
        check(p.scope==a.scope and m.instrument==event.instrument and p.scope.network==event.instrument.network,'CONTEXT_SCOPE')
        check(p.completeness=='COMPLETE' and p.evidence!='LEGACY_UNKNOWN','ACCOUNT_INCOMPLETE')
        check(p.exchange_ms is not None and 0<=now-p.exchange_ms<=policy.max_market_age_ms
            and 0<=now-p.received_ms<=policy.max_market_age_ms,'ACCOUNT_STALE')
        check(m.completeness=='COMPLETE' and m.freshness=='FRESH' and m.exchange_ms is not None
            and 0<=now-m.exchange_ms<=policy.max_market_age_ms and 0<=now-m.received_ms<=policy.max_market_age_ms,'MARKET_STALE_OR_INCOMPLETE')
        check(a.revision==p.revision and 0<=now-a.received_ms<=policy.max_market_age_ms,'ALLOCATION_STALE')
        check(e.unresolved is False,'EXECUTION_UNRESOLVED_OR_UNKNOWN')
        check(not any(x.evidence!='VERIFIED' or x.margin is None for x in p.positions),'EXPOSURE_UNATTRIBUTED')
        check(not any(not x.reduce_only for x in p.orders),'OPEN_ORDERS_UNRESOLVED')
        check(math.isfinite(a.committed+a.reserved) and math.isclose(a.available,max(0.,a.limit-a.committed-a.reserved),rel_tol=1e-10,abs_tol=1e-10),'ALLOCATION_INCONSISTENT')
        check(e.required_margin<=a.available and e.required_capacity>=e.required_margin,'ALLOCATION_CAPACITY')
        check(p.available_collateral is not None and e.required_capacity<=p.available_collateral,'ACCOUNT_CAPACITY')
        check(e.slippage_pct<=e.max_slippage_pct,'SLIPPAGE_LIMIT')
        if m.ask is not None and m.bid is not None:
            mid=m.bid+(m.ask-m.bid)/2
            check(math.isfinite(mid) and (m.ask-m.bid)/mid*10000<=policy.max_spread_bps,'SPREAD_LIMIT')
        else: check(False,'SPREAD_UNAVAILABLE')
    except (ValueError,TypeError,AttributeError,ArithmeticError):
        blocked.append('FINANCIAL_CONTEXT_UNAVAILABLE')
    state='BLOCK' if blocked else 'CAUTION' if cautions else 'PASS'
    return AgentResult(agent_id='risk_context',instrument=event.instrument,created_ms=now,direction=state,
        confidence=1.,score=1. if state=='PASS' else -1.,evidence=tuple(blocked+cautions) or ('CONTEXT_CHECKS_PASS',),
        freshness='FRESH',feature_version='risk-context-v2',strategy_version='risk-context-v2')
