"""An exit that does not need the leader to act, and cannot do anything else.

Before this there was no stop loss, no maximum hold and no liquidation-distance
check anywhere in the tree. The only way out of a position was a leader CLOSE
arriving on a feed whose stall looks exactly like "healthy and idle", so a
position opened at 03:00 on a feed that died at 03:01 stayed open.

The authority this adds is deliberately the narrowest one that can exist: a
grant with no consensus behind it, usable only to reduce or close, only against
a position this account has already proven it owns. Most of what follows checks
that it refuses everything else, because an authority that can open a position
without consensus would be worth more than the stop is.
"""
import sys, json, math, tempfile, types
sys.path.insert(0, '.')

from pathlib import Path

from core.foundation.contracts import (Contribution, InstrumentId, MarketSnapshot, OrderIntent,
                                       Position, PortfolioSnapshot, Scope)
from core.foundation.store import scope_key
from core.intelligence.models import IntelligencePolicy, LeaderScore, LeaderTradeEvent
from core.position_protection import PositionProtectionPolicy, protection_reasons
from core.autonomous import load_paper_backend

NOW = 1_700_000_000_000
ACCOUNT = '0x' + 'c' * 40
WALLET = '0x' + 'a' * 40
SCOPE = Scope(tenant='t', account=ACCOUNT, network='MAINNET')
INST = InstrumentId(network='MAINNET', dex='', symbol='BTC')


def ok(label, cond, detail=''):
    if not cond: raise AssertionError(label + ' :: ' + str(detail))
    print('  ok  ' + label)


def refuses(label, call, expect):
    try:
        call()
    except Exception as exc:
        ok(label, expect.lower() in str(exc).lower(), '%s: %s' % (type(exc).__name__, exc))
        return
    raise AssertionError(label + ' :: nothing was raised')


def position(side='LONG', size=2.0, liquidation=None, evidence='VERIFIED', source='intelligence'):
    return Position(instrument=INST, side=side, size=size, entry_price=100.0, notional=size*100.0,
                    margin=size*20.0, leverage=5, liquidation_price=liquidation, evidence=evidence,
                    order_ids=('42',) if evidence == 'VERIFIED' else (),
                    contributions=(Contribution(source=source, notional=size*100.0),) if evidence == 'VERIFIED' else ())


# ------------------------------------------------------------ the thresholds
print('a policy with no threshold is not a stop')
ok('and is refused rather than kept as one that never fires',
   not PositionProtectionPolicy(scope=SCOPE).enabled)
ok('a policy with a hold limit is a stop',
   PositionProtectionPolicy(scope=SCOPE, max_hold_ms=1000).enabled)

print()
print('nothing fires while both thresholds are off')
off = PositionProtectionPolicy(scope=SCOPE)
ok('a position held for a year is still not closed by an unset policy',
   protection_reasons(position(), 100.0, NOW - 365*24*3600*1000, NOW, off) == ())

print()
print('the hold limit measures the hold, and nothing else')
hold = PositionProtectionPolicy(scope=SCOPE, max_hold_ms=3600_000)
ok('one minute short of the limit is not yet due',
   protection_reasons(position(), 100.0, NOW - 3599_000, NOW, hold) == ())
ok('the limit itself is due',
   protection_reasons(position(), 100.0, NOW - 3600_000, NOW, hold) == ('MAX_HOLD_EXCEEDED',))
ok('and it does not need a mark to say so',
   protection_reasons(position(), None, NOW - 7200_000, NOW, hold) == ('MAX_HOLD_EXCEEDED',))

print()
print('the liquidation stop reads the exchange price, and refuses to guess')
near = PositionProtectionPolicy(scope=SCOPE, liquidation_buffer_pct=5.0)
ok('a long ten percent above its liquidation price is left alone',
   protection_reasons(position(liquidation=90.0), 100.0, NOW, NOW, near) == ())
ok('a long four percent above it is closed',
   protection_reasons(position(liquidation=96.0), 100.0, NOW, NOW, near) == ('LIQUIDATION_PROXIMITY',))
ok('a short four percent below it is closed',
   protection_reasons(position(side='SHORT', liquidation=104.0), 100.0, NOW, NOW, near) == ('LIQUIDATION_PROXIMITY',))
