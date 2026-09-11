"""Safety invariants that must hold after moving agents from veto to sizing.

Every case here is something that must STILL refuse, or must still be allowed
to close. If any of these ever passes when it should not, the loosening went
too far.
"""
import sys, types
sys.path.insert(0, '.')

from core.foundation.contracts import Contribution, InstrumentId, Position
from core.intelligence.models import AgentResult, IntelligencePolicy, LeaderTradeEvent
from core.intelligence.agents import consensus

NOW = 1_700_000_000_000
INST = InstrumentId(network='MAINNET', dex='', symbol='BTC')
STRICT = IntelligencePolicy(consensus_mode='STRICT')
WEIGHTED = IntelligencePolicy(consensus_mode='WEIGHTED')


def ok(label, cond, detail=''):
    if not cond: raise AssertionError(label + ' :: ' + str(detail))
    print('  ok  ' + label)


def event(action='OPEN', side='BUY', age=0):
    return LeaderTradeEvent(event_id='e1', wallet='0x' + 'a' * 40, instrument=INST,
                            action=action, side=side, size=1.0, before_size=0.0, after_size=1.0,
                            exchange_ms=NOW - age, received_ms=NOW - age, fill_id='f1')


def agent(name, direction, score, confidence=1.0, freshness='FRESH', created=None, instrument=None):
    return AgentResult(agent_id=name, instrument=instrument or INST,
                       created_ms=NOW if created is None else created, direction=direction,
                       confidence=confidence, score=score, evidence=('TEST',), freshness=freshness)


def board(trend=1.0, leader_score=0.9, qualified=True, volatility='PASS', liquidity='PASS',
          context='PASS', side='BUY', flow=None, drop=()):
    lead_dir = ('LONG' if side == 'BUY' else 'SHORT') if qualified else 'CAUTION'
    signed = leader_score * (1 if side == 'BUY' else -1)
    rows = {
        'structure': agent('structure', 'LONG' if trend > 0 else 'SHORT' if trend < 0 else 'WAIT', trend, 0.7),
        'momentum': agent('momentum', 'LONG' if trend > 0 else 'SHORT', trend, abs(trend) or 0.01),
        'volatility': agent('volatility', volatility, 0.5 if volatility == 'PASS' else -1.0, 0.8),
        'liquidity': agent('liquidity', liquidity, 1.0 if liquidity == 'PASS' else -1.0, 1.0),
        'order_flow': (agent('order_flow', 'LONG' if flow > 0 else 'SHORT', flow, 1.0)
                       if flow is not None else agent('order_flow', 'WAIT', 0.0, 0.0, freshness='UNKNOWN')),
        'leader': agent('leader', lead_dir, signed, 0.6),
        'risk_context': agent('risk_context', context, 1.0 if context == 'PASS' else -1.0, 1.0),
    }
    for name in drop: rows.pop(name, None)
    return tuple(rows.values())


held = Position(instrument=INST, side='LONG', size=1.0, entry_price=100.0, notional=100.0,
                margin=10.0, leverage=10, evidence='VERIFIED', order_ids=('o1',),
                contributions=(Contribution(source='AUTONOMOUS', notional=100.0),))

print('these must never produce an entry, in either mode')
CASES = [
    ('an unrated leader', dict(qualified=False)),
    ('a wide spread (liquidity BLOCK)', dict(liquidity='BLOCK')),
    ('no book at all', dict(liquidity='WAIT')),
    ('no candles at all', dict(volatility='WAIT')),
    ('a BLOCKing financial context', dict(context='BLOCK')),
    ('a missing leader agent', dict(drop=('leader',))),
    ('a missing risk_context agent', dict(drop=('risk_context',))),
    ('a missing liquidity agent', dict(drop=('liquidity',))),
    ('a missing volatility agent', dict(drop=('volatility',))),
]
for label, kw in CASES:
    for name, policy in (('STRICT', STRICT), ('WEIGHTED', WEIGHTED)):
        d = consensus(event(), board(**kw), NOW, policy)
        ok('%s refuses %s' % (name, label), d.decision == 'WAIT' and bool(d.blockers), d)

for label, kw in [('a stale leader signal', dict(age=10 ** 6))]:
    for name, policy in (('STRICT', STRICT), ('WEIGHTED', WEIGHTED)):
        d = consensus(event(**kw), board(), NOW, policy)
        ok('%s refuses %s' % (name, label), d.decision == 'WAIT' and 'STALE_SIGNAL' in d.blockers, d)

stale_agents = tuple(agent(a.agent_id, a.direction, a.score, a.confidence, a.freshness,
                           created=NOW - 10 ** 6) for a in board())
for name, policy in (('STRICT', STRICT), ('WEIGHTED', WEIGHTED)):
    d = consensus(event(), stale_agents, NOW, policy)
    ok('%s refuses out-of-window agents' % name, d.decision == 'WAIT' and 'AGENT_SCOPE_OR_AGE' in d.blockers, d)

