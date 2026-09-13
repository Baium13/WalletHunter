"""LIVE_AUTO end to end, offline: the mode that could never have placed an order.

Measured on the deployed tree before this patch::

    PositionEpisode(mode='PAPER_AUTO')   OK
    PositionEpisode(mode='LIVE_CONFIRM') OK
    PositionEpisode(mode='LIVE_AUTO')    ValidationError

LIVE_AUTO was added to the authorization policy and to the live guard, and not
to the episode contract or to the outcome mode map. Every unattended live entry
would therefore have raised inside the decision transaction, rolled back and
quarantined the job. It failed closed - no order was ever signed - but the mode
was unusable, and nothing in the tree said so.

This is also the first check that drives the money path end to end: a real
AutonomousBackend, holding a real HyperliquidExecutionAdapter, over a fake
signing client that answers from a dict. No network, no credentials, no key,
and no order ever leaves the process.
"""
import sys, json, math, tempfile, types, time
sys.path.insert(0, '.')

from pathlib import Path
from typing import get_args

from core.foundation.contracts import InstrumentId, PortfolioSnapshot
from core.foundation.authorization import AuthorizationPolicy
from core.foundation.store import scope_key
from core.position_episodes import PositionEpisode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _offline_exchange import NOW, SCOPE, INST, WALLET, live_backend, record


def ok(label, cond, detail=''):
    if not cond: raise AssertionError(label + ' :: ' + str(detail))
    print('  ok  ' + label)


# ------------------------------------------------- the contract that broke
print('every authorization mode is a mode an episode can be recorded in')
policy_modes = set(get_args(AuthorizationPolicy.model_fields['mode'].annotation))
episode_modes = set(get_args(PositionEpisode.model_fields['mode'].annotation))
ok('no authorization mode is missing from PositionEpisode',
   not policy_modes - episode_modes, sorted(policy_modes - episode_modes))
for mode in sorted(policy_modes):
    PositionEpisode(episode_id='e', scope=SCOPE, mode=mode, leader=WALLET, instrument=INST,
                    first_event_id='f', created_ms=NOW, state='PROPOSED')
    print('  ok  PositionEpisode accepts %s' % mode)

print('and every one of them has an outcome channel')
import core.autonomous_outcomes as outcomes_module
source = Path('core/autonomous_outcomes.py').read_text()
ok('LIVE_AUTO settles on the LIVE channel', "'LIVE_AUTO':'LIVE'" in source)
ok('a live episode never carries a simulated fee',
   "episode.mode not in ('LIVE_CONFIRM','LIVE_AUTO')" in source)


print()
print('a LIVE_AUTO backend reaches a signed, reconciled entry without raising')
with tempfile.TemporaryDirectory() as tmp:
    backend, client, clock_box, store = live_backend(tmp=tmp)
    result = backend.process(record())
    ok('the decision is not an exception and not a quarantine',
       result.get('status') not in {None, 'QUARANTINED'}, result.get('status'))
    ok('the mode on the decision is LIVE_AUTO', result.get('mode') == 'LIVE_AUTO', result.get('mode'))
    print('      status=%s reason=%s' % (result.get('status'), result.get('reason')))
    if result.get('status') == 'FILLED':
        ok('exactly one order was signed', len(client.submitted) == 1, client.submitted)
        ok('an episode exists and it is a LIVE_AUTO episode',
           result.get('episode', {}).get('mode') == 'LIVE_AUTO', result.get('episode'))
        intent = result.get('receipt', {})
        ok('the receipt is exchange evidence, not simulated',
           intent.get('provenance') == 'EXCHANGE', intent.get('provenance'))

    print()
    print('  and the leader closing it settles a LIVE outcome, with no invented fee')
    clock_box['now'] += 5000
    later = clock_box['now']
    closed = backend.process(record(action='CLOSE', side='SELL', before_size=1.0, after_size=0.0,
                                    event_id='e1-close', now=later))
    print('      status=%s reason=%s episode=%s' % (closed.get('status'), closed.get('reason'),
                                                    (closed.get('episode') or {}).get('state')))
    ok('the close is filled', closed.get('status') == 'FILLED', closed.get('status'))
    ok('the episode is CLOSED', (closed.get('episode') or {}).get('state') == 'CLOSED', closed.get('episode'))
    with store.transaction() as db:
        row = db.execute('SELECT mode,body FROM autonomous_outcomes WHERE scope=?',
                         (scope_key(SCOPE),)).fetchone()
    ok('an outcome was recorded at all', row is not None)
    outcome = json.loads(row['body'])
    ok('it settles on the LIVE channel', row['mode'] == 'LIVE', row['mode'])
    ok('it keeps the operating mode it was traded in',
       outcome.get('operating_mode') == 'LIVE_AUTO', outcome.get('operating_mode'))
    ok('its evidence is the exchange', outcome.get('evidence') == 'EXCHANGE', outcome.get('evidence'))
    ok('no simulated fee is presented as a live cost', outcome.get('fees') is None, outcome.get('fees'))
    ok('and therefore no net pnl is claimed either',
       outcome.get('net_pnl') is None, outcome.get('net_pnl'))
    ok('the gross pnl is still measured', outcome.get('gross_pnl') is not None, outcome.get('gross_pnl'))