ok('a long already past its liquidation price is closed, not measured as far away',
   protection_reasons(position(liquidation=120.0), 100.0, NOW, NOW, near) == ('LIQUIDATION_PROXIMITY',))
ok('a short already past its liquidation price is closed too',
   protection_reasons(position(side='SHORT', liquidation=80.0), 100.0, NOW, NOW, near) == ('LIQUIDATION_PROXIMITY',))
ok('no liquidation price means this stop cannot speak, not that we are safe',
   protection_reasons(position(liquidation=None), 100.0, NOW, NOW, near) == ())
ok('no mark means the same',
   protection_reasons(position(liquidation=99.0), None, NOW, NOW, near) == ())
for bad in (0.0, -1.0, float('nan'), True, '100'):
    ok('a mark of %r is not a price' % (bad,),
       protection_reasons(position(liquidation=99.0), bad, NOW, NOW, near) == ())

print()
print('both stops can fire at once, and both are named')
both = PositionProtectionPolicy(scope=SCOPE, max_hold_ms=1000, liquidation_buffer_pct=5.0)
ok('the record says why twice',
   set(protection_reasons(position(liquidation=99.0), 100.0, NOW - 5000, NOW, both))
   == {'MAX_HOLD_EXCEEDED', 'LIQUIDATION_PROXIMITY'})


# --------------------------------------------------------------- the backend
def config(directory, *, protection=None, mode='PAPER_AUTO'):
    body = {
        'allocation': {'scope': SCOPE.model_dump(mode='json'), 'source': 'intelligence',
                       'allocation_limit': 200.0, 'other_allocation_limits': [], 'entry_fraction': 0.5,
                       'max_position_margin': 100.0, 'max_leverage': 5},
        'authorization': {'scope': SCOPE.model_dump(mode='json'), 'mode': mode},
        'risk': {'scope': SCOPE.model_dump(mode='json'), 'instrument': INST.model_dump(mode='json'),
                 'sources': ['intelligence'], 'enabled': True, 'max_leverage': 5, 'min_notional': 10.0,
                 'max_notional': 500.0, 'max_symbol_notional': 500.0, 'max_total_notional': 500.0,
                 'max_slippage_pct': 0.5, 'max_price_deviation_pct': 1.0, 'fee_buffer_pct': 0.1,
                 'size_step': 0.0001, 'max_market_age_ms': 60000, 'max_portfolio_age_ms': 60000,
                 'max_intent_age_ms': 60000},
        'initial_paper_equity': 1000.0,
    }
    if protection is not None: body['protection'] = protection
    path = Path(directory) / 'config.json'
    path.write_text(json.dumps(body), encoding='utf-8')
    return str(path)


def book(now, bid=100.0, ask=100.05, size=200.0):
    return {'time': now - 200,
            'levels': [[{'px': str(bid - i*0.01), 'sz': str(size)} for i in range(5)],
                       [{'px': str(ask + i*0.01), 'sz': str(size)} for i in range(5)]]}


def candles(now, n=60, base=100.0, drift=0.02):
    return [{'T': now - (n-i)*900000, 'c': str(base + drift*i),
             'h': str((base + drift*i)*1.002), 'l': str((base + drift*i)*0.998)} for i in range(n)]


def record(now, event_id='e1'):
    event = LeaderTradeEvent(event_id=event_id, wallet=WALLET, instrument=INST, action='OPEN', side='BUY',
                             size=1.0, before_size=0.0, after_size=1.0, exchange_ms=now-1000,
                             received_ms=now-500, fill_id='f-'+event_id)
    leader = LeaderScore(wallet=WALLET, network='MAINNET', policy_id='leader-v1', computed_ms=now,
                         score=0.8, confidence=0.7, profit_quality=0.8, consistency=0.7,
                         drawdown_quality=0.7, sample_quality=0.7, recent_quality=0.7, anomaly=0.0,
                         qualified=True, reasons=())
    return {'event': event.model_dump(mode='json'), 'leader': leader.model_dump(mode='json'),
            'policy': IntelligencePolicy().model_dump(mode='json'), 'book': book(now),
            'candles': candles(now), 'consensus': {'created_ms': now - 400}}


