"""Production copy connection. Signing is confined to this gateway adapter.

The planner retains policy, but cannot submit. The journal and canonical store
share one database; the parent envelope remains the P1.2 reservation authority.
"""
import math
import time
from dataclasses import dataclass
from .contracts import Scope, InstrumentId, SourceContribution, OrderIntent, MarketSnapshot, ExecutionReceipt
from .data import account_snapshot
from .ledger import CopyLedger
from .risk import RiskGateway, RiskPolicy
from .store import Store
from .live_reconciliation import reconcile_order, client_order_id


@dataclass(frozen=True)
class LiveReport:
    receipt: ExecutionReceipt
    after: object


class HyperliquidExecutionAdapter:
    def __init__(self, client, scope, clock, before):
        self.__client, self.scope, self.clock, self.before = client, scope, clock, before
        self.response = None

    def validate_scope(self, intent):
        c = self.__client
        if intent.scope != self.scope or c.network != self.scope.network or c.address.lower() != self.scope.account:
            raise ValueError('Account/network mismatch')
        if intent.execution_mode != 'LIVE' or not (intent.authorization == 'COPY_POLICY' or
                (intent.version in (3,4) and intent.authorization == 'USER_CONFIRMED')):
            raise ValueError('Copy authorization required')

    def submit(self, intent, before, now):
        self.validate_scope(intent)
        self.before = before
        c, i = self.__client, intent.instrument
        coin = i.market_key.split('|')[0]
        if intent.action == 'PLACE_STOP':
            self.response = c.place_stop_loss(coin, 'LONG' if intent.side == 'SELL' else 'SHORT',
                intent.size, intent.limit_price, i.dex, cloid=client_order_id(intent))
            return self.query(intent)
        if intent.action == 'CANCEL_OWNED':
            self.response = c.cancel_order(coin, int(intent.owned_order_id), i.dex)
            return self.query(intent)
        if intent.configure_leverage or intent.action == 'LEVERAGE_UPDATE':
            response = c.set_leverage(coin, intent.leverage, i.dex)
            if c.response_error(response): raise ValueError('Leverage rejected; reconcile configuration')
        if intent.action != 'LEVERAGE_UPDATE':
            self.response = c.submit_copy_ioc(coin, intent.side == 'BUY', intent.size, intent.limit_price,
                intent.action in {'REDUCE','CLOSE'}, client_order_id(intent), i.dex, expires_ms=intent.expires_ms)
        return self.query(intent)

    def query(self, intent):
        self.validate_scope(intent)
        c, before = self.__client, self.before
        after = account_snapshot(c, intent.scope, getattr(self, 'next_revision', before.revision+1), self.clock, dex=intent.instrument.dex,
            require_collateral=intent.action not in {'REDUCE','CLOSE','PLACE_STOP','CANCEL_OWNED'})
        now = self.clock()
        if intent.action in {'PLACE_STOP','CANCEL_OWNED'}:
            receipt = self._protection_receipt(intent, before, after, now)
        elif intent.action == 'LEVERAGE_UPDATE':
            b = next((p for p in before.positions if p.instrument == intent.instrument), None)
            a = next((p for p in after.positions if p.instrument == intent.instrument), None)
            good = (a is not None and b is not None and a.side == b.side and a.size == b.size
                and a.entry_price == b.entry_price and a.leverage == intent.leverage and a.margin is not None
                and after.exchange_ms is not None and 0 <= now-after.exchange_ms <= 30000)
            receipt = ExecutionReceipt(intent_id=intent.intent_id, scope=intent.scope,
                status='CONFIGURED' if good else 'UNKNOWN', reconciliation='CONFIRMED' if good else 'RECONCILIATION_REQUIRED',
                received_ms=now, provenance='EXCHANGE' if good else 'UNKNOWN')
        else:
            receipt = reconcile_order(intent, before, after, c, now_ms=now, max_age_ms=30000)
            proof = getattr(c, 'verify_copy_execution', None)
            if callable(proof) and self.response is not None and receipt.status in {'FILLED','PARTIAL'}:
                def legacy(snapshot):
                    p = next((p for p in snapshot.positions if p.instrument == intent.instrument), None)
                    return None if p is None else dict(side=p.side,size=p.size,entry_price=p.entry_price,
                        leverage=p.leverage,position_value=p.notional,margin_used=p.margin)
                proof(self.response, intent.instrument.market_key.split('|')[0], intent.instrument.dex,
                    legacy(before), legacy(after), intent.created_ms)
        return LiveReport(receipt, after)

    def _protection_receipt(self, intent, before, after, now):
        """Exact reduce-only object proof, never a market-level cancellation."""
        c = self.__client
        oid = intent.owned_order_id
        good = False
        try:
            rows = c.frontend_open_orders(intent.instrument.dex)
            coin = intent.instrument.market_key.split('|')[0]
            if intent.action == 'PLACE_STOP':
                matches = [o for o in rows if o.get('cloid') == client_order_id(intent)]
                if len(matches) == 1:
                    o = matches[0]; oid = str(o['oid'])
                    good = (o['coin'] == coin and o['reduceOnly'] is True and o.get('isTrigger') is True
                            and str(o.get('orderType','')).lower() == 'stop market'
                            and o['side'] == ('B' if intent.side == 'BUY' else 'A')
                            and float(o['sz']) == intent.size and float(o['triggerPx']) == intent.limit_price)
            else:
                good = not any(str(o.get('oid')) == oid for o in rows)
            # A protection mutation cannot prove a concurrently changed position.
            same = lambda s: tuple((p.instrument,p.side,p.size,p.entry_price,p.leverage) for p in s.positions)
            good = good and same(before) == same(after) and after.exchange_ms is not None and 0 <= now-after.exchange_ms <= 30000
        except Exception:
            good = False
        return ExecutionReceipt(intent_id=intent.intent_id,scope=intent.scope,
            status='CONFIGURED' if good else 'UNKNOWN',order_ids=(oid,) if good and oid else (),
            reconciliation='CONFIRMED' if good else 'RECONCILIATION_REQUIRED',received_ms=now,
            provenance='EXCHANGE' if good else 'UNKNOWN')

    def refresh(self, revision, dex=''):
        """Read-only fresh bound account evidence for a confirmed proposal."""
        c = self.__client
        if c.network != self.scope.network or c.address.lower() != self.scope.account:
            raise ValueError('Account/network mismatch')
        return account_snapshot(c, self.scope, revision, self.clock, dex=dex)


