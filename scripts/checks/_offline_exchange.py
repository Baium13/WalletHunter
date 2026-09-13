"""An offline Hyperliquid account: the reads the adapter makes, answered from a dict.

Shared by the checks that drive a real AutonomousBackend holding a real
HyperliquidExecutionAdapter. Nothing here opens a socket, holds a credential or
signs anything - submit_copy_ioc mutates a dictionary and the account reads come
back out of it - which is what makes the live path testable at all.

Deliberately not named check_*: run_all.py runs the checks, not their fixtures.
"""
import types
from pathlib import Path

from core.foundation.contracts import InstrumentId, PortfolioSnapshot, Scope
from core.foundation.authorization import AuthorizationPolicy
from core.foundation.autonomous_allocation import AutonomousAllocationPolicy
from core.foundation.risk import RiskPolicy
from core.foundation.store import Store, scope_key
from core.foundation.copy_execution import HyperliquidExecutionAdapter
from core.foundation.data import copy_account_snapshot
from core.autonomous import AutonomousBackend, LiveGuardPolicy
from core.intelligence.models import IntelligencePolicy, LeaderScore, LeaderTradeEvent

NOW = 1_700_000_000_000
ACCOUNT = '0x' + 'b' * 40
WALLET = '0x' + 'a' * 40
SCOPE = Scope(tenant='t', account=ACCOUNT, network='MAINNET')
INST = InstrumentId(network='MAINNET', dex='', symbol='BTC')


class FakeInfo:
    def __init__(self, outer): self.outer = outer
    def user_state(self, account, pool): return {'time': self.outer.stamp}
    def user_fills_by_time(self, account, start, end):
        # The real endpoint is a time range, and reconcile_order relies on it:
        # any fill of this coin inside the window that is not this order's makes
        # the result UNKNOWN.
        return [f for f in self.outer.fills if start <= f['time'] <= end]


class FakeClient:
    """Answers the reads the adapter makes. submit_copy_ioc mutates a dict."""
    network = 'MAINNET'
    address = ACCOUNT

    def __init__(self, clock):
        self.clock = clock
        self.stamp = NOW - 1000
        self.rows = []
        self.fills = []
        self.orders = []
        self.info = FakeInfo(self)
        self.submitted = []
        self.oid = 41
        self.responses = {}
        # Reported on any position this account holds. None is what a venue
        # returns when it will not state one; a test that needs the liquidation
        # stop sets it.
        self.liquidation = None

    # --- reads
    def positions(self, *a): return [dict(r) for r in self.rows]
    def frontend_open_orders(self, pool): return list(self.orders) if pool == '' else []
    def capital_snapshot(self): return types.SimpleNamespace(sizing_base_usdc=1000.0)
    def available_margin(self, dex): return 1000.0
    def query_order_by_cloid(self, cloid): return self.responses[cloid]

    # --- writes (never leave this process)
    def set_leverage(self, coin, leverage, dex): return {'status': 'ok'}
    def response_error(self, response): return False
    def submit_copy_ioc(self, coin, is_buy, size, price, reduce_only, cloid, dex, expires_ms=None):
        self.submitted.append(dict(coin=coin, is_buy=is_buy, size=size, price=price,
                                   reduce_only=reduce_only, cloid=cloid, expires_ms=expires_ms))
        self.oid += 1
        oid = self.oid
        # The order is placed at, and settles at, the clock the intent was
        # minted on: reconcile_order requires created_ms <= placed <= status <=
        # the account watermark <= now, all inside max_age_ms.
        placed = self.clock()
        self.responses[cloid] = {'status': 'order', 'order': {
            'status': 'filled', 'statusTimestamp': placed,
            'order': {'oid': oid, 'cloid': cloid, 'coin': coin, 'side': 'B' if is_buy else 'A',
                      'reduceOnly': reduce_only, 'origSz': str(size), 'limitPx': str(price),
                      'timestamp': placed}}}
        self.fills.append({'coin': coin, 'oid': oid, 'side': 'B' if is_buy else 'A',
                           'tid': 900000 + oid, 'time': placed, 'sz': str(size), 'px': str(price)})
        # The exchange now reports the position this fill created.
        signed = sum(f['size'] if f['is_buy'] else -f['size'] for f in self.submitted)
        cost = sum(f['size'] * f['price'] for f in self.submitted)
        if abs(signed) < 1e-12:
            self.rows = []
        else:
            entry = cost / abs(signed)
            self.rows = [{'coin': coin, 'dex': dex or '', 'side': 'LONG' if signed > 0 else 'SHORT',
                          'size': abs(signed), 'entry_price': entry,
                          'position_value': abs(signed) * entry,
                          'margin_used': abs(signed) * entry / 5,
                          'leverage': 5, 'liquidation_price': self.liquidation, 'margin_mode': 'cross'}]
        self.stamp = self.clock()
        return {'status': 'ok'}