other = InstrumentId(network='MAINNET', dex='', symbol='ETH')
mixed = board()[:-1] + (agent('risk_context', 'PASS', 1.0, instrument=other),)
for name, policy in (('STRICT', STRICT), ('WEIGHTED', WEIGHTED)):
    d = consensus(event(), mixed, NOW, policy)
    ok('%s refuses an agent scored on another market' % name,
       d.decision == 'WAIT' and 'AGENT_SCOPE_OR_AGE' in d.blockers, d)

dupe = board() + (agent('leader', 'LONG', 0.9, 0.6),)
for name, policy in (('STRICT', STRICT), ('WEIGHTED', WEIGHTED)):
    d = consensus(event(), dupe, NOW, policy)
    ok('%s refuses a duplicated agent' % name, d.decision == 'WAIT' and 'DUPLICATE_AGENT' in d.blockers, d)

for action in ('REDUCE', 'CLOSE', 'REVERSE'):
    for name, policy in (('STRICT', STRICT), ('WEIGHTED', WEIGHTED)):
        d = consensus(event(action=action, side='SELL'), board(side='SELL'), NOW, policy)
        ok('%s refuses %s with no proven position' % (name, action),
           d.decision == 'WAIT' and 'POSITION_LIFECYCLE_REQUIRED' in d.blockers, d)

unverified = Position(instrument=INST, side='LONG', size=1.0, entry_price=100.0, notional=100.0,
                      margin=10.0, leverage=10, evidence='UNKNOWN')
for name, policy in (('STRICT', STRICT), ('WEIGHTED', WEIGHTED)):
    d = consensus(event(action='CLOSE', side='SELL'), board(side='SELL'), NOW, policy, position=unverified)
    ok('%s refuses to reduce an unproven position' % name,
       d.decision == 'WAIT' and 'REDUCTION_OWNERSHIP_OR_SIDE' in d.blockers, d)
    d = consensus(event(action='CLOSE', side='BUY'), board(side='BUY'), NOW, policy, position=held)
    ok('%s refuses a reduction on the wrong side' % name,
       d.decision == 'WAIT' and 'REDUCTION_OWNERSHIP_OR_SIDE' in d.blockers, d)

print('an exit must not be blocked by an opinion about the market')
for label, kw in [('abnormal volatility', dict(volatility='CAUTION')),
                  ('a leader demoted since we entered', dict(qualified=False)),
                  ('a thin book', dict(liquidity='CAUTION')),
                  ('a cautionary context', dict(context='CAUTION')),
                  ('a trend that disagrees', dict(trend=1.0))]:
    d = consensus(event(action='CLOSE', side='SELL'), board(side='SELL', **kw), NOW, WEIGHTED, position=held)
    ok('WEIGHTED closes despite %s' % label, d.decision == 'COPY_SHORT' and not d.blockers, d)

print('an exit must still be blocked by an inability to place it')
for label, kw in [('no usable book', dict(liquidity='WAIT')),
                  ('a BLOCKing financial context', dict(context='BLOCK'))]:
    d = consensus(event(action='CLOSE', side='SELL'), board(side='SELL', **kw), NOW, WEIGHTED, position=held)
    ok('WEIGHTED still refuses to close with %s' % label, d.decision == 'WAIT' and bool(d.blockers), d)

print('attenuation is advisory and can never reject an order')
seen = set()
for kw in (dict(trend=-1.0), dict(trend=0.0), dict(volatility='CAUTION'), dict(liquidity='CAUTION'),
           dict(context='CAUTION'), dict(flow=-0.8), dict(flow=0.2)):
    d = consensus(event(), board(**kw), NOW, WEIGHTED)
    seen |= set(d.attenuation)
    ok('%r attenuates without blocking' % (kw,), not (set(d.attenuation) & set(d.blockers)), d)
ok('every attenuation reason is reachable',
   seen == {'TREND_DISAGREEMENT', 'TREND_PARTIAL', 'VOLATILITY_CAUTION', 'DEPTH_CAUTION',
            'CONTEXT_CAUTION', 'FLOW_DISAGREEMENT', 'FLOW_PARTIAL'}, seen)

print('conviction only ever goes down')
full = consensus(event(), board(trend=1.0, flow=1.0), NOW, WEIGHTED)
ok('a perfectly aligned entry is capped at the leader quality',
   abs(full.confidence - 0.9) < 1e-9, full)
for kw in (dict(trend=0.5), dict(flow=0.5), dict(volatility='CAUTION'), dict(liquidity='CAUTION'),
           dict(context='CAUTION')):
    d = consensus(event(), board(**{'trend': 1.0, 'flow': 1.0, **kw}), NOW, WEIGHTED)
    ok('%r cannot raise conviction above it' % (kw,), d.confidence <= full.confidence + 1e-12,
       (d.confidence, full.confidence))
ok('confidence is a valid Unit in every case above', 0.0 <= full.confidence <= 1.0)

print('ALL OK')
