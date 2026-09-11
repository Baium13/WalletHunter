"""Leverage clamp, per-class liquidity, order flow (patch 0006)."""
import sys, math, time, types
sys.path.insert(0, '.')

from core.foundation.contracts import Contribution, InstrumentId, Position
from core.intelligence.models import (AgentResult, IntelligencePolicy, LeaderTradeEvent,
                                      LiquidityThresholds)
from core.intelligence.agents import consensus, evaluate

NOW = 1_700_000_000_000


def ok(label, cond, detail=''):
    if not cond:
        raise AssertionError(label + ' :: ' + str(detail))
    print('  ok  ' + label)


def inst(dex='', symbol='BTC'):
    return InstrumentId(network='MAINNET', dex=dex, symbol=symbol)


def event(dex='', symbol='BTC', side='BUY', action='OPEN'):
    return LeaderTradeEvent(event_id='e1', wallet='0x' + 'a' * 40, instrument=inst(dex, symbol),
                            action=action, side=side, size=1.0, before_size=0.0, after_size=1.0,
                            exchange_ms=NOW, received_ms=NOW, fill_id='f1')


# ------------------------------------------------------------------- leverage
print('leverage is clamped to what the venue allows')
from core.autonomous import AutonomousBackend

class Stub:
    """Only the fields _leverage touches; no store, no exchange, no adapter."""
    _whole = staticmethod(AutonomousBackend._whole)
    _leverage = AutonomousBackend._leverage
    def __init__(self, cap=20, market=None, observed=None):
        self.allocation_policy = types.SimpleNamespace(max_leverage=cap)
        self.risk = types.SimpleNamespace(policy=types.SimpleNamespace(max_leverage=cap))
        self.ceilings = (lambda i: market) if market is not None else None
        self.leader_leverage = (lambda w, i: observed) if observed is not None else None

lev, src = Stub(cap=20, market=3)._leverage(inst('xyz', 'AAPL'), None, '0xL')
ok('a 3x stock is not sent 20x', (lev, src) == (3, 'POLICY_CLAMPED'), (lev, src))

lev, src = Stub(cap=20, market=50)._leverage(inst(), None, '0xL')
ok('the policy cap still wins over a higher venue ceiling', (lev, src) == (20, 'POLICY'), (lev, src))

lev, src = Stub(cap=20, market=40, observed=5)._leverage(inst(), None, '0xL')
ok('the leader running 5x is copied at 5x, not 20x', (lev, src) == (5, 'LEADER'), (lev, src))

lev, src = Stub(cap=20, market=3, observed=10)._leverage(inst('xyz', 'AAPL'), None, '0xL')
ok('an observed leverage above the venue ceiling is clamped',
   (lev, src) == (3, 'LEADER_CLAMPED'), (lev, src))

lev, src = Stub(cap=20)._leverage(inst(), None, '0xL')
ok('no resolver means the policy cap, flagged as unverified',
   (lev, src) == (20, 'POLICY_UNVERIFIED_MARKET'), (lev, src))

held = Position(instrument=inst(), side='LONG', size=1.0, entry_price=100.0, notional=100.0,
                margin=10.0, leverage=7, evidence='VERIFIED', order_ids=('o1',),
                contributions=(Contribution(source='AUTONOMOUS', notional=100.0),))
lev, src = Stub(cap=20, market=3)._leverage(inst(), held, '0xL')
ok('an open position keeps its own leverage and is never re-clamped',
   (lev, src) == (7, 'POSITION'), (lev, src))

class Exploding(Stub):
    def __init__(self):
        super().__init__(cap=20)
        def boom(*a): raise RuntimeError('venue metadata down')
        self.ceilings = boom
        self.leader_leverage = boom
lev, src = Exploding()._leverage(inst(), None, '0xL')
ok('a resolver failure falls back instead of stalling the pipeline',
   (lev, src) == (20, 'POLICY_UNVERIFIED_MARKET'), (lev, src))

for bad in (None, True, float('nan'), 0, -3, 'four'):
    stub = Stub(cap=20)
    stub.ceilings = lambda i, v=bad: v
    lev, src = stub._leverage(inst(), None, '0xL')
    ok('rubbish ceiling %r is ignored' % (bad,), (lev, src) == (20, 'POLICY_UNVERIFIED_MARKET'), (lev, src))


