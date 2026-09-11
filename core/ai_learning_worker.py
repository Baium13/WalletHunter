"""One shared public-market learner, without account or order capabilities.

Collector state is persistent across bot/web processes. Failed reads never become
zero prices, empty successful history, or a false healthy training status.
"""
from contextlib import closing
import json
import math
import os
import sqlite3
import time
from core.ai_review import account_guard

BAR_MS = 900000
UNIVERSE = ('BTC', 'ETH')


class AiLearningWorker:
    def __init__(self, root, learner=None):
        self.root = os.path.abspath(root)
        if learner is None:
            from core.ai_learning import AiLearning
            learner = AiLearning(self.root)
        self.learner = learner
        self.path = os.path.join(self.root, 'data', 'ai_learning.sqlite3')
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with closing(self._connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS collector_status (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            db.commit()
        if os.name != 'nt': os.chmod(self.path, 0o600)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def _read_status(self):
        with closing(self._connect()) as db:
            row = db.execute('SELECT payload FROM collector_status WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {'status': 'NOT_STARTED', 'last_success_ms': None,
            'last_attempt_ms': None, 'watermarks': {}, 'errors': []}

    def _save_status(self, status):
        with closing(self._connect()) as db:
            db.execute('INSERT INTO collector_status VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                       (json.dumps(status, allow_nan=False, separators=(',', ':')),))
            db.commit()

    def summary(self, now_ms=None):
        try:
            result = self.learner.summary(now_ms=now_ms)
            result['collector'] = self._read_status()
            now = int(time.time() * 1000) if now_ms is None else now_ms
            collector = result['collector']
            basis = collector.get('last_attempt_ms') if collector['status'] == 'FETCHING' else collector.get('last_success_ms')
            if collector['status'] in {'OK', 'FETCHING'} and basis and now - basis > 2 * BAR_MS:
                collector['status'] = 'STALE'
            result['real_execution_available'] = False
            return result
        except Exception:
            return {'status': 'UNAVAILABLE', 'reason': 'learning_state_unavailable',
                'real_execution_available': False, 'collector': {'status': 'UNAVAILABLE',
                    'error_code': 'learning_state_unavailable'}}

    def cycle(self, public_reader, now_ms=None):
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError('invalid_learning_time')
        try:
            with account_guard(self.root, 'ai-learning-public-collector'):
                return self._cycle_locked(public_reader, now, refresh_clock=now_ms is None)
        except OSError:
            # Another process owns this collector. Do not overwrite its status.
            result = self.summary(now)
            result['collector'] = dict(result.get('collector', {}), status='BUSY')
            return result

    def _cycle_locked(self, reader, now, refresh_clock=False):
        status = self._read_status()
        if status.get('last_attempt_ms') and status['last_attempt_ms'] > now:
            raise ValueError('out_of_order_learning_cycle')
        bucket = now // BAR_MS
        if status['status'] == 'OK' and (status.get('last_success_ms') or 0) // BAR_MS == bucket:
            return self.summary(now)
        status.update(status='FETCHING', last_attempt_ms=now, errors=[])
        self._save_status(status)
        watermarks = dict(status.get('watermarks') or {})
        for coin in UNIVERSE:
            try:
                prior = watermarks.get(coin)
                start = max(0, now - 1500 * BAR_MS) if prior is None else max(0, prior - 64 * BAR_MS, now - 4998 * BAR_MS)
                rows = reader._info({'type': 'candleSnapshot', 'req': {'coin': coin,
                    'interval': '15m', 'startTime': start, 'endTime': now}})
                if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
                    raise ValueError('invalid_candle_response')
                closed = []
                for row in rows:
                    if not isinstance(row, dict): raise ValueError('invalid_candle_row')
                    stamp = row.get('T')
                    if isinstance(stamp, bool): raise ValueError('invalid_candle_time')
                    stamp = float(stamp)
                    if not math.isfinite(stamp) or stamp < 0 or stamp != int(stamp):
                        raise ValueError('invalid_candle_time')
                    if stamp < now: closed.append(row)
                if len(closed) < 60:
                    raise ValueError('insufficient_closed_history')
                opens = [int(row['t']) for row in closed]
                if max(opens) != (bucket - 1) * BAR_MS:
                    raise ValueError('latest_closed_candle_missing')
                self.learner.ingest(coin, closed, now_ms=now)
                watermarks[coin] = max(opens)
            except Exception:
                # Do not return raw HTTP errors, URLs or arbitrary payloads.
                status['errors'].append({'coin': coin, 'reason': 'public_history_unavailable_or_invalid'})
        status['watermarks'] = watermarks
        if status['errors']:
            status.update(status='UNAVAILABLE' if len(status['errors']) == len(UNIVERSE) else 'PARTIAL_DATA')
        else:
            try:
                trained_at = int(time.time() * 1000) if refresh_clock else now
                if trained_at < now or trained_at // BAR_MS != bucket:
                    raise ValueError('collection_crossed_candle_boundary')
                self.learner.train(now_ms=trained_at)
                status.update(status='OK', last_success_ms=trained_at)
            except Exception:
                status.update(status='UNAVAILABLE', errors=[{'reason': 'training_failed'}])
        self._save_status(status)
        return self.summary(now)
