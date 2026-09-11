"""Action-specific validation of durable, explicitly confirmed product actions.

An explicit account owner's reduction is not adoption into a strategy. The
expected position is a confirmation precondition, not fabricated provenance.
"""
import math
from decimal import Decimal
from .contracts import RiskDecision
from .store import digest


def evaluate(policy, intent, market, ledger, now, authorized, unresolved):
    s = ledger.portfolio
    reasons = []
    def check(ok, reason):
        if not ok: reasons.append(reason)
    reducing = intent.action in {'REDUCE','CLOSE','PLACE_STOP','CANCEL_OWNED'}
    current = next((p for p in s.positions if p.instrument == intent.instrument), None)
    check(authorized is True and intent.authorization == 'USER_CONFIRMED' and intent.execution_mode == 'LIVE', 'AUTHORIZATION_REQUIRED')
    check(intent.scope == policy.scope == s.scope and intent.instrument == policy.instrument == market.instrument, 'SCOPE_MISMATCH')
    check(policy.enabled, 'ACCOUNT_DISABLED')
    check(not unresolved and not ledger.pending_conflict, 'UNRESOLVED_EXECUTION')
    check(intent.created_ms <= now < intent.expires_ms and now-intent.created_ms <= policy.max_intent_age_ms, 'INTENT_EXPIRED')
    check(s.evidence == 'EXCHANGE' and s.exchange_ms is not None and 0 <= now-s.exchange_ms <= policy.max_portfolio_age_ms
          and 0 <= now-s.received_ms <= policy.max_portfolio_age_ms, 'ACCOUNT_STALE')
    if intent.action != 'CANCEL_OWNED':
        check(market.price is not None and market.freshness == 'FRESH' and 0 <= now-market.received_ms <= policy.max_market_age_ms, 'PRICE_STALE')
    check(intent.slippage_pct <= policy.max_slippage_pct, 'SLIPPAGE_LIMIT')
    try:
        if intent.action == 'OPEN': check(current is None, 'POSITION_ALREADY_EXISTS')
        elif intent.action == 'CANCEL_OWNED':
            check(intent.owned_order_id in ledger.owned_order_ids, 'ORDER_OWNERSHIP_UNPROVEN')
            check(any(o.instrument == intent.instrument and o.order_id == intent.owned_order_id and o.reduce_only for o in s.orders), 'ORDER_SCOPE')
        else:
            check(current is not None and ledger.matches_position(current), 'POSITION_CHANGED_OR_UNPROVEN')
            if current:
                check((current.side == 'LONG') != (intent.side == 'BUY') if reducing else (current.side == 'LONG') == (intent.side == 'BUY'), 'ACTION_SIDE')
                check(intent.leverage == current.leverage, 'LEVERAGE_CHANGE_NOT_AUTHORIZED')
                if reducing: check(intent.size <= current.size, 'REDUCTION_SIZE')
                if intent.action in {'CLOSE','PLACE_STOP'}: check(intent.size == current.size, 'POSITION_SIZE_CHANGED')
        if intent.action != 'CANCEL_OWNED':
            check(Decimal(str(intent.size)) % Decimal(str(policy.size_step)) == 0, 'SIZE_NOT_NORMALIZED')
            if intent.action == 'PLACE_STOP':
                check(intent.limit_price < market.price if intent.side == 'SELL' else intent.limit_price > market.price, 'STOP_SIDE')
            else:
                check(abs(intent.limit_price/market.price-1)*100 <= intent.slippage_pct+1e-8, 'PRICE_BOUNDARY')
        if not reducing:
            check(market.completeness == 'COMPLETE' and market.exchange_ms is not None and 0 <= now-market.exchange_ms <= policy.max_market_age_ms, 'MARKET_UNKNOWN')
            check(s.completeness == 'COMPLETE' and s.collateral_dex == intent.instrument.dex, 'COLLATERAL_UNKNOWN')
            check(not ledger.errors, 'LEDGER_RECONCILIATION_REQUIRED')
            check(not any(not o.reduce_only for o in s.orders), 'OPEN_ORDERS_UNRESOLVED')
            check(intent.leverage <= policy.max_leverage, 'LEVERAGE_LIMIT')
            notional = intent.size*max(market.price, intent.limit_price)
            margin = notional/intent.leverage
            check(math.isfinite(notional) and policy.min_notional <= notional <= policy.max_notional, 'NOTIONAL_LIMIT')
            check(notional+(current.notional if current else 0) <= policy.max_symbol_notional
                  and math.fsum(p.notional for p in s.positions)+notional <= policy.max_total_notional, 'EXPOSURE_LIMIT')
            check(margin <= ledger.available_source, 'SOURCE_CAPACITY')
            check(s.available_collateral is not None and margin+notional*policy.fee_buffer_pct/100 <= s.available_collateral, 'ACCOUNT_CAPACITY')
    except (TypeError, ValueError, ArithmeticError):
        reasons.append('FINANCIAL_EVIDENCE_INVALID')
    return RiskDecision(intent_id=intent.intent_id,intent_hash=digest(intent),policy_hash=digest(policy),market_hash=digest(market),
        outcome='REJECTED' if reasons else 'APPROVED',reasons=tuple(dict.fromkeys(reasons)),
        approved_size=0. if reasons else intent.size,approved_limit=0. if reasons else intent.limit_price,
        portfolio_revision=s.revision,created_ms=now)