# --------------------------------------------------------- per-class liquidity
print('a stock is judged against a book it can actually have')
STOCKS = IntelligencePolicy(liquidity_overrides=(
    LiquidityThresholds(dex='xyz', max_spread_bps=60., minimum_depth_usd=1500.),))


def book(bid=100.0, ask=100.05, size=100.0, age=0):
    return {'time': NOW - age,
            'levels': [[{'px': str(bid - i * 0.01), 'sz': str(size)} for i in range(5)],
                       [{'px': str(ask + i * 0.01), 'sz': str(size)} for i in range(5)]]}


def candles(n=40, base=100.0, drift=0.0):
    out = []
    start = NOW - n * 900000
    for i in range(n):
        c = base + drift * i
        out.append({'T': start + i * 900000, 'c': str(c), 'h': str(c * 1.001), 'l': str(c * 0.999)})
    return out


def leader(score=0.8, qualified=True, side='BUY'):
    return types.SimpleNamespace(qualified=qualified, confidence=0.6, score=score,
                                 computed_ms=NOW)


def liquidity_of(policy, dex, symbol, depth_size, spread=0.05):
    e = event(dex, symbol)
    agents = evaluate(e, leader(), candles(), book(ask=100.0 + spread, size=depth_size),
                      NOW, policy)
    return {a.agent_id: a for a in agents}['liquidity']

thin = liquidity_of(IntelligencePolicy(), 'xyz', 'AAPL', depth_size=5.0)
ok('a stock fails the default BTC depth floor', thin.direction == 'CAUTION', thin)
allowed = liquidity_of(STOCKS, 'xyz', 'AAPL', depth_size=5.0)
ok('and passes its own', allowed.direction == 'PASS', allowed)
wide = liquidity_of(STOCKS, 'xyz', 'AAPL', depth_size=5.0, spread=5.0)
ok('a genuinely wide spread still BLOCKs', wide.direction == 'BLOCK', wide)
ok('the block names the spread', 'SPREAD_LIMIT' in wide.evidence, wide)


# ----------------------------------------------------------- depth is sizing now
print('thin depth costs size, a wide spread costs the trade')
def decide(policy, **kw):
    e = event(**{k: v for k, v in kw.items() if k in ('dex', 'symbol', 'side')})
    agents = list(evaluate(e, leader(), candles(drift=kw.get('drift', 0.2)),
                           book(ask=100.0 + kw.get('spread', 0.05), size=kw.get('depth', 5000.0)),
                           NOW, policy, flow=kw.get('flow')))
    agents = [a for a in agents if a.agent_id != 'risk_context']
    agents.append(AgentResult(agent_id='risk_context', instrument=e.instrument, created_ms=NOW,
                              direction='PASS', confidence=1.0, score=1.0,
                              evidence=('CONTEXT_CHECKS_PASS',), freshness='FRESH'))
    return consensus(e, tuple(agents), NOW, policy)

deep = decide(IntelligencePolicy(), depth=5000.0)
shallow = decide(IntelligencePolicy(), depth=2.0)
ok('a thin book still trades', shallow.decision == 'COPY_LONG', shallow)
ok('but smaller than a deep one', shallow.confidence < deep.confidence,
   (shallow.confidence, deep.confidence))
ok('and says why', 'DEPTH_CAUTION' in shallow.attenuation, shallow)
blocked = decide(IntelligencePolicy(), spread=5.0)
ok('a wide spread refuses the entry', blocked.decision == 'WAIT', blocked)
strict_thin = decide(IntelligencePolicy(consensus_mode='STRICT'), depth=2.0)
ok('STRICT still refuses a thin book outright', strict_thin.decision == 'WAIT', strict_thin)


# ------------------------------------------------------------------ order flow
print('order flow reads the tape instead of returning WAIT forever')
def flow_agent(policy, window):
    e = event()
    return {a.agent_id: a for a in evaluate(e, leader(), candles(), book(), NOW, policy,
                                            flow=window)}['order_flow']

none = flow_agent(IntelligencePolicy(), None)
ok('no window is still INSUFFICIENT_EVIDENCE, not a fabricated neutral vote',
   none.direction == 'WAIT' and none.confidence == 0.0 and none.evidence == ('INSUFFICIENT_EVIDENCE',), none)

