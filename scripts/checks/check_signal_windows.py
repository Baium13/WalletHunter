"""Exit windows and research-queue throughput (patch 0007).

The defect these cover: every CLOSE in a 24h sample was refused as stale
(median age 315s) and 12 of 20 REDUCE (median 766s), because one 60-second
window judged exits and entries alike. Positions opened and none ever closed.
"""
import sys, time, types, tempfile, os
sys.path.insert(0, '.')

from core.foundation.contracts import Contribution, InstrumentId, Position
from core.intelligence.models import AgentResult, IntelligencePolicy, LeaderTradeEvent
from core.intelligence.agents import consensus
from core.intelligence.service import WalletDiscoveryEngine

NOW = 1_700_000_000_000
INST = InstrumentId(network='MAINNET', dex='', symbol='BTC')
P = IntelligencePolicy()
SECOND = 1000


def ok(label, cond, detail=''):
    if not cond: raise AssertionError(label + ' :: ' + str(detail))
    print('  ok  ' + label)


def event(action='OPEN', side='BUY', age_s=0):
    return LeaderTradeEvent(event_id='e1', wallet='0x' + 'a' * 40, instrument=INST,
                            action=action, side=side, size=1.0, before_size=0.0, after_size=1.0,
                            exchange_ms=NOW - age_s * SECOND, received_ms=NOW, fill_id='f1')


def agent(name, direction, score, confidence=1.0, freshness='FRESH'):
    return AgentResult(agent_id=name, instrument=INST, created_ms=NOW, direction=direction,
                       confidence=confidence, score=score, evidence=('TEST',), freshness=freshness)


def board(side='BUY'):
    signed = 0.9 * (1 if side == 'BUY' else -1)
    return (agent('structure', 'LONG', 1.0, 0.7), agent('momentum', 'LONG', 1.0, 1.0),
            agent('volatility', 'PASS', 0.5, 0.8), agent('liquidity', 'PASS', 1.0),
            agent('order_flow', 'WAIT', 0.0, 0.0, 'UNKNOWN'),
            agent('leader', 'LONG' if side == 'BUY' else 'SHORT', signed, 0.6),
            agent('risk_context', 'PASS', 1.0))


held = Position(instrument=INST, side='LONG', size=1.0, entry_price=100.0, notional=100.0,
                margin=10.0, leverage=10, evidence='VERIFIED', order_ids=('o1',),
                contributions=(Contribution(source='AUTONOMOUS', notional=100.0),))


print('the window depends on the action, not just the clock')
ok('an entry keeps the 60s window', P.signal_window_ms('OPEN') == 60000, P.signal_window_ms('OPEN'))
ok('ADD is an entry too', P.signal_window_ms('ADD') == 60000)
for action in ('REDUCE', 'CLOSE'):
    ok('%s gets the exit window' % action, P.signal_window_ms(action) == P.max_exit_signal_age_ms)
ok('the exit window is the wider of the two', P.max_exit_signal_age_ms > P.max_signal_age_ms)


print('a late exit can now close a position it could not before')
# The measured reality: CLOSE at 315s, REDUCE at 766s. Both were refused.
for action, age in (('CLOSE', 315), ('REDUCE', 766)):
    d = consensus(event(action=action, side='SELL', age_s=age), board('SELL'), NOW, P, position=held)
    ok('%s at %ds closes' % (action, age), d.decision == 'COPY_SHORT' and not d.blockers, d)

single = IntelligencePolicy(max_exit_signal_age_ms=P.max_signal_age_ms)
d = consensus(event(action='CLOSE', side='SELL', age_s=315), board('SELL'), NOW, single, position=held)
ok('setting the windows equal restores the old refusal',
   d.decision == 'WAIT' and 'STALE_SIGNAL' in d.blockers, d)


print('the wider window does not leak into entries or unowned exits')
for action, side in (('OPEN', 'BUY'), ('ADD', 'BUY')):
    d = consensus(event(action=action, side=side, age_s=315), board(side), NOW, P)
    ok('a %s at 315s is still refused' % action,
       d.decision == 'WAIT' and 'STALE_SIGNAL' in d.blockers, d)

d = consensus(event(action='CLOSE', side='SELL', age_s=315), board('SELL'), NOW, P)
ok('an UNPROVEN exit is still refused at 315s',
   d.decision == 'WAIT' and 'POSITION_LIFECYCLE_REQUIRED' in d.blockers, d)

beyond = P.max_exit_signal_age_ms // 1000 + 60
d = consensus(event(action='CLOSE', side='SELL', age_s=beyond), board('SELL'), NOW, P, position=held)
ok('an exit past its own window is still refused',
   d.decision == 'WAIT' and 'STALE_SIGNAL' in d.blockers, d)

future = event(action='CLOSE', side='SELL', age_s=-120)
d = consensus(future, board('SELL'), NOW, P, position=held)
ok('an exit timestamped in the future is refused',
   d.decision == 'WAIT' and 'STALE_SIGNAL' in d.blockers, d)


print('the queue scales with its own depth')
engine = WalletDiscoveryEngine(os.path.join(tempfile.mkdtemp(), 'r.sqlite'), 'MAINNET')
base, ceiling = P.research_per_cycle, P.research_max_per_cycle
for depth, want in ((0, base), (1, base), (base, base), (base + 1, base + 1),
                    (ceiling, ceiling), (ceiling + 50, ceiling), (10 ** 6, ceiling)):
    ok('depth %-7d -> %d slots' % (depth, want), engine.research_slots(depth) == want,
       engine.research_slots(depth))

engine.policy = IntelligencePolicy(research_per_cycle=8, research_max_per_cycle=4)
ok('a ceiling below the base never lowers the base', engine.research_slots(100) == 8,
   engine.research_slots(100))
engine.policy = P


print('an expired event costs no market read')
class Reader:
    network = 'MAINNET'
    def __init__(self): self.calls = []
    def _info(self, payload):
        self.calls.append(payload.get('type'))
        raise AssertionError('an expired event must not reach the market')

leader = types.SimpleNamespace()
from core.intelligence.models import LeaderScore
score = LeaderScore(wallet='0x' + 'a' * 40, network='MAINNET', policy_id='intelligence-v1',
                    computed_ms=NOW, score=0.9, confidence=0.6, profit_quality=0.5,
                    consistency=0.5, drawdown_quality=0.5, sample_quality=0.5,
                    recent_quality=0.5, anomaly=0.0, qualified=True, reasons=())

reader = Reader()
stale_entry = event(action='OPEN', age_s=600)
ok('the engine calls it expired', engine.expired(stale_entry, NOW))
decision = engine.research(stale_entry, score, reader._info, NOW)
ok('no market request was made', reader.calls == [], reader.calls)
ok('and it is refused as stale', 'STALE_SIGNAL' in decision.blockers, decision.blockers)

recent_exit = event(action='CLOSE', side='SELL', age_s=300)
ok('a 300s exit is NOT expired', not engine.expired(recent_exit, NOW))
ok('a 300s entry IS expired', engine.expired(event(action='OPEN', age_s=300), NOW))

print('ALL OK')