def snapshot(now, price=100.0):
    return MarketSnapshot(instrument=INST, exchange_ms=now-100, received_ms=now-100, price=price,
                          bid=price-0.02, ask=price+0.02, depth_usd=50000.0, completeness='COMPLETE',
                          freshness='FRESH', source='REST', source_version='test-v1')


print()
print('a configuration whose stop can never fire is refused at construction')
with tempfile.TemporaryDirectory() as tmp:
    clock = lambda: NOW
    refuses('a protection policy with no threshold',
            lambda: load_paper_backend(config(tmp, protection={'scope': SCOPE.model_dump(mode='json')}),
                                       tmp, 'MAINNET', clock),
            'protects nothing')

with tempfile.TemporaryDirectory() as tmp:
    other = Scope(tenant='t', account='0x' + 'd'*40, network='MAINNET')
    refuses('a protection policy bound to another account',
            lambda: load_paper_backend(config(tmp, protection={'scope': other.model_dump(mode='json'),
                                                               'max_hold_ms': 1000}),
                                       tmp, 'MAINNET', lambda: NOW),
            'scope mismatch')


print()
print('a held position past its limit is closed with no leader event at all')
with tempfile.TemporaryDirectory() as tmp:
    box = {'now': NOW}
    clock = lambda: box['now']
    backend = load_paper_backend(config(tmp, protection={'scope': SCOPE.model_dump(mode='json'),
                                                         'max_hold_ms': 3600_000}),
                                 tmp, 'MAINNET', clock)
    opened = backend.process(record(NOW))
    ok('the position is open', opened.get('status') == 'FILLED', opened.get('status'))
    ok('nothing fires while it is inside the limit', backend.protect(lambda i: snapshot(box['now'])) == [])

    box['now'] = NOW + 3600_000 + 1000
    results = backend.protect(lambda i: snapshot(box['now']))
    ok('one exit was taken', len(results) == 1, results)
    ok('and it says why', results[0]['reasons'] == ['MAX_HOLD_EXCEEDED'], results[0])
    ok('the order filled', results[0]['status'] == 'FILLED', results[0])
    ok('the episode is closed', results[0]['episode_state'] == 'CLOSED', results[0])
    ok('nothing is left to protect', backend.protect(lambda i: snapshot(box['now'])) == [])

    with backend.store.transaction() as db:
        row = db.execute('SELECT body FROM protective_exits WHERE scope=?', (scope_key(SCOPE),)).fetchone()
        outcome = db.execute('SELECT mode,body FROM autonomous_outcomes WHERE scope=?', (scope_key(SCOPE),)).fetchone()
    ok('the authority it used is recorded immutably', row is not None)
    stored = json.loads(row[0])
    ok('with the reason it was granted for', stored['reasons'] == ['MAX_HOLD_EXCEEDED'], stored['reasons'])
    ok('and the position it acted on, as proven', stored['position']['evidence'] == 'VERIFIED', stored['position'])
    ok('which is the side the episode opened', stored['position']['side'] == 'LONG', stored['position'])
    ok('the closed episode settled an outcome', outcome is not None)
    ok('on the PAPER channel', outcome['mode'] == 'PAPER', outcome['mode'])


print()
print('a stop that cannot read the market says so instead of staying silent')
with tempfile.TemporaryDirectory() as tmp:
    box = {'now': NOW}
    clock = lambda: box['now']
    backend = load_paper_backend(config(tmp, protection={'scope': SCOPE.model_dump(mode='json'),
                                                         'max_hold_ms': 3600_000}),
                                 tmp, 'MAINNET', clock)
    backend.process(record(NOW))
    box['now'] = NOW + 7200_000
    results = backend.protect(lambda i: None)
    ok('the position is reported, not skipped', len(results) == 1, results)
    ok('and the record names the reason it could not act',
       results[0]['status'] == 'MARKET_UNAVAILABLE', results[0])