good = {'prints': 300, 'notional': 500000.0, 'imbalance': 0.6, 'exchange_ms': NOW, 'window_ms': 300000}
a = flow_agent(IntelligencePolicy(), good)
ok('a one-sided tape reads LONG at full sample confidence',
   a.direction == 'LONG' and a.confidence == 1.0 and abs(a.score - 0.6) < 1e-9, a)

small = dict(good, notional=25000.0)
a = flow_agent(IntelligencePolicy(), small)
ok('a small tape speaks quietly', abs(a.confidence - 0.1) < 1e-9, a)

for bad, why in ((dict(good, prints=3), 'too few prints'),
                 (dict(good, exchange_ms=NOW - 10 ** 6), 'stale'),
                 (dict(good, imbalance=4.0), 'out of range'),
                 (dict(good, notional=float('inf')), 'nonfinite'),
                 ({'prints': 300}, 'incomplete')):
    a = flow_agent(IntelligencePolicy(), bad)
    ok('a %s window is discarded' % why, a.direction == 'WAIT' and a.confidence == 0.0, a)

with_flow = decide(IntelligencePolicy(), flow=good)
against = decide(IntelligencePolicy(), flow=dict(good, imbalance=-0.6))
ok('flow agreeing with the entry does not shrink it',
   'FLOW_DISAGREEMENT' not in with_flow.attenuation, with_flow)
ok('flow against the entry shrinks it', against.confidence < with_flow.confidence,
   (against.confidence, with_flow.confidence))
ok('and says why', 'FLOW_DISAGREEMENT' in against.attenuation, against)
ok('but never refuses on flow alone', against.decision == 'COPY_LONG', against)


# --------------------------------------------------------------- the flow window
print('the flow window is fed from trades we already receive')
from core.intelligence.service import WalletDiscoveryEngine
import tempfile, os
engine = WalletDiscoveryEngine(os.path.join(tempfile.mkdtemp(), 'r.sqlite'), 'MAINNET')
trades = ([{'coin': 'BTC', 'time': NOW - 1000, 'side': 'B', 'px': '100', 'sz': '10'}] * 30 +
          [{'coin': 'BTC', 'time': NOW - 1000, 'side': 'A', 'px': '100', 'sz': '5'}] * 10)
engine.record_flow(trades, NOW)
w = engine.flow_window('BTC', NOW)
ok('buy pressure reads positive', w['imbalance'] > 0, w)
ok('the sample is counted', w['prints'] == 40, w)
ok('notional is the gross, not the net', abs(w['notional'] - (30 * 1000 + 10 * 500)) < 1e-6, w)
ok('an unseen market has no window', engine.flow_window('DOGE', NOW) is None)

engine.record_flow([{'coin': 'ETH', 'time': NOW, 'side': 'B', 'px': 'x', 'sz': '1'},
                    {'coin': 'ETH', 'time': NOW, 'side': 'Z', 'px': '1', 'sz': '1'},
                    {'coin': 'ETH', 'time': NOW, 'side': 'B', 'px': '-1', 'sz': '1'},
                    {'coin': 'ETH'}], NOW)
ok('malformed prints are dropped without raising', engine.flow_window('ETH', NOW) is None)

engine.record_flow([{'coin': 'SOL', 'time': NOW - 10 ** 6, 'side': 'B', 'px': '1', 'sz': '1'}], NOW)
ok('prints older than the window never enter it', engine.flow_window('SOL', NOW) is None)

engine.record_flow([{'coin': 'OLD', 'time': NOW, 'side': 'B', 'px': '1', 'sz': '1'}], NOW)
ok('a window ages out on read', engine.flow_window('OLD', NOW + 10 ** 6) is None)

big = [{'coin': 'CAP', 'time': NOW - i, 'side': 'B', 'px': '1', 'sz': '1'} for i in range(5000)]
engine.record_flow(big, NOW)
ok('per-market memory is bounded',
   engine.flow_window('CAP', NOW)['prints'] == WalletDiscoveryEngine.FLOW_PER_MARKET,
   engine.flow_window('CAP', NOW))

print('ALL OK')