print()
print('proven ownership survives an account refresh - and only where it is still true')
from core.autonomous import carried_attribution
from core.foundation.contracts import Contribution, Position


def snap(positions, revision=1, evidence='EXCHANGE'):
    return PortfolioSnapshot(scope=SCOPE, revision=revision, exchange_ms=NOW, received_ms=NOW,
                             equity=1000.0, sizing_capital=1000.0, available_collateral=1000.0,
                             collateral_dex='', completeness='COMPLETE', evidence=evidence,
                             positions=tuple(positions))


def owned(size=2.0, side='LONG', notional=200.0):
    return Position(instrument=INST, side=side, size=size, entry_price=notional / size, notional=notional,
                    margin=notional / 5, leverage=5, evidence='VERIFIED', order_ids=('42',),
                    contributions=(Contribution(source='intelligence', notional=notional),))


def unknown(size=2.0, side='LONG', notional=200.0):
    return Position(instrument=INST, side=side, size=size, entry_price=notional / size, notional=notional,
                    margin=notional / 5, leverage=5, evidence='UNKNOWN')


carried = carried_attribution(snap([unknown(notional=210.0)], 2), snap([owned()], 1))
ok('the same position, still on the exchange, keeps its proof',
   carried.positions[0].evidence == 'VERIFIED', carried.positions[0].evidence)
ok('and its order ids', carried.positions[0].order_ids == ('42',), carried.positions[0].order_ids)
ok('with the attribution rescaled to what the position is worth now',
   math.isclose(carried.positions[0].contributions[0].notional, 210.0),
   carried.positions[0].contributions)

for why, fresh in (('a size that changed', unknown(size=3.0, notional=300.0)),
                   ('a side that flipped', unknown(side='SHORT'))):
    result = carried_attribution(snap([fresh], 2), snap([owned()], 1))
    ok('%s is not our proven position any more' % why,
       result.positions[0].evidence == 'UNKNOWN', result.positions[0].evidence)

result = carried_attribution(snap([unknown()], 2), snap([], 1))
ok('nothing proven means nothing carried', result.positions[0].evidence == 'UNKNOWN')
other = InstrumentId(network='MAINNET', dex='', symbol='ETH')
result = carried_attribution(snap([unknown().model_copy(update={'instrument': other})], 2), snap([owned()], 1))
ok('a different instrument is never attributed from another one',
   result.positions[0].evidence == 'UNKNOWN', result.positions[0].evidence)

print()
print('the live guard still stops an unattended entry dead')
with tempfile.TemporaryDirectory() as tmp:
    backend, client, clock_box, store = live_backend(halted=True, tmp=tmp)
    result = backend.process(record(event_id='e2'))
    ok('a halted guard blocks the entry', result.get('status') == 'LIVE_GUARD_BLOCKED', result.get('status'))
    ok('and nothing was signed', client.submitted == [], client.submitted)

print()
print('recovery of an unsent live action abandons it instead of stalling forever')
source = Path('core/autonomous.py').read_text()
ok('LIVE_AUTO has its own recovery branch', "elif self.auth_policy.mode=='LIVE_AUTO':" in source)
ok('it never re-signs on the recovery path',
   'ABANDONED_BEFORE_SUBMISSION' in source and
   source.index("elif self.auth_policy.mode=='LIVE_AUTO':") < source.index('ABANDONED_BEFORE_SUBMISSION'))

print()
print('all live_auto checks passed')