# ------------------------------------------------- what the authority refuses
print()
print('the protective grant reduces exposure, and can do nothing else')
with tempfile.TemporaryDirectory() as tmp:
    box = {'now': NOW}
    clock = lambda: box['now']
    backend = load_paper_backend(config(tmp, protection={'scope': SCOPE.model_dump(mode='json'),
                                                         'max_hold_ms': 3600_000}),
                                 tmp, 'MAINNET', clock)
    backend.process(record(NOW))
    with backend.store.transaction() as db:
        held = backend.store.portfolio_in(db, SCOPE).positions[0]
    good = dict(version=1, scope=SCOPE, instrument=INST, source='intelligence', action='CLOSE', side='SELL',
                size=held.size, limit_price=100.0, leverage=held.leverage, slippage_pct=0.5,
                authorization='PAPER_POLICY', execution_mode='PAPER', created_ms=NOW,
                expires_ms=NOW + 60000)
    evidence = {'reasons': ['MAX_HOLD_EXCEEDED'], 'episode_id': 'e', 'policy': None}

    def grant(**changes):
        fields = dict(good, **changes)
        fields.setdefault('intent_id', 'protective-' + str(abs(hash(json.dumps(str(sorted(changes)))))))
        fields.setdefault('correlation_id', fields['intent_id'])
        return lambda: backend.gateway.authorize_protective(OrderIntent(**fields),
                                                            changes.pop('_evidence', evidence))

    refuses('an OPEN', grant(action='OPEN', side='BUY'), 'reduce')
    refuses('an ADD', grant(action='ADD', side='BUY'), 'reduce')
    refuses('a grant with no stated reason',
            lambda: backend.gateway.authorize_protective(
                OrderIntent(intent_id='p-noreason', correlation_id='p-noreason', **good), {'reasons': []}),
            'stated reason')
    refuses('a live authorization on the paper adapter',
            lambda: backend.gateway.authorize_protective(
                OrderIntent(intent_id='p-live', correlation_id='p-live',
                            **dict(good, version=3, authorization='AUTONOMOUS_POLICY', execution_mode='LIVE')),
                evidence),
            'adapter and policy')
    refuses('a close on the same side as the position', grant(side='BUY'), 'against the position')
    refuses('a size larger than the position', grant(size=held.size*2), 'exceed the position')
    refuses('a CLOSE that is not the whole position', grant(size=held.size/2), 'exactly the position')
    refuses('a source that does not own this position', grant(source='someone-else'), 'proven it owns')

    # A partial REDUCE is the one narrowing that IS allowed.
    backend.gateway.authorize_protective(
        OrderIntent(intent_id='p-reduce', correlation_id='p-reduce', **dict(good, action='REDUCE', size=held.size/2)),
        evidence)
    with backend.store.transaction() as db:
        ok('a partial reduce is granted',
           db.execute('SELECT 1 FROM grants WHERE id=?', ('p-reduce',)).fetchone() is not None)

print()
print('the same stop, in the mode where it protects real money')
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _offline_exchange as live

with tempfile.TemporaryDirectory() as tmp:
    guard = PositionProtectionPolicy(scope=live.SCOPE, liquidation_buffer_pct=5.0)
    backend, client, box, store = live.live_backend(tmp=tmp, protection=guard)
    # The venue reports a liquidation price four percent under a 100 mark.
    client.liquidation = 96.0
    opened = backend.process(live.record())
    ok('a live position is open', opened.get('status') == 'FILLED', opened.get('status'))
    ok('nothing fires while the account is not near liquidation',
       backend.protect(lambda i: live.market_snapshot(i, now=box['now'], price=200.0)) == [])

    box['now'] += 5000
    results = backend.protect(lambda i: live.market_snapshot(i, now=box['now']))
    ok('the liquidation stop fired', len(results) == 1, results)
    ok('and named the reason', results[0]['reasons'] == ['LIQUIDATION_PROXIMITY'], results[0])
    ok('the live close filled', results[0]['status'] == 'FILLED', results[0])
    ok('the episode is closed', results[0]['episode_state'] == 'CLOSED', results[0])
    sent = client.submitted[-1]
    ok('the order was sent reduce-only', sent['reduce_only'] is True, sent)
    ok('and it sold the long it was closing', sent['is_buy'] is False, sent)
    with store.transaction() as db:
        outcome = db.execute('SELECT mode FROM autonomous_outcomes WHERE scope=?',
                             (scope_key(live.SCOPE),)).fetchone()
    ok('and it settled on the LIVE channel', outcome is not None and outcome['mode'] == 'LIVE', outcome)

print()
print('all protection checks passed')
