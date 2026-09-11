"""AutonomyView: the read model must never write, never invent, never crash."""
import sys, os, json, sqlite3, tempfile, types, time
sys.path.insert(0, '.')

# The view class under test does not need fastapi, so this runs with or
# without it installed. When it is absent we stand in for the import surface
# and can additionally exercise the route functions directly.
STUBBED = False
try:
    import fastapi  # noqa: F401
except ImportError:
    fake = types.ModuleType('fastapi')
    class APIRouter:
        def __init__(self): self.routes = []
        def get(self, path):
            def wrap(fn): self.routes.append((path, fn)); return fn
            return wrap
    fake.APIRouter = APIRouter
    fake.Header = lambda default=None: default
    fake.Query = lambda default=0, **k: default
    sys.modules['fastapi'] = fake
    STUBBED = True

from webapp.autonomy_api import AutonomyView, router


def ok(label, cond, detail=''):
    if not cond: raise AssertionError(label + ' :: ' + str(detail))
    print('  ok  ' + label)


SCOPE = '{"tenant":"1","account":"0xabc","network":"MAINNET"}'


def build(directory, *, decisions=(), positions=(), guard=None, intents=(), mode='PAPER_AUTO'):
    path = os.path.join(directory, 'autonomy.sqlite')
    db = sqlite3.connect(path)
    db.executescript('''
      CREATE TABLE autonomous_modes(scope TEXT PRIMARY KEY,mode TEXT NOT NULL);
      CREATE TABLE autonomous_decisions(id TEXT PRIMARY KEY,scope TEXT,event_id TEXT,body TEXT,intent TEXT);
      CREATE TABLE portfolios(scope TEXT PRIMARY KEY,body TEXT NOT NULL);
      CREATE TABLE intents(id TEXT PRIMARY KEY,scope TEXT NOT NULL,body TEXT NOT NULL,
        status TEXT NOT NULL,decision TEXT,reservation TEXT,receipt TEXT);
      CREATE TABLE position_episodes(id TEXT PRIMARY KEY,scope TEXT NOT NULL,body TEXT NOT NULL);
      CREATE TABLE autonomous_outcomes(id TEXT PRIMARY KEY,scope TEXT NOT NULL,mode TEXT NOT NULL,body TEXT NOT NULL);
    ''')
    db.execute('INSERT INTO autonomous_modes VALUES(?,?)', (SCOPE, mode))
    db.execute('INSERT INTO portfolios VALUES(?,?)', (SCOPE, json.dumps({
        'revision': 12, 'equity': 980.0, 'sizing_capital': 980.0, 'available_collateral': 900.0,
        'exchange_ms': 1, 'received_ms': 2, 'completeness': 'COMPLETE', 'evidence': 'FAKE',
        'positions': list(positions), 'secret_internal_field': 'must not leak'})))
    for i, body in enumerate(decisions):
        db.execute('INSERT INTO autonomous_decisions VALUES(?,?,?,?,?)',
                   ('d%d' % i, SCOPE, 'e%d' % i, json.dumps(body), 'x' if body.get('_submitted') else None))
    for i, body in enumerate(intents):
        db.execute('INSERT INTO intents VALUES(?,?,?,?,NULL,NULL,?)',
                   ('i%d' % i, SCOPE, json.dumps(body), body.get('_status', 'FILLED'),
                    json.dumps({'status': 'FILLED', 'filled_size': 1.0, 'average_price': 100.0,
                                'exchange_ms': 5, 'internal': 'hidden'})))
    if guard is not None:
        db.execute('CREATE TABLE live_guard_state(scope TEXT PRIMARY KEY,day TEXT,opening_equity REAL,halted INTEGER NOT NULL DEFAULT 0)')
        db.execute('INSERT INTO live_guard_state VALUES(?,?,?,?)', (SCOPE, guard[0], guard[1], guard[2]))
    db.execute('INSERT INTO position_episodes VALUES(?,?,?)', ('ep1', SCOPE, json.dumps({'leader': '0xL', 'state': 'OPEN'})))
    db.execute('INSERT INTO autonomous_outcomes VALUES(?,?,?,?)', ('o1', SCOPE, mode, json.dumps({'pnl': 4.5})))
    db.commit(); db.close()
    return path


print('an unconfigured or missing runtime answers, it does not fail')
missing = AutonomyView('PAPER', tempfile.mkdtemp(), 'MAINNET').read()
ok('a missing store is reported, not crashed',
   missing['ready'] is False and missing['reason'] == 'STATE_NOT_INITIALIZED', missing)
ok('and invents no numbers', missing['portfolio'] is None and missing['counts'] == {}, missing)

junk = tempfile.mkdtemp()
open(os.path.join(junk, 'autonomy.sqlite'), 'wb').write(b'this is not a database')
broken = AutonomyView('PAPER', junk, 'MAINNET').read()
ok('a corrupt store degrades cleanly', broken['ready'] is False and broken['reason'] == 'STATE_UNAVAILABLE', broken)

bare = tempfile.mkdtemp()
sqlite3.connect(os.path.join(bare, 'autonomy.sqlite')).close()
ok('an empty database is not initialized',
   AutonomyView('PAPER', bare, 'MAINNET').read()['reason'] == 'STATE_NOT_INITIALIZED')


