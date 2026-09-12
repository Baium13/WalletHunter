"""The public read cache must never hold account, order or history evidence.

Merged from the upstream `perf: coalesce public Hyperliquid reads` work. The
cache is worth having - one busy minute spent on useful evidence instead of
duplicate snapshots - but a cache that admits a balance or an order status
would make a stale financial read look fresh, which is the one failure this
system cannot tolerate. These assertions pin the allow-list and the ceilings
the merge deliberately did NOT take from upstream.
"""
import sqlite3, sys, tempfile, os, time, json
sys.path.insert(0, '.')

from core import hl_budget


def ok(label, cond, detail=''):
    if not cond: raise AssertionError(label + ' :: ' + str(detail))
    print('  ok  ' + label)


print('only public snapshots are cacheable')
FINANCIAL = ('clearinghouseState', 'spotClearinghouseState', 'userFills', 'userFillsByTime',
             'openOrders', 'frontendOpenOrders', 'userAbstraction', 'historicalOrders',
             'orderStatus', 'userFunding', 'userNonFundingLedgerUpdates', 'subAccounts',
             'exchange', 'withdraw', 'usdSend', 'vaultTransfer')
for endpoint in FINANCIAL:
    ok('%-32s is NOT cacheable' % endpoint, endpoint not in hl_budget.READ_CACHE_TTL)
for endpoint in ('allMids', 'l2Book', 'candleSnapshot', 'metaAndAssetCtxs'):
    ok('%-32s is cacheable' % endpoint, endpoint in hl_budget.READ_CACHE_TTL)
ok('every TTL is short and positive',
   all(0 < ttl <= 300 for ttl in hl_budget.READ_CACHE_TTL.values()), hl_budget.READ_CACHE_TTL)
ok('the market TTLs are seconds, not minutes',
   all(hl_budget.READ_CACHE_TTL[k] <= 5 for k in ('allMids', 'l2Book', 'metaAndAssetCtxs', 'candleSnapshot')))


print('the merge did not take upstream\'s raised ceilings')
# Upstream planned to 1180 and dropped the reserve. The 150 units between the
# elevated band and the hard ceiling exist so discovery and history work can
# never crowd out order safety.
ok('elevated band stays at 1000', hl_budget.ELEVATED_CEILING == 1000, hl_budget.ELEVATED_CEILING)
ok('hard planned ceiling stays at 1150', hl_budget.HARD_PLANNED_CEILING == 1150, hl_budget.HARD_PLANNED_CEILING)
ok('the P0 reserve is intact',
   hl_budget.HARD_PLANNED_CEILING - hl_budget.ELEVATED_CEILING == 150)


print('a moving candle window collapses onto closed bars')
now_ms = 1_700_000_000_000
def window(end):
    return {'type': 'candleSnapshot', 'req': {'coin': 'BTC', 'interval': '15m',
                                              'startTime': end - 10 * 900000, 'endTime': end}}
a = hl_budget._read_cache_payload('candleSnapshot', window(now_ms))
b = hl_budget._read_cache_payload('candleSnapshot', window(now_ms + 1000))
ok('one second later is the same request', a == b, (a, b))
c = hl_budget._read_cache_payload('candleSnapshot', window(now_ms + 900000))
ok('the next bar is a different request', a != c)
ok('a non-candle payload is untouched',
   hl_budget._read_cache_payload('l2Book', {'type': 'l2Book', 'coin': 'BTC'}) == {'type': 'l2Book', 'coin': 'BTC'})
for broken in ({'req': {'interval': 'nope', 'startTime': 1, 'endTime': 2}},
               {'req': {'interval': '15m', 'endTime': 2}},
               {'req': {'interval': '15m', 'startTime': 9, 'endTime': 2}},
               {'req': 'not-a-dict'}, {}):
    ok('malformed candle window passes through unchanged',
       hl_budget._read_cache_payload('candleSnapshot', broken) == broken, broken)


print('the cache coalesces without inventing evidence')
path = os.path.join(tempfile.mkdtemp(), 'b.sqlite3')
budget = hl_budget.Budget(path)
status, lease = budget.read_acquire('k1', 1.0)
ok('first caller owns the lease', status == 'owner')
budget.read_complete('k1', lease, json.dumps({'px': 1}), 1.0)
status, body = budget.read_acquire('k1', 1.0)
ok('the next caller gets the cached body', status == 'hit' and json.loads(body) == {'px': 1}, body)

budget.read_complete('k2', 0, None, 1.0)
status, _ = budget.read_acquire('k2', 1.0)
ok('a failed upstream read caches nothing', status == 'owner')

status, lease = budget.read_acquire('k3', 0.01)
budget.read_complete('k3', lease, json.dumps({'px': 2}), 0.01)
time.sleep(0.05)
status, _ = budget.read_acquire('k3', 0.01)
ok('an expired entry is not served', status == 'owner')

with sqlite3.connect(path) as db:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
ok('the cache has its own table', 'read_cache' in tables, sorted(tables))

print('and the saving is real, measured through the request path')
import requests
os.environ['HL_API_BUDGET_DB'] = os.path.join(tempfile.mkdtemp(), 'b2.sqlite3')
hl_budget._configured = None if hasattr(hl_budget, '_configured') else None
calls = []


def stub(self, method, url, **kw):
    calls.append(kw.get('json', {}).get('type'))
    response = requests.Response(); response.status_code = 200; response.url = url
    response._content = json.dumps({'levels': [[{'px': '1', 'sz': '2'}], [{'px': '1.1', 'sz': '3'}]],
                                    'time': 1}).encode()
    response.headers['Content-Type'] = 'application/json'
    return response


requests.Session.request = stub
session = hl_budget.BudgetSession()
url = 'https://api.hyperliquid.xyz/info'
for _ in range(5):
    session.request('POST', url, json={'type': 'l2Book', 'coin': 'BTC'})
ok('five identical book reads cost one request', calls.count('l2Book') == 1, calls)

before = len(calls)
for _ in range(2):
    session.request('POST', url, json={'type': 'clearinghouseState', 'user': '0x' + 'ab' * 20})
ok('a financial read is never served from cache', len(calls) - before == 2, calls)

before = len(calls)
session.request('POST', url, json={'type': 'l2Book', 'coin': 'ETH'})
ok('a different instrument is a different request', len(calls) - before == 1, calls)

print('ALL OK')
