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

from core.foundation.contracts import InstrumentId, Scope, PortfolioSnapshot
from core.foundation.authorization import AuthorizationPolicy
from core.foundation.autonomous_allocation import AutonomousAllocationPolicy
from core.foundation.risk import RiskPolicy
from core.foundation.store import Store, scope_key
from core.foundation.copy_execution import HyperliquidExecutionAdapter, client_order_id
from core.foundation.data import copy_account_snapshot
from core.position_episodes import PositionEpisode
from core.autonomous import AutonomousBackend, LiveGuardPolicy
from core.intelligence.models import IntelligencePolicy, LeaderScore, LeaderTradeEvent

NOW = 1_700_000_000_000
ACCOUNT = '0x' + 'b' * 40
WALLET = '0x' + 'a' * 40
SCOPE = Scope(tenant='t', account=ACCOUNT, network='MAINNET')
INST = InstrumentId(network='MAINNET', dex='', symbol='BTC')


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


# --------------------------------------------------- offline signing client
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

    # --- reads
    def positions(self, *a): return [dict(r) for r in self.rows]
    def frontend_open_orders(self, pool): return list(self.orders) if pool == '' else []
    def capital_snapshot(self): return types.SimpleNamespace(sizing_base_usdc=1000.0)
    def available_margin(self, dex): return 1000.0
    def query_order_by_cloid(self, cloid):
        return self.responses[cloid]

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
            self.rows = [{'coin': coin, 'dex': dex or '', 'side': 'LONG' if signed > 0 else 'SHORT',
                          'size': abs(signed), 'entry_price': cost / abs(signed),
                          'position_value': abs(signed) * (cost / abs(signed)),
                          'margin_used': abs(signed) * (cost / abs(signed)) / 5,
                          'leverage': 5, 'liquidation_price': None, 'margin_mode': 'cross'}]
        self.stamp = self.clock()
        return {'status': 'ok'}

    responses = {}


# ------------------------------------------------------------ the backend
def build(*, halted=False, tmp=None):
    clock_box = {'now': NOW}
    clock = lambda: clock_box['now']
    client = FakeClient(clock)
    client.responses = {}
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
                                live_guard=LiveGuardPolicy(halted=halted), evidence=evidence)
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


print()
print('a LIVE_AUTO backend reaches a signed, reconciled entry without raising')
with tempfile.TemporaryDirectory() as tmp:
    backend, client, clock_box, store = build(tmp=tmp)
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
    backend, client, clock_box, store = build(halted=True, tmp=tmp)
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
