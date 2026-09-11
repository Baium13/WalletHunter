"""Authenticated, strictly read-only view of the autonomous PAPER/LIVE runtime.

The whole autonomous engine - modes, consensus, sizing, intents, episodes,
outcomes, the live guard - had no API at all, so no interface could show
whether it was doing anything. This exposes it.

Two properties are deliberate and load-bearing:

* Every store is opened ``mode=ro`` with ``PRAGMA query_only=ON``. This module
  cannot write a row, cannot authorize, and cannot submit. A read model that
  can mutate financial state is not a read model.
* Nothing here invents a value. A store that is absent, unreadable or empty is
  reported as such; it is never smoothed into a zero that reads like a fact.
"""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from fastapi import APIRouter, Header, Query
from pydantic import TypeAdapter
from core.foundation.contracts import Network

# Kept small on purpose: these are phone-sized payloads polled by a UI, not a
# replay interface. Full evidence stays in the store for the audit path.
DECISION_FIELDS = ('event', 'consensus', 'authorization', 'mode', 'status', 'sizing',
                   'live_guard', 'allocation', 'correlation_id', 'risk', 'reason')
INTENT_FIELDS = ('intent_id', 'instrument', 'action', 'side', 'size', 'limit_price',
                 'leverage', 'execution_mode', 'authorization', 'version', 'created_ms')


def _load(text):
    try:
        value = json.loads(text) if text else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _pick(body, fields):
    return {k: body[k] for k in fields if k in body} if isinstance(body, dict) else {}


