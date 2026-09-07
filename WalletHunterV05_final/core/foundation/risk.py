"""Pure deterministic gate. Policy is trusted configuration, not agent output."""
import math
from decimal import Decimal
from typing import Annotated
from pydantic import Field
from .contracts import Amount, Positive, Millis, Name, Contract, Scope, InstrumentId, OrderIntent, MarketSnapshot, RiskDecision
from .store import digest


class RiskPolicy(Contract):
    scope: Scope
    instrument: InstrumentId
    sources: tuple[Name, ...]
    enabled: bool
    max_leverage: Annotated[int, Field(strict=True, ge=1)]
    min_notional: Amount
    max_notional: Positive
    max_symbol_notional: Positive
    max_total_notional: Positive
    max_slippage_pct: Positive
    max_price_deviation_pct: Amount
    fee_buffer_pct: Amount
    size_step: Positive
    max_market_age_ms: Millis
    max_portfolio_age_ms: Millis
    max_intent_age_ms: Millis


class RiskGateway:
    def __init__(self, policy):
        self.policy = RiskPolicy.model_validate_json(policy.model_dump_json())

    def evaluate(self, intent, market, ledger, now, *, authorized=False, unresolved=False):
        intent = OrderIntent.model_validate_json(intent.model_dump_json())
        market = MarketSnapshot.model_validate_json(market.model_dump_json())
        if intent.version == 2:
            return self._copy(intent, market, ledger, now, authorized, unresolved)
        p, snapshot = self.policy, ledger.portfolio
        reasons = []
        def require(condition, reason):
            if not condition: reasons.append(reason)
        require(intent.scope == p.scope == snapshot.scope and intent.instrument == p.instrument == market.instrument, "SCOPE_MISMATCH")
        require(p.enabled, "ACCOUNT_DISABLED")
        require(authorized is True, "AUTHORIZATION_REQUIRED")
        # No production execution adapter is enabled in this additive block.
        require((intent.execution_mode in {"FAKE", "PAPER"} and intent.authorization == "PAPER_TEST")
            or (intent.execution_mode == 'PAPER' and intent.authorization == 'PAPER_POLICY')
            or (intent.version == 3 and intent.execution_mode == 'LIVE' and intent.authorization == 'USER_CONFIRMED'), "LIVE_ROUTE_NOT_MIGRATED")
        require(intent.action == "OPEN", "ACTION_NOT_MIGRATED")
        require(not unresolved, "UNRESOLVED_EXECUTION")
        require(type(now) is int and intent.created_ms <= now < intent.expires_ms
            and 0 <= now-intent.created_ms <= p.max_intent_age_ms, "INTENT_EXPIRED")
        require(snapshot.completeness == "COMPLETE" and snapshot.evidence == ('EXCHANGE' if intent.version == 3 else 'FAKE')
            and snapshot.exchange_ms is not None and 0 <= now-snapshot.exchange_ms <= p.max_portfolio_age_ms
            and 0 <= now-snapshot.received_ms <= p.max_portfolio_age_ms, "PORTFOLIO_STALE_OR_UNKNOWN")
        if intent.version == 3:
            require(snapshot.collateral_dex == intent.instrument.dex, 'COLLATERAL_POOL_MISMATCH')
        require(market.completeness == "COMPLETE" and market.freshness == "FRESH" and market.exchange_ms is not None
            and 0 <= now-market.exchange_ms <= p.max_market_age_ms and 0 <= now-market.received_ms <= p.max_market_age_ms,
            "MARKET_STALE_OR_UNKNOWN")
        require(not ledger.errors, "LEDGER_RECONCILIATION_REQUIRED")
        require(not snapshot.orders, "OPEN_ORDERS_REQUIRE_RECONCILIATION")
        require(not any(row.instrument == intent.instrument for row in snapshot.positions), "POSITION_ALREADY_EXISTS")
        require(intent.leverage <= p.max_leverage, "LEVERAGE_LIMIT")
        require(intent.slippage_pct <= p.max_slippage_pct, "SLIPPAGE_LIMIT")
        try:
            require(Decimal(str(intent.size)) % Decimal(str(p.size_step)) == 0, "SIZE_NOT_NORMALIZED")
            reference = market.price
            if reference is None: raise ValueError("unknown price")
            deviation = abs(intent.limit_price/reference-1)*100
            require(deviation <= p.max_price_deviation_pct+1e-10, "PRICE_DEVIATION")
            require(deviation <= intent.slippage_pct+1e-10, "INTENT_SLIPPAGE_EXCEEDED")
            notional = intent.size*max(reference, intent.limit_price)
            margin = notional/intent.leverage
            capacity = margin+notional*p.fee_buffer_pct/100
            total = math.fsum(row.notional for row in snapshot.positions)+notional
            require(all(math.isfinite(x) for x in (notional, margin, capacity, total)), "NONFINITE_CALCULATION")
            require(p.min_notional <= notional <= p.max_notional, "NOTIONAL_LIMIT")
            require(notional <= p.max_symbol_notional and total <= p.max_total_notional, "EXPOSURE_LIMIT")
            allocation = ledger.allocation(intent.source)
            require(margin <= allocation.available, "SOURCE_CAPACITY")
            require(capacity <= ledger.available_capacity, "ACCOUNT_CAPACITY")
        except (ValueError, ArithmeticError, TypeError):
            reasons.append("FINANCIAL_EVIDENCE_INVALID")
        return RiskDecision(intent_id=intent.intent_id, intent_hash=digest(intent), policy_hash=digest(p), market_hash=digest(market),
            outcome="REJECTED" if reasons else "APPROVED", reasons=tuple(dict.fromkeys(reasons)),
            approved_size=0. if reasons else intent.size, approved_limit=0. if reasons else intent.limit_price,
            portfolio_revision=snapshot.revision, created_ms=now)

    def _copy(self, intent, market, ledger, now, authorized, unresolved):
        p, s = self.policy, ledger.portfolio
        reasons = []
        def check(ok, code):
            if not ok: reasons.append(code)
        reducing = intent.action in {'REDUCE', 'CLOSE'}
        check(intent.scope == p.scope == s.scope and intent.instrument == p.instrument == market.instrument, 'SCOPE_MISMATCH')
        check(authorized is True and intent.authorization == 'COPY_POLICY' and intent.execution_mode == 'LIVE', 'AUTHORIZATION_REQUIRED')
        check(p.enabled, 'ACCOUNT_DISABLED')
        check(not unresolved, 'UNRESOLVED_EXECUTION')
        check(intent.created_ms <= now < intent.expires_ms and now-intent.created_ms <= p.max_intent_age_ms, 'INTENT_EXPIRED')
        check(s.evidence == 'EXCHANGE' and s.exchange_ms is not None
            and 0 <= now-s.exchange_ms <= p.max_portfolio_age_ms
            and 0 <= now-s.received_ms <= p.max_portfolio_age_ms, 'ACCOUNT_STALE')
        check(market.price is not None and market.freshness == 'FRESH'
            and 0 <= now-market.received_ms <= p.max_market_age_ms, 'PRICE_STALE')
        check(intent.slippage_pct <= p.max_slippage_pct, 'SLIPPAGE_LIMIT')
        try:
            b = next((x for x in s.positions if x.instrument == intent.instrument), None)
            if intent.action == 'OPEN': check(b is None, 'OPEN_SCOPE')
            if intent.action == 'ADD': check(b is not None and (b.side == 'LONG') == (intent.side == 'BUY'), 'ADD_SCOPE')
            if intent.action == 'LEVERAGE_UPDATE': check(b is not None and b.size == intent.size, 'LEVERAGE_SCOPE')
            if b is not None: check(ledger.owns(intent), 'OWNERSHIP_UNPROVEN')
            if reducing:
                check(b is not None and (b.side == 'LONG') != (intent.side == 'BUY') and intent.size <= b.size, 'REDUCTION_SCOPE')
                if intent.action == 'CLOSE': check(b is not None and intent.size == b.size, 'CLOSE_SCOPE')
            else:
                check(s.completeness == 'COMPLETE' and s.collateral_dex == intent.instrument.dex, 'COLLATERAL_UNKNOWN')
                check(not ledger.errors and ledger.permits(intent, market.price), 'SOURCE_CAPACITY')
                check(not any(not o.reduce_only for o in s.orders), 'OPEN_ORDERS_UNRESOLVED')
                check(intent.leverage <= p.max_leverage, 'LEVERAGE_LIMIT')
                current = b.size if b else 0.
                target = current if intent.action == 'LEVERAGE_UPDATE' else current+intent.size
                added = 0. if intent.action == 'LEVERAGE_UPDATE' else intent.size*market.price
                margin = max(0., target*market.price/intent.leverage - (b.notional/b.leverage if b else 0.))
                reserved = math.fsum(x.reserved_margin for x in ledger.book.accounts.values()) if ledger.book else float('inf')
                required = margin + added*(.001 + intent.slippage_pct/100)
                check(math.isfinite(required) and s.available_collateral is not None
                    and required <= max(0., s.available_collateral-reserved)+1e-9, 'ACCOUNT_CAPACITY')
                if intent.action != 'LEVERAGE_UPDATE':
                    check(intent.size*market.price >= p.min_notional, 'MIN_NOTIONAL')
                check(target*market.price <= p.max_symbol_notional+1e-9, 'EXPOSURE_LIMIT')
                check(math.fsum(x.notional for x in s.positions)+added <= p.max_total_notional+1e-9, 'TOTAL_EXPOSURE_LIMIT')
            if intent.action != 'LEVERAGE_UPDATE':
                check(Decimal(str(intent.size)) % Decimal(str(p.size_step)) == 0, 'SIZE_NOT_NORMALIZED')
            check(abs(intent.limit_price/market.price-1)*100 <= intent.slippage_pct+1e-8, 'PRICE_BOUNDARY')
        except (ValueError, ArithmeticError, TypeError, AttributeError):
            reasons.append('FINANCIAL_EVIDENCE_INVALID')
        return RiskDecision(intent_id=intent.intent_id, intent_hash=digest(intent), policy_hash=digest(p), market_hash=digest(market),
            outcome='REJECTED' if reasons else 'APPROVED', reasons=tuple(dict.fromkeys(reasons)),
            approved_size=0. if reasons else intent.size, approved_limit=0. if reasons else intent.limit_price,
            portfolio_revision=s.revision, created_ms=now)
