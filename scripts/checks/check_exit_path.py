"""Why a proven position could not be closed, and why 37 jobs said "ValueError".

Measured on a live PAPER run: 7 positions opened, 0 ever closed, 0 settled
outcomes, 37 quarantined ADD jobs whose only recorded reason was the string
"ValueError". Three defects, one of them structural:

* Two non-closed episodes for one (mode, leader, instrument) made active_in()
  raise. The worker stored type(exc).__name__, so every one of those 37 records
  said "ValueError" and nothing else.
* When those duplicates were all REJECTED and unrepairable, active_in returned
  None, the exit was refused pre-consensus as FOLLOWER_OWNERSHIP_UNPROVEN, and
  a position we actually held could not be closed at all.
* Exits that DID reach consensus were refused by risk_context on staleness or
  WAIT - not on an actual BLOCK - which is stricter than _exit_gates' own
  docstring and trapped the operator in the position whenever the feed lagged.

These assertions pin the fixes AND the guards that must survive them.
"""
import sys, json, tempfile, os
sys.path.insert(0, '.')

from core.foundation.contracts import Contribution, InstrumentId, Position
from core.intelligence.models import AgentResult, IntelligencePolicy, LeaderTradeEvent
from core.intelligence.agents import consensus, _exit_gates
from core.autonomous import _fault

NOW = 1_700_000_000_000
INST = InstrumentId(network='MAINNET', dex='', symbol='BTC')
P = IntelligencePolicy()


def ok(label, cond, detail=''):
    if not cond: raise AssertionError(label + ' :: ' + str(detail))
    print('  ok  ' + label)


def agent(name, direction, score, confidence=1.0, freshness='FRESH'):
    return AgentResult(agent_id=name, instrument=INST, created_ms=NOW, direction=direction,
                       confidence=confidence, score=score, evidence=('TEST',), freshness=freshness)


def board(context=('PASS', 'FRESH'), book=('PASS', 'FRESH')):
    return (agent('structure', 'LONG', 1.0, .7), agent('momentum', 'LONG', 1.0),
            agent('volatility', 'PASS', .5, .8),
            agent('liquidity', book[0], 1.0, 1.0, book[1]),
            agent('order_flow', 'WAIT', 0.0, 0.0, 'UNKNOWN'),
            agent('leader', 'SHORT', -.9, .6),
            agent('risk_context', context[0], 1.0, 1.0, context[1]))


held = Position(instrument=INST, side='LONG', size=1.0, entry_price=100.0, notional=100.0,
                margin=10.0, leverage=10, evidence='VERIFIED', order_ids=('o1',),
                contributions=(Contribution(source='AUTONOMOUS', notional=100.0),))


def event(action='CLOSE', age_s=0):
    return LeaderTradeEvent(event_id='e1', wallet='0x' + 'a' * 40, instrument=INST,
                            action=action, side='SELL', size=1.0, before_size=1.0, after_size=0.0,
                            exchange_ms=NOW - age_s * 1000, received_ms=NOW, fill_id='f1')


print('an unknown financial context no longer traps us in a position')
by_id = {a.agent_id: a for a in board(context=('PASS', 'STALE'))}
ok('a stale risk context does not block an exit',
   'risk_context_UNAVAILABLE_OR_BLOCKING' not in _exit_gates(by_id), _exit_gates(by_id))
by_id = {a.agent_id: a for a in board(context=('WAIT', 'FRESH'))}
ok('a waiting risk context does not block an exit',
   'risk_context_UNAVAILABLE_OR_BLOCKING' not in _exit_gates(by_id), _exit_gates(by_id))
by_id = {a.agent_id: a for a in board() if a.agent_id != 'risk_context'}
ok('a missing risk context does not block an exit',
   'risk_context_UNAVAILABLE_OR_BLOCKING' not in _exit_gates(by_id), _exit_gates(by_id))

print('but a real financial failure still does')
by_id = {a.agent_id: a for a in board(context=('BLOCK', 'FRESH'))}
ok('BLOCK still refuses the exit',
   'risk_context_UNAVAILABLE_OR_BLOCKING' in _exit_gates(by_id), _exit_gates(by_id))

print('the book is an ability, not a caution: it keeps every check')
for book, why in ((('BLOCK', 'FRESH'), 'a blocking book'), (('WAIT', 'FRESH'), 'a waiting book'),
                  (('PASS', 'STALE'), 'a stale book')):
    by_id = {a.agent_id: a for a in board(book=book)}
    ok('%s still refuses the exit' % why,
       'liquidity_UNAVAILABLE_OR_BLOCKING' in _exit_gates(by_id), _exit_gates(by_id))
by_id = {a.agent_id: a for a in board() if a.agent_id != 'liquidity'}
ok('a missing book still refuses the exit',
   'liquidity_UNAVAILABLE_OR_BLOCKING' in _exit_gates(by_id))


print('a proven CLOSE is not refused for signal age')
for age in (60, 315, 900, 4000, 86400):
    d = consensus(event('CLOSE', age), board(), NOW, P, position=held)
    ok('a proven CLOSE at %6ds still closes' % age,
       d.decision == 'COPY_SHORT' and not d.blockers, (age, d.blockers))

print('and the exemption is exactly that narrow')
d = consensus(event('CLOSE', 4000), board(), NOW, P)
ok('an UNPROVEN close is still refused',
   'POSITION_LIFECYCLE_REQUIRED' in d.blockers, d.blockers)