print('a live runtime reports what actually happened')
d = tempfile.mkdtemp()
build(d, mode='LIVE_AUTO', guard=('2026-09-11', 1000.0, 0),
      positions=[{'instrument': {'symbol': 'BTC'}, 'side': 'LONG', 'size': 0.01, 'entry_price': 60000.0,
                  'notional': 600.0, 'margin': 60.0, 'leverage': 10, 'evidence': 'VERIFIED',
                  'contributions': [{'source': 'AUTONOMOUS', 'notional': 600.0}]}],
      decisions=[
          {'status': 'SUBMITTED', 'mode': 'LIVE_AUTO', '_submitted': True,
           'consensus': {'decision': 'COPY_LONG', 'confidence': .42, 'blockers': [],
                         'attenuation': ['TREND_PARTIAL', 'DEPTH_CAUTION']},
           'sizing': {'leverage': 3, 'leverage_source': 'LEADER_CLAMPED'},
           'event': {'event_id': 'e0', 'instrument': {'symbol': 'BTC'}}, 'candles': ['huge'] * 500},
          {'status': 'WAIT', 'mode': 'LIVE_AUTO',
           'consensus': {'decision': 'WAIT', 'confidence': .0,
                         'blockers': ['CONSENSUS_BELOW_THRESHOLD'], 'attenuation': ['TREND_DISAGREEMENT']},
           'event': {'event_id': 'e1'}, 'book': {'huge': True}},
      ],
      intents=[{'intent_id': 'i0', 'instrument': {'symbol': 'BTC'}, 'action': 'OPEN', 'side': 'BUY',
                'size': 0.01, 'limit_price': 60000.0, 'leverage': 3, 'execution_mode': 'LIVE',
                'authorization': 'AUTONOMOUS_POLICY', 'version': 3, 'created_ms': 7,
                'private_signing_detail': 'must not leak'}])
view = AutonomyView('LIVE', d, 'MAINNET')
r = view.read()
ok('the mode is read from the store, not configuration', r['mode'] == 'LIVE_AUTO', r['mode'])
ok('equity is reported', r['portfolio']['equity'] == 980.0, r['portfolio'])
ok('open positions are listed', len(r['positions']) == 1 and r['positions'][0]['leverage'] == 10, r['positions'])
ok('the live guard shows the day P&L', r['live_guard']['day_pnl'] == -20.0, r['live_guard'])
ok('the guard halt flag is a bool', r['live_guard']['halted'] is False, r['live_guard'])
ok('decision statuses are counted', r['counts'].get('SUBMITTED') == 1 and r['counts'].get('WAIT') == 1, r['counts'])
ok('intents are counted', r['counts'].get('intents') == 1, r['counts'])
ok('episodes and outcomes are counted',
   r['counts'].get('episodes') == 1 and r['counts'].get('outcomes') == 1, r['counts'])
ok('why trades were sized down is aggregated',
   r['attenuation'] == {'TREND_PARTIAL': 1, 'DEPTH_CAUTION': 1, 'TREND_DISAGREEMENT': 1}, r['attenuation'])
ok('why trades were refused is aggregated',
   r['blockers'] == {'CONSENSUS_BELOW_THRESHOLD': 1}, r['blockers'])
by_event = {d['event_id']: d for d in r['decisions']}
ok('the applied leverage and its source are visible',
   by_event['e0']['sizing'] == {'leverage': 3, 'leverage_source': 'LEADER_CLAMPED'}, by_event['e0'])
ok('submission is flagged', by_event['e0']['submitted'] is True, by_event['e0'])
ok('a refused decision is not flagged as submitted', by_event['e1']['submitted'] is False, by_event['e1'])

print('payloads stay phone-sized and leak nothing')
blob = json.dumps(r)
ok('raw candles are not broadcast', 'huge' not in blob, blob[:200])
ok('unlisted portfolio fields do not leak', 'secret_internal_field' not in blob)
ok('the payload is small', len(blob) < 6000, len(blob))
i = view.intents()
ok('intent fields are whitelisted', 'private_signing_detail' not in json.dumps(i), i)
ok('receipt fields are whitelisted', 'internal' not in json.dumps(i), i)
ok('the receipt still says what filled',
   i['intents'][0]['receipt']['filled_size'] == 1.0 and i['intents'][0]['status'] == 'FILLED', i)

print('paging is bounded and ordered newest first')
ok('newest decision first', r['decisions'][0]['seq'] > r['decisions'][-1]['seq'], [d['seq'] for d in r['decisions']])
for bad in ((-1, 20), (0, 0), (0, 51), ('x', 20), (0, 'x')):
    try:
        view.read(*bad); raise AssertionError('accepted %r' % (bad,))
    except ValueError: pass
ok('out-of-range paging is refused', True)

print('the store is opened read-only')
db = view._connect()
try:
    db.execute("INSERT INTO autonomous_modes VALUES('x','y')")
    raise AssertionError('the read model was able to write')
except sqlite3.OperationalError as exc:
    ok('a write through the read model fails', 'readonly' in str(exc).lower() or 'query_only' in str(exc).lower(), exc)
finally:
    db.close()

if not STUBBED:
    print('router route bodies skipped: real fastapi installed, view checks above cover the logic')
    print('ALL OK')
    raise SystemExit(0)

print('the router answers for runtimes that are not configured')
api = router({'PAPER': d}, 'MAINNET', lambda h: {'id': 1})
routes = dict(api.routes)
one = routes['/api/autonomy/{name}']('LIVE', 0, 20, None)
ok('an unconfigured runtime says so instead of 404',
   one == {'name': 'LIVE', 'ready': False, 'reason': 'RUNTIME_NOT_CONFIGURED'}, one)
allr = routes['/api/autonomy'](0, 20, None)
ok('the index lists what is configured', allr['configured'] == ['PAPER'], allr['configured'])
ok('and includes the runtime payload', allr['runtimes'][0]['mode'] == 'LIVE_AUTO', allr['runtimes'][0]['mode'])
ok('an absent budget database is None, not a fabricated zero', allr['budget'] is None, allr['budget'])

print('ALL OK')