def execute_copy(engine, account, client, operation, action, size, buy, spec, before_position, *, configure=False):
    """Called only after existing planner/ownership checks, under account lock."""
    from .execution import ExecutionGateway
    if not operation or not engine.journal: raise ValueError('Durable journal required')
    scope = Scope(tenant=str(account['_tenant']), account=account['address'].lower(), network=client.network)
    clock = lambda: int(time.time()*1000)
    store = Store(engine.journal.path)
    try: revision = store.portfolio(scope).revision+1
    except ValueError: revision = 1
    dex = spec.get('dex') or (before_position or {}).get('dex') or ''
    coin = spec.get('coin') or (before_position or {})['coin']
    instrument = InstrumentId(network=scope.network, dex=dex, symbol=coin.split(':')[-1])
    before = account_snapshot(client, scope, revision, clock, dex=dex, require_collateral=action not in {'REDUCE','CLOSE'})
    price = client.mid(coin, dex)
    if not math.isfinite(price) or price <= 0: raise ValueError('Price unavailable')
    slippage = float(engine.settings.max_slippage_pct)
    # Rounding inward must never widen the risk-approved bound.
    raw = price*(1 + slippage/100 if buy else 1-slippage/100)
    limit = client.round_price(coin, raw, dex)
    if (limit > raw if buy else limit < raw): limit = client.round_price(coin, price, dex)
    weights = spec.get('sources') or engine.journal.owned(scope.account).get(instrument.market_key, {}).get('source_targets', [])
    if action in {'OPEN','ADD','REDUCE'}:
        size = client.round_size(coin, size, dex)
    contributions = tuple(SourceContribution(source=x['wallet'].lower(), target_notional=abs(float(x['signed_notional'])),
        target_margin=float(x['margin'])) for x in weights)
    now = clock()
    intent = OrderIntent(version=2, intent_id=operation+'-'+action.lower(), parent_intent_id=operation,
        correlation_id=operation, scope=scope, instrument=instrument, source=contributions[0].source,
        source_contributions=contributions, action=action, side='BUY' if buy else 'SELL', size=float(size),
        limit_price=float(limit), leverage=int(spec['leverage']), slippage_pct=slippage,
        configure_leverage=configure, order_type='LEVERAGE' if action == 'LEVERAGE_UPDATE' else 'IOC',
        authorization='COPY_POLICY', execution_mode='LIVE', created_ms=now, expires_ms=now+30000)
    market = MarketSnapshot(instrument=instrument, exchange_ms=None, received_ms=now, price=price,
        bid=None, ask=None, completeness='UNKNOWN', freshness='FRESH', source='REST', source_version='copy-mid')
    allocation_sources = tuple(dict.fromkeys(tuple(spec.get('_allocation_sources') or account.get('_sources', ()))
                                           + tuple(x.source for x in contributions)))
    allocation_limits = spec.get('_allocation_limits')
    ownership_sources = spec.get('_ownership_sources')
    ownership_strategy = spec.get('_ownership_strategy')
    ledger = CopyLedger(before, allocation_sources, engine.journal, operation, spec,
                        allocation_limits=allocation_limits, ownership_sources=ownership_sources,
                        ownership_strategy=ownership_strategy)
    policy = RiskPolicy(scope=scope, instrument=instrument, sources=allocation_sources, enabled=bool(engine.settings.auto_trading),
        max_leverage=int(engine.settings.max_leverage), min_notional=float(engine.MIN_ORDER_NOTIONAL_USD),
        max_notional=float(engine.settings.max_total_exposure_usd), max_symbol_notional=float(engine.settings.max_total_exposure_usd),
        max_total_notional=float(engine.settings.max_total_exposure_usd), max_slippage_pct=slippage,
        max_price_deviation_pct=slippage, fee_buffer_pct=.1, size_step=client.size_step(coin,dex),
        max_market_age_ms=30000,max_portfolio_age_ms=30000,max_intent_age_ms=30000)
    adapter = HyperliquidExecutionAdapter(client, scope, clock, before)
    gateway = ExecutionGateway(store, RiskGateway(policy), adapter, clock)
    store.publish_portfolio(before, operation)
    gateway.authorize_copy(intent)
    receipt = gateway.execute(intent, market, copy_ledger=ledger)
    if receipt.status not in {'FILLED','PARTIAL','CONFIGURED'}:
        raise ValueError('Copy '+receipt.status+'; execution reconciliation required')
    return receipt