d = consensus(event('REDUCE', 4000), board(), NOW, P, position=held)
ok('a stale REDUCE is still refused', 'STALE_SIGNAL' in d.blockers, d.blockers)
d = consensus(event('REDUCE', 300), board(), NOW, P, position=held)
ok('a fresh REDUCE still passes', not d.blockers, d.blockers)
entry = LeaderTradeEvent(event_id='e2', wallet='0x' + 'a' * 40, instrument=INST, action='OPEN',
                         side='BUY', size=1.0, before_size=0.0, after_size=1.0,
                         exchange_ms=NOW - 4000 * 1000, received_ms=NOW, fill_id='f2')
d = consensus(entry, board(), NOW, P)
ok('a stale OPEN is still refused', 'STALE_SIGNAL' in d.blockers, d.blockers)
future = consensus(event('CLOSE', -600), board(), NOW, P, position=held)
ok('a CLOSE timestamped in the future is accepted only because it is proven',
   future.decision == 'COPY_SHORT', future.blockers)


print('a failure names itself instead of saying only "ValueError"')
ok('the message survives', _fault(ValueError('Ambiguous position episode: 2 candidates'))
   == 'ValueError: Ambiguous position episode: 2 candidates')
ok('a bare exception still reports its class', _fault(ValueError()) == 'ValueError')
ok('newlines are folded', _fault(RuntimeError('a\nb  c')) == 'RuntimeError: a b c')
ok('the record is bounded', len(_fault(ValueError('x' * 4000))) <= 200)



print()
print('duplicate episodes are resolved by order evidence, not by raising')
import sqlite3, uuid
from core.foundation.store import Store, scope_key
from core.foundation.contracts import Scope, PortfolioSnapshot
from core.position_episodes import EpisodeService, PositionEpisode

SCOPE = Scope(tenant='1', account='0x' + 'ab' * 20, network='MAINNET')
LEADER = '0x' + 'cd' * 20


def build(portfolio_order_ids, episodes):
    """A store holding one VERIFIED BTC position and the given episodes."""
    store = Store(os.path.join(tempfile.mkdtemp(), 's.sqlite'))
    service = EpisodeService(store)
    with store.transaction() as db:
        if portfolio_order_ids is not None:
            store.publish_portfolio_in(db, PortfolioSnapshot(
                scope=SCOPE, revision=1, exchange_ms=NOW, received_ms=NOW, equity=100.,
                sizing_capital=100., available_collateral=90., completeness='COMPLETE',
                evidence='FAKE', positions=(Position(
                    instrument=INST, side='LONG', size=1., entry_price=100., notional=100.,
                    margin=10., leverage=10, evidence='VERIFIED',
                    order_ids=tuple(portfolio_order_ids),
                    contributions=(Contribution(source='autonomous', notional=100.),)),)), 'seed')
        for name, state, filled in episodes:
            episode = PositionEpisode(episode_id=name, scope=SCOPE, mode='PAPER_AUTO',
                                      instrument=INST, leader=LEADER, state=state,
                                      created_ms=NOW, first_event_id='ev-' + name)
            db.execute('INSERT INTO position_episodes VALUES(?,?,?)',
                       (name, scope_key(SCOPE), episode.model_dump_json()))
            for order_id in filled:
                intent_id = 'i-' + name + '-' + order_id
                db.execute('INSERT INTO episode_actions VALUES(?,?)', (intent_id, name))
                db.execute('INSERT INTO intents(id,scope,body,status,receipt) VALUES(?,?,?,?,?)',
                           (intent_id, scope_key(SCOPE), '{}', 'FILLED',
                            json.dumps({'order_ids': [order_id]})))
    return store, service


def resolve(portfolio_order_ids, episodes):
    store, service = build(portfolio_order_ids, episodes)
    with store.transaction() as db:
        return service.active_in(db, SCOPE, 'PAPER_AUTO', LEADER, INST)


got = resolve(['oA'], [('epA', 'OPEN', ['oA']), ('epB', 'OPEN', ['oB'])])
ok('the episode whose fill is in the position wins', got.episode_id == 'epA', got)

got = resolve(['oB'], [('epA', 'REJECTED', ['oA']), ('epB', 'OPEN', ['oB'])])
ok('order evidence wins even when the other is REJECTED', got.episode_id == 'epB', got)

got = resolve(['oZ'], [('epA', 'REJECTED', []), ('epB', 'OPEN', [])])
ok('with no order evidence the single live episode is used', got.episode_id == 'epB', got)

print('and it still refuses when the evidence cannot single one out')
for portfolio_ids, episodes, why in (
        (['oA', 'oB'], [('epA', 'OPEN', ['oA']), ('epB', 'OPEN', ['oB'])], 'two owners'),
        (['oZ'], [('epA', 'OPEN', []), ('epB', 'OPEN', [])], 'two live, no evidence')):
    try:
        resolve(portfolio_ids, episodes)
        ok('%s is refused' % why, False, 'it returned instead of raising')
    except ValueError as exc:
        ok('%s is refused' % why, 'Ambiguous' in str(exc), exc)
        ok('  and the refusal says why', 'candidates' in str(exc), exc)

print('a single episode is unaffected')
got = resolve(['oA'], [('epA', 'OPEN', ['oA'])])
ok('one live episode resolves', got.episode_id == 'epA', got)
got = resolve(['oZ'], [('epA', 'REJECTED', ['oA'])])
ok('a lone REJECTED episode with no matching fill stays unowned', got is None, got)
got = resolve(['oA'], [('epA', 'REJECTED', ['oA'])])
ok('a lone REJECTED episode WITH a matching fill is recovered',
   got is not None and got.state == 'RECONCILIATION_REQUIRED', got)

print('ALL OK')