class AutonomyView:
    """One configured runtime: a mode, its state directory, its stores."""

    def __init__(self, name, state_directory, network):
        self.name = str(name)
        self.directory = Path(state_directory)
        self.network = TypeAdapter(Network).validate_python(network)

    @property
    def path(self):
        return self.directory / 'autonomy.sqlite'

    def _connect(self):
        path = self.path
        if not path.exists(): raise FileNotFoundError('STATE_NOT_INITIALIZED')
        if path.is_symlink() or self.directory.is_symlink(): raise ValueError('STATE_PATH_UNSAFE')
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return db

    @staticmethod
    def _tables(db):
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def empty(self, reason):
        return {'name': self.name, 'network': self.network, 'mode': None, 'ready': False,
                'reason': reason, 'portfolio': None, 'live_guard': None, 'counts': {},
                'decisions': [], 'positions': [], 'episodes': [], 'outcomes': [],
                'attenuation': {}, 'blockers': {}, 'cursor': 0}

    def read(self, after=0, limit=20):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('QUERY_BOUND')
        try:
            with closing(self._connect()) as db:
                return self._read(db, after, limit)
        except FileNotFoundError:
            return self.empty('STATE_NOT_INITIALIZED')
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            return self.empty('STATE_UNAVAILABLE')

    def _read(self, db, after, limit):
        tables = self._tables(db)
        if 'autonomous_modes' not in tables: return self.empty('STATE_NOT_INITIALIZED')
        row = db.execute('SELECT scope,mode FROM autonomous_modes LIMIT 1').fetchone()
        if row is None: return self.empty('MODE_NOT_CLAIMED')
        scope, mode = row['scope'], row['mode']
        out = self.empty('READY')
        out.update(mode=mode, ready=True, scope=scope)

        portfolio = None
        if 'portfolios' in tables:
            hit = db.execute('SELECT body FROM portfolios WHERE scope=?', (scope,)).fetchone()
            body = _load(hit['body']) if hit else None
            if body:
                portfolio = {k: body.get(k) for k in ('revision', 'equity', 'sizing_capital',
                    'available_collateral', 'exchange_ms', 'received_ms', 'completeness', 'evidence')}
                # Positions are the thing an operator actually looks for; the
                # rest of the snapshot is accounting they can ask for later.
                out['positions'] = [{k: p.get(k) for k in ('instrument', 'side', 'size', 'entry_price',
                    'notional', 'margin', 'leverage', 'liquidation_price', 'evidence')}
                    for p in (body.get('positions') or [])]
        out['portfolio'] = portfolio

        if 'live_guard_state' in tables:
            hit = db.execute('SELECT day,opening_equity,halted FROM live_guard_state WHERE scope=?',
                             (scope,)).fetchone()
            if hit is not None:
                equity = portfolio.get('equity') if portfolio else None
                opening = hit['opening_equity']
                out['live_guard'] = {'day': hit['day'], 'opening_equity': opening,
                    'halted': bool(hit['halted']),
                    'day_pnl': (equity - opening) if isinstance(equity, (int, float))
                               and isinstance(opening, (int, float)) else None}

        counts = {}
        if 'autonomous_decisions' in tables:
            for status, total in db.execute(
                    "SELECT COALESCE(json_extract(body,'$.status'),'UNKNOWN'),COUNT(*) "
                    'FROM autonomous_decisions WHERE scope=? GROUP BY 1', (scope,)):
                counts[str(status)] = total
        if 'intents' in tables:
            counts['intents'] = db.execute('SELECT COUNT(*) FROM intents WHERE scope=?', (scope,)).fetchone()[0]
            for status, total in db.execute('SELECT status,COUNT(*) FROM intents WHERE scope=? GROUP BY status',
                                            (scope,)):
                counts['intent_' + str(status)] = total
        if 'position_episodes' in tables:
            counts['episodes'] = db.execute('SELECT COUNT(*) FROM position_episodes WHERE scope=?',
                                            (scope,)).fetchone()[0]
        if 'autonomous_outcomes' in tables:
            counts['outcomes'] = db.execute('SELECT COUNT(*) FROM autonomous_outcomes WHERE scope=?',
                                            (scope,)).fetchone()[0]
        if 'autonomous_quarantine' in tables:
            counts['quarantined'] = db.execute('SELECT COUNT(*) FROM autonomous_quarantine WHERE scope=?',
                                               (scope,)).fetchone()[0]
        out['counts'] = counts

        if 'autonomous_decisions' in tables:
            rows = db.execute('SELECT rowid AS seq,event_id,body,intent IS NOT NULL AS submitted '
                              'FROM autonomous_decisions WHERE scope=? AND rowid>? ORDER BY rowid DESC LIMIT ?',
                              (scope, after, limit)).fetchall()
            decisions = []
            reasons, refusals = {}, {}
            for hit in rows:
                body = _load(hit['body']) or {}
                item = _pick(body, DECISION_FIELDS)
                item.update(seq=hit['seq'], event_id=hit['event_id'], submitted=bool(hit['submitted']))
                consensus = body.get('consensus') or {}
                # Why a decision was small, and why one was refused, are the two
                # questions an operator asks; surface both without a second call.
                for name in (consensus.get('attenuation') or ()):
                    reasons[str(name)] = reasons.get(str(name), 0) + 1
                for name in (consensus.get('blockers') or ()):
                    refusals[str(name)] = refusals.get(str(name), 0) + 1
                decisions.append(item)
            out['decisions'] = decisions
            out['attenuation'] = reasons
            out['blockers'] = refusals
            out['cursor'] = max((d['seq'] for d in decisions), default=after)

        if 'position_episodes' in tables:
            out['episodes'] = [_load(r['body']) or {} for r in db.execute(
                'SELECT body FROM position_episodes WHERE scope=? ORDER BY rowid DESC LIMIT 20', (scope,))]
        if 'autonomous_outcomes' in tables:
            out['outcomes'] = [_load(r['body']) or {} for r in db.execute(
                'SELECT body FROM autonomous_outcomes WHERE scope=? ORDER BY rowid DESC LIMIT 20', (scope,))]
        return out

    def intents(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 50: raise ValueError('QUERY_BOUND')
        try:
            with closing(self._connect()) as db:
                tables = self._tables(db)
                if 'intents' not in tables: return {'name': self.name, 'intents': []}
                rows = db.execute('SELECT id,body,status,receipt FROM intents ORDER BY rowid DESC LIMIT ?',
                                  (limit,)).fetchall()
                out = []
                for hit in rows:
                    body = _load(hit['body']) or {}
                    receipt = _load(hit['receipt'])
                    out.append({**_pick(body, INTENT_FIELDS), 'id': hit['id'], 'status': hit['status'],
                                'receipt': {k: receipt.get(k) for k in ('status', 'filled_size',
                                    'average_price', 'exchange_ms')} if receipt else None})
                return {'name': self.name, 'intents': out}
        except FileNotFoundError:
            return {'name': self.name, 'intents': [], 'reason': 'STATE_NOT_INITIALIZED'}
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            return {'name': self.name, 'intents': [], 'reason': 'STATE_UNAVAILABLE'}


def router(runtimes, network, authenticate, budget_path=None):
    """``runtimes`` maps a display name to a state directory (PAPER, LIVE, ...)."""
    api = APIRouter()
    views = {str(name): AutonomyView(name, directory, network)
             for name, directory in dict(runtimes or {}).items()}

    def budget():
        """Current weighted API usage, so the UI can show the real constraint."""
        if not budget_path: return None
        try:
            path = Path(budget_path)
            if not path.exists(): return None
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                now = time.time()
                used = db.execute('SELECT COALESCE(SUM(weight),0) FROM requests WHERE at>?', (now - 60,)).fetchone()[0]
                limits = db.execute('SELECT soft,hard,cooldown FROM limits WHERE id=1').fetchone()
                top = [{'source': r[0], 'endpoint': r[1], 'weight_per_minute': round(r[2], 1)}
                       for r in db.execute('SELECT source,endpoint,SUM(weight)/5.0 FROM requests '
                                           'WHERE at>? GROUP BY source,endpoint ORDER BY 3 DESC LIMIT 8',
                                           (now - 300,))]
                return {'weight_1m': used, 'soft': limits['soft'], 'hard': limits['hard'],
                        'cooling_down': bool(limits['cooldown'] > now), 'top_sources': top}
        except (sqlite3.Error, OSError, ValueError, TypeError):
            return None

    @api.get('/api/autonomy')
    def autonomy(after: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50),
                 x_telegram_init_data: str | None = Header(default=None)):
        authenticate(x_telegram_init_data)
        return {'network': network, 'runtimes': [view.read(after, limit) for view in views.values()],
                'budget': budget(), 'configured': sorted(views)}

    @api.get('/api/autonomy/{name}')
    def one(name: str, after: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50),
            x_telegram_init_data: str | None = Header(default=None)):
        authenticate(x_telegram_init_data)
        view = views.get(name)
        # An unconfigured runtime is a configuration answer, not a 404 the UI
        # has to special-case: it says plainly that nothing is set up.
        if view is None: return {'name': name, 'ready': False, 'reason': 'RUNTIME_NOT_CONFIGURED'}
        return view.read(after, limit)

    @api.get('/api/autonomy/{name}/intents')
    def intents(name: str, limit: int = Query(20, ge=1, le=50),
                x_telegram_init_data: str | None = Header(default=None)):
        authenticate(x_telegram_init_data)
        view = views.get(name)
        if view is None: return {'name': name, 'intents': [], 'reason': 'RUNTIME_NOT_CONFIGURED'}
        return view.intents(limit)

    return api