def live_backend(*, halted=False, tmp=None, protection=None):
    clock_box = {'now': NOW}
    clock = lambda: clock_box['now']
    client = FakeClient(clock)
    directory = Path(tmp)
    store = Store(directory / 'autonomy.sqlite')
    before = PortfolioSnapshot(scope=SCOPE, revision=1, exchange_ms=NOW - 2000, received_ms=NOW - 2000,
                               equity=1000.0, sizing_capital=1000.0, available_collateral=1000.0,
                               collateral_dex='', completeness='COMPLETE', evidence='EXCHANGE')
    adapter = HyperliquidExecutionAdapter(client, SCOPE, clock, before)
    allocation = AutonomousAllocationPolicy(scope=SCOPE, allocation_limit=200.0, other_allocation_limits=(),
                                            entry_fraction=0.5, max_position_margin=100.0, max_leverage=5)
    authorization = AuthorizationPolicy(scope=SCOPE, mode='LIVE_AUTO')
    risk = RiskPolicy(scope=SCOPE, instrument=INST, sources=('intelligence',), enabled=True, max_leverage=5,
                      min_notional=10.0, max_notional=500.0, max_symbol_notional=500.0, max_total_notional=500.0,
                      max_slippage_pct=0.5, max_price_deviation_pct=1.0, fee_buffer_pct=0.1, size_step=0.0001,
                      max_market_age_ms=60000, max_portfolio_age_ms=60000, max_intent_age_ms=60000)
    revisions = {'n': 1}

    def evidence(revision, dex):
        revisions['n'] = max(revisions['n'] + 1, revision)
        return copy_account_snapshot(client, SCOPE, revisions['n'], clock, dex or '')

    backend = AutonomousBackend(store, adapter, allocation, authorization, risk, clock,
                                live_guard=LiveGuardPolicy(halted=halted), evidence=evidence,
                                protection=protection)
    # Exactly what the unattended consumer does before its first drain: a live
    # scope opens on exchange evidence, never on a number from a config file.
    with store.transaction() as db:
        if not db.execute('SELECT 1 FROM portfolios WHERE scope=?', (scope_key(SCOPE),)).fetchone():
            store.publish_portfolio_in(db, evidence(1, ''), 'live-auto-open')
    return backend, client, clock_box, store


def book(bid=100.0, ask=100.05, size=200.0, now=NOW):
    return {'time': now - 200,
            'levels': [[{'px': str(bid - i * 0.01), 'sz': str(size)} for i in range(5)],
                       [{'px': str(ask + i * 0.01), 'sz': str(size)} for i in range(5)]]}


def candles(n=60, base=100.0, drift=0.02, now=NOW):
    out = []
    start = now - n * 900000
    for i in range(n):
        c = base + drift * i
        out.append({'T': start + i * 900000, 'c': str(c), 'h': str(c * 1.002), 'l': str(c * 0.998)})
    return out


def leader_score(now=NOW):
    return LeaderScore(wallet=WALLET, network='MAINNET', policy_id='leader-v1', computed_ms=now,
                       score=0.8, confidence=0.7, profit_quality=0.8, consistency=0.7,
                       drawdown_quality=0.7, sample_quality=0.7, recent_quality=0.7, anomaly=0.0,
                       qualified=True, reasons=())


def record(action='OPEN', side='BUY', size=1.0, before_size=0.0, after_size=1.0, now=NOW, event_id='e1'):
    event = LeaderTradeEvent(event_id=event_id, wallet=WALLET, instrument=INST, action=action, side=side,
                             size=size, before_size=before_size, after_size=after_size,
                             exchange_ms=now - 1000, received_ms=now - 500, fill_id='f-' + event_id)
    return {'event': event.model_dump(mode='json'), 'leader': leader_score(now).model_dump(mode='json'),
            'policy': IntelligencePolicy().model_dump(mode='json'), 'book': book(now=now),
            'candles': candles(now=now), 'consensus': {'created_ms': now - 400}}


def market_snapshot(instrument=INST, now=NOW, price=100.0):
    from core.foundation.contracts import MarketSnapshot
    return MarketSnapshot(instrument=instrument, exchange_ms=now - 100, received_ms=now - 100, price=price,
                          bid=price - 0.02, ask=price + 0.02, depth_usd=50000.0, completeness='COMPLETE',
                          freshness='FRESH', source='REST', source_version='offline-fixture-v1')