def recover_pending_copy(engine, account, client):
    """Bounded query-only restart recovery; never mints authority or submits.

    A terminal partial close remains HOLD under the pre-existing close policy.
    Full order proof may restore the already executed journal projection.
    """
    from .execution import ExecutionGateway
    from .contracts import PortfolioSnapshot
    from .store import scope_key
    scope = Scope(tenant=str(account['_tenant']), account=account['address'].lower(), network=client.network)
    store = Store(engine.journal.path)
    with store.transaction() as db:
        rows = db.execute("SELECT i.body,p.body AS pre,r.body AS policy FROM intents i JOIN intent_prestate p ON p.id=i.id JOIN policies r ON r.hash=json_extract(i.decision,'$.policy_hash') WHERE i.scope=? AND i.status IN ('UNKNOWN','SUBMITTING') LIMIT 3", (scope_key(scope),)).fetchall()
    recovered = set()
    for row in rows:
        intent = OrderIntent.model_validate_json(row['body'])
        if intent.version != 2: continue
        before = PortfolioSnapshot.model_validate_json(row['pre'])
        clock = lambda: int(time.time()*1000)
        adapter = HyperliquidExecutionAdapter(client, scope, clock, before)
        gateway = ExecutionGateway(store, RiskGateway(RiskPolicy.model_validate_json(row['policy'])), adapter, clock)
        receipt = gateway.recover(intent)
        if receipt.status == 'FILLED' and engine.journal.resolve_canonical_copy(intent.intent_id):
            recovered.add(intent.instrument.market_key)
    return recovered
