"""Durable, bounded public research worker. It has no execution dependencies.

Canonical events contain immutable record references; analytics payload and event
are committed together. A ranking change never mutates trading profiles.
"""
import json
import math
import re
import time
import uuid
import hashlib
import sqlite3
import random
from collections import deque
from pathlib import Path
from contextlib import closing
from decimal import Decimal
from core.settings import validated_network
from core.fill_history import fetch_fills, filter_perp_fills
from core.foundation.contracts import Scope, InstrumentId, AnalysisResult, DomainEvent
from core.foundation.store import Store
from .models import IntelligencePolicy, LeaderTradeEvent, LeaderScore, ConsensusDecision
from .analysis import reports, score, DAY
from .agents import evaluate, consensus
from core.hl_budget import priority_scope, BudgetUnavailable


def packed(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def identity(value): return hashlib.sha256(packed(value).encode()).hexdigest()


def wallet(value):
    if not isinstance(value,str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}',value): raise ValueError('WALLET_INVALID')
    return value.lower()


class RequestBudget:
    def __init__(self, reader, limit, seconds=45):
        self.reader,self.remaining,self.deadline=reader,limit,time.monotonic()+seconds
    def __call__(self,payload):
        if self.remaining<=0 or time.monotonic()>=self.deadline: raise ValueError('RESOURCE_BUDGET')
        self.remaining-=1
        return self.reader._info(payload)


class PublicTrades:
    """One continuous public consumer; financial fills remain REST-cursor owned."""
    def __init__(self, network, coins=('BTC','ETH','HYPE')):
        import threading
        from .stream_buffer import TradeBuffer
        self.network=validated_network(network)
        if not 1<=len(coins)<=4:raise ValueError('SUBSCRIPTION_LIMIT')
        for coin in coins:InstrumentId(network=self.network,symbol=coin)
        self.coins,self.socket=coins,None
        self.telemetry_id=uuid.uuid4().hex
        self.failures=0;self.retry_at=0;self.last_disconnect_reason=None
        self.buffer=TradeBuffer();self.stop=threading.Event();self.thread=None
        self.connected_at=None;self.reconnects=0;self.connections=0;self.messages_received=0;self.invalid_messages=0

    def _disconnect(self):
        sock,self.socket=self.socket,None
        if sock is not None:
            try:sock.close()
            except Exception:pass
        from core.hl_budget import configured
        budget=configured()
        if budget:budget.websocket(self.telemetry_id,'close')

    def _failed(self,exc):
        self.failures+=1
        self.retry_at=time.monotonic()+min(300,2**min(self.failures,8)+random.uniform(0,2))
        self.last_disconnect_reason=type(exc).__name__
        self._disconnect()

    def _consume(self):
        import websocket
        from core.hl_budget import configured
        heartbeat=0.
        try:
            while not self.stop.is_set():
                sock=self.socket
                if sock is None:return
                if time.monotonic()-heartbeat>=5:
                    budget=configured()
                    if budget:budget.websocket(self.telemetry_id,'heartbeat',len(self.coins))
                    heartbeat=time.monotonic()
                try:raw=sock.recv()
                except websocket.WebSocketTimeoutException:continue
                if not raw or len(raw)>262144:raise ValueError('STREAM_DISCONNECTED_OR_OVERSIZED')
                self.messages_received+=1
                message=json.loads(raw)
                if message.get('channel')!='trades':continue
                rows=message.get('data')
                if not isinstance(rows,list) or len(rows)>256:raise ValueError('STREAM_BATCH_INVALID')
                for row in rows:
                    if not isinstance(row,dict) or not isinstance(row.get('users'),list):continue
                    if not all(isinstance(x,str) for x in row['users']):continue
                    try:
                        if type(row.get('time')) is not int or row['time']<0:raise ValueError()
                        if any(not math.isfinite(float(row[k])) or float(row[k])<=0 for k in ('px','sz')):raise ValueError()
                    except (KeyError,ValueError,TypeError,OverflowError):
                        self.invalid_messages+=1;continue
                    self.buffer.put(row,self.stop)
                if rows:self.failures=0
        except Exception as exc:
            if not self.stop.is_set():self._failed(exc)

    def poll(self):
        import threading
        import websocket
        if self.stop.is_set():raise ValueError('PUBLIC_STREAM_CLOSED')
        if time.monotonic()<self.retry_at:raise ValueError('PUBLIC_STREAM_BACKOFF')
        if self.thread is None or not self.thread.is_alive():
            try:
                from core.hl_budget import configured
                budget=configured()
                if budget:budget.websocket(self.telemetry_id,'attempt')
                host='api.hyperliquid.xyz' if self.network=='MAINNET' else 'api.hyperliquid-testnet.xyz'
                self.socket=websocket.create_connection('wss://'+host+'/ws',timeout=2,enable_multithread=True)
                self.socket.settimeout(.5)
                self.connected_at=time.monotonic()
                self.reconnects+=int(self.connections>0);self.connections+=1
                if budget:budget.websocket(self.telemetry_id,'connect',0)
                for coin in self.coins:self.socket.send(packed({'method':'subscribe','subscription':{'type':'trades','coin':coin}}))
                if budget:budget.websocket(self.telemetry_id,'send',len(self.coins))
                self.thread=threading.Thread(target=self._consume,name='public-trades-reader',daemon=True)
                self.thread.start()
            except Exception as exc:
                self._failed(exc)
                raise ValueError('PUBLIC_STREAM_UNAVAILABLE') from None
        return self.buffer.take()

    def metrics(self):
        value=self.buffer.metrics()
        value.update(connection_age_ms=int((time.monotonic()-self.connected_at)*1000) if self.socket and self.connected_at else None,
            reconnects=self.reconnects,connected=self.socket is not None,messages_received=self.messages_received,
            messages_per_second=self.messages_received/max(1,time.monotonic()-self.buffer.started),invalid_messages=self.invalid_messages)
        return value

    def close(self):
        self.stop.set()
        with self.buffer.condition:self.buffer.condition.notify_all()
        self._disconnect()
        if self.thread is not None:self.thread.join(timeout=3)


class UserFillsStream:
    """Shared user-fills stream for the bounded active-leader watchlist.

    Public leader addresses are safe subscription identifiers; this stream has
    no signing or account authority.  It intentionally returns ``None`` once
    after a reconnect so the caller performs an incremental REST recovery
    before trusting the live cursor again.
    """
    def __init__(self, network, max_users=64, capacity=4096):
        import threading
        from collections import defaultdict, deque
        self.network=validated_network(network)
        # The watched set is position-owned leaders PLUS the active watchlist,
        # so a ceiling near the watchlist size drops the whole stream exactly
        # when the account is most active. The venue advertises 1000
        # subscriptions per connection; this stays far under that.
        self.max_users=max(1,min(1000,int(max_users)))
        # Per-user bound. An unpolled stream - lease held elsewhere, a run of
        # cycle errors - must not grow without limit; oldest rows go first and
        # the REST cursor recovers whatever was dropped.
        self.capacity=max(64,int(capacity))
        self.socket=None;self.thread=None;self.stop=threading.Event();self.lock=threading.RLock()
        self.users=set();self.buffers=defaultdict(deque);self.failures=0;self.retry_at=0
        self.connections=0;self.reconnects=0;self.messages_received=0;self.invalid_messages=0
        self.dropped=0
        self.needs_recovery=False;self.connected_at=None

    def _disconnect(self):
        sock,self.socket=self.socket,None
        if sock is not None:
            try:sock.close()
            except Exception:pass
        from core.hl_budget import configured
        budget=configured()
        if budget:budget.websocket('leader-fills','close')

    def _failed(self,exc):
        self.failures+=1
        self.retry_at=time.monotonic()+min(300,2**min(self.failures,8)+random.uniform(0,2))
        self.needs_recovery=True
        self._disconnect()

    @staticmethod
    def _rows(message):
        if message.get('channel') not in {'user','userFills'}:return None,None
        data=message.get('data')
        if not isinstance(data,dict):return None,None
        user=data.get('user')
        rows=data.get('fills')
        if rows is None:rows=data.get('userFills')
        return user,rows

    def _consume(self):
        import websocket
        try:
            while not self.stop.is_set():
                sock=self.socket
                if sock is None:return
                try:raw=sock.recv()
                except websocket.WebSocketTimeoutException:continue
                if not raw or len(raw)>262144:raise ValueError('STREAM_DISCONNECTED_OR_OVERSIZED')
                self.messages_received+=1
                message=json.loads(raw)
                user,rows=self._rows(message)
                if not user or not isinstance(rows,list):continue
                try:user=wallet(user)
                except ValueError:self.invalid_messages+=1;continue
                # Reconnect acks contain historical snapshots.  They are used
                # only for recovery detection; REST fills close the gap.
                if isinstance(message.get('data'),dict) and message['data'].get('isSnapshot'):
                    continue
                with self.lock:
                    if user not in self.users:continue
                    for row in rows:
                        if not isinstance(row,dict):self.invalid_messages+=1;continue
                        try:
                            stamp=row.get('time');px=float(row.get('px'));size=float(row.get('sz'))
                            if type(stamp) is not int or stamp<0 or not math.isfinite(px) or px<=0 or not math.isfinite(size) or size<=0:raise ValueError()
                            if row.get('side') not in {'A','B'} or not isinstance(row.get('coin'),str) or not row['coin']:raise ValueError()
                        except (TypeError,ValueError,OverflowError):self.invalid_messages+=1;continue
                        queue=self.buffers[user]
                        while len(queue)>=self.capacity:
                            # Dropping the oldest row is safe: it is only a
                            # latency shortcut, and the durable REST cursor
                            # still carries every fill identity.
                            queue.popleft();self.dropped+=1;self.needs_recovery=True
                        queue.append(dict(row))
                self.failures=0
        except Exception as exc:
            if not self.stop.is_set():self._failed(exc)

    def _ensure(self,users):
        import threading
        import websocket
        # Caller order is priority order - position-owned leaders first - so a
        # set larger than the ceiling keeps the subscriptions that matter most
        # rather than dropping the stream for everyone. Whoever is left out is
        # reported as unknown by poll() and recovered over REST.
        ordered=[]
        for u in users:
            address=wallet(u)
            if address not in ordered: ordered.append(address)
        requested=set(ordered[:self.max_users])
        if time.monotonic()<self.retry_at:raise ValueError('USER_STREAM_BACKOFF')
        old=set(self.users)
        fresh_connection=False
        if self.thread is None or not self.thread.is_alive() or self.socket is None:
            had_connection=self.connections>0
            fresh_connection=True
            from core.hl_budget import configured
            budget=configured()
            if budget:budget.websocket('leader-fills','attempt')
            host='api.hyperliquid.xyz' if self.network=='MAINNET' else 'api.hyperliquid-testnet.xyz'
            try:
                self.socket=websocket.create_connection('wss://'+host+'/ws',timeout=2,enable_multithread=True)
                self.socket.settimeout(.5)
            except Exception as exc:
                self._failed(exc)
                raise ValueError('USER_STREAM_UNAVAILABLE') from None
            self.connected_at=time.monotonic()
            self.reconnects+=int(had_connection);self.connections+=1
            if budget:budget.websocket('leader-fills','connect',0)
            with self.lock:self.users=requested
            self.thread=threading.Thread(target=self._consume,name='leader-fills-reader',daemon=True)
            self.thread.start()
            # EVERY fresh connection needs recovery, the first one included.
            # A subscription only delivers fills from the moment it is made,
            # while the stored cursor can be minutes or hours old; trusting an
            # empty first batch would silently swallow that whole window.
            self.needs_recovery=True
        removed=old-requested if not fresh_connection else set()
        added=requested if fresh_connection else requested-old
        sock=self.socket
        if sock is None:
            # The reader thread failed between the liveness check above and
            # here. Treat it as a disconnect rather than raising AttributeError.
            self._failed(ValueError('SOCKET_CLOSED'))
            raise ValueError('USER_STREAM_UNAVAILABLE')
        for user in sorted(removed):
            sock.send(packed({'method':'unsubscribe','subscription':{'type':'userFills','user':user}}))
        for user in sorted(added):
            sock.send(packed({'method':'subscribe','subscription':{'type':'userFills','user':user}}))
        with self.lock:self.users=requested
        from core.hl_budget import configured
        budget=configured()
        if budget:budget.websocket('leader-fills','send',len(removed)+len(added))

    def poll(self,users):
        if self.stop.is_set():raise ValueError('USER_STREAM_CLOSED')
        self._ensure(users)
        if self.needs_recovery:
            self.needs_recovery=False
            return None
        with self.lock:
            return {u:list(self.buffers.pop(u,())) for u in self.users}

    def metrics(self):
        with self.lock:depth=sum(len(v) for v in self.buffers.values())
        return {'connected':self.socket is not None,'connections':self.connections,'reconnects':self.reconnects,
            'messages_received':self.messages_received,'invalid_messages':self.invalid_messages,'queue_depth':depth,
            'dropped':self.dropped,'subscription_capacity':self.max_users,
            'watched_users':len(self.users),'connection_age_ms':int((time.monotonic()-self.connected_at)*1000) if self.connected_at else None}

    def close(self):
        self.stop.set();self._disconnect()
        if self.thread is not None:self.thread.join(timeout=3)


class WalletDiscoveryEngine:
    def __init__(self, path, network, policy=None, operations=None):
        from .discovery_operations import DiscoveryOperations
        self.network=validated_network(network)
        self.policy=policy or IntelligencePolicy()
        self.operations=operations or DiscoveryOperations()
        path=Path(path)
        if path.is_symlink() or path.parent.is_symlink(): raise ValueError('RESEARCH_PATH_UNSAFE')
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as existing:
                tables={r[0] for r in existing.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables and 'intelligence_records' not in tables:
                    raise ValueError('DEDICATED_RESEARCH_DATABASE_REQUIRED')
        self.store=Store(path)
        self.scope=Scope(tenant='public-intelligence',account='0x'+'0'*40,network=self.network)
        self.owner=uuid.uuid4().hex
        # Rolling taker imbalance per market, fed from the public trades the
        # discovery stream already delivers. It costs no request of its own.
        self.flow={}
        with self.store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS candidates(network TEXT,wallet TEXT,first_seen INTEGER,last_seen INTEGER,
                    status TEXT,next_eval INTEGER,cursor INTEGER,score REAL,confidence REAL,analysis TEXT,
                    PRIMARY KEY(network,wallet));
                CREATE TABLE IF NOT EXISTS intelligence_records(id TEXT PRIMARY KEY,network TEXT,kind TEXT,created INTEGER,body TEXT);
                CREATE INDEX IF NOT EXISTS candidate_due ON candidates(network,next_eval);
                CREATE INDEX IF NOT EXISTS intelligence_created ON intelligence_records(network,created);
                CREATE INDEX IF NOT EXISTS intelligence_actionable ON intelligence_records(network,kind);
                CREATE TABLE IF NOT EXISTS intelligence_health(network TEXT PRIMARY KEY,last_success INTEGER,last_attempt INTEGER,error TEXT,errors INTEGER);
                CREATE TABLE IF NOT EXISTS component_health(network TEXT,component TEXT,body TEXT NOT NULL,PRIMARY KEY(network,component));
                CREATE TABLE IF NOT EXISTS intelligence_lease(network TEXT PRIMARY KEY,owner TEXT,until_ms INTEGER);
                CREATE TABLE IF NOT EXISTS decision_links(event_id TEXT PRIMARY KEY,record_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observed_fills(network TEXT,wallet TEXT,fill_id TEXT,event_id TEXT,
                    PRIMARY KEY(network,wallet,fill_id));
                CREATE TABLE IF NOT EXISTS watch_epochs(network TEXT,wallet TEXT,started INTEGER,PRIMARY KEY(network,wallet));
                CREATE TABLE IF NOT EXISTS candidate_retirements(network TEXT,wallet TEXT,retired INTEGER,prior_status TEXT,reason TEXT,
                    PRIMARY KEY(network,wallet,retired));
                CREATE TABLE IF NOT EXISTS discovery_schedule(network TEXT PRIMARY KEY,turn INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS watch_scan_state(network TEXT,wallet TEXT,last_success INTEGER,last_attempt INTEGER,error TEXT,PRIMARY KEY(network,wallet));
                CREATE TABLE IF NOT EXISTS research_archive(id TEXT PRIMARY KEY,archived_ms INTEGER,reason TEXT);
                CREATE TABLE IF NOT EXISTS candidate_segments(network TEXT,wallet TEXT,sector TEXT,reason TEXT,checked_ms INTEGER,recheck_ms INTEGER,evidence TEXT,PRIMARY KEY(network,wallet));
                CREATE INDEX IF NOT EXISTS candidate_due ON candidates(network,status,next_eval,wallet);
                CREATE INDEX IF NOT EXISTS segment_due ON candidate_segments(network,recheck_ms);
                CREATE VIEW IF NOT EXISTS research_hot AS SELECT r.* FROM intelligence_records r WHERE NOT EXISTS (SELECT 1 FROM research_archive a WHERE a.id=r.id);
                CREATE TRIGGER IF NOT EXISTS research_no_update BEFORE UPDATE ON intelligence_records BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS research_no_delete BEFORE DELETE ON intelligence_records BEGIN SELECT RAISE(ABORT,'append only'); END;
            ''')

    def health_observation(self,component,now,*,error=None,details=None):
        """Telemetry only: a scan with no new trade is still a successful scan.

        Never substitutes a heartbeat for a price/fill exchange watermark.
        """
        with self.store.transaction() as db:
            row=db.execute('SELECT body FROM component_health WHERE network=? AND component=?',(self.network,component)).fetchone()
            old=json.loads(row[0]) if row else {}
            value=dict(last_success_ms=old.get('last_success_ms') if error else now,
                heartbeat_ms=now,error=error,error_count=old.get('error_count',0)+int(bool(error)),**(details or {}))
            db.execute('INSERT OR REPLACE INTO component_health VALUES(?,?,?)',(self.network,component,packed(value)))

    def _record(self,db,kind,body,now,instrument=None):
        record_id=identity([self.network,kind,body])
        old=db.execute('SELECT body FROM intelligence_records WHERE id=?',(record_id,)).fetchone()
        if old:
            if old[0]!=packed(body): raise ValueError('IDENTITY_COLLISION')
            return record_id
        if db.execute('SELECT COUNT(*) FROM research_hot').fetchone()[0]>=100000:
            # Logical hot/archive separation preserves original immutable rows,
            # IDs, replay cursors and all financial/decision references.
            eligible=db.execute("SELECT id FROM research_hot WHERE kind IN ('WALLET_DISCOVERED','LEADER_ANALYZED') AND created<? AND id NOT IN (SELECT record_id FROM decision_links) ORDER BY created,id LIMIT 20000",(now-7*DAY,)).fetchall()
            db.executemany('INSERT OR IGNORE INTO research_archive VALUES(?,?,?)',
                [(r['id'],now,'COLD_PUBLIC_RESEARCH') for r in eligible])
            # Protected audit evidence is never evicted. A full logical hot
            # index must not become a lifetime ceiling on wallet discovery.
        db.execute('INSERT INTO intelligence_records VALUES(?,?,?,?,?)',(record_id,self.network,kind,now,packed(body)))
        correlation=body.get('event_id') or body.get('event',{}).get('event_id') or record_id
        reference=AnalysisResult(instrument=instrument or InstrumentId(network=self.network,symbol='BTC'),
            correlation_id=correlation,created_ms=now,evidence_ids=(record_id,),conclusion='RESEARCH_ONLY')
        self.store.append_in(db,DomainEvent(event_id=record_id,event_type='LEADER_EVENT',correlation_id=correlation,
            scope=self.scope,event_ms=now,received_ms=now,payload=reference))
        return record_id

    def retire_candidates(self,now):
        """Cold public observations, not deletion or an account-inactivity claim.

        There is no total registry ceiling. Cold entries remain searchable and
        re-enter on fresh evidence; active/qualified/position-owned survive.
        """
        protected=set(getattr(self,'position_owned_leaders',()))
        with self.store.transaction() as db:
            rows=db.execute("SELECT wallet,status FROM candidates WHERE network=? AND status IN ('DISCOVERED','CANDIDATE','PROBATION','RETIRED') AND last_seen<? ORDER BY last_seen,wallet LIMIT 64",
                (self.network,now-self.operations.inactive_ms)).fetchall()
            retired=0
            for row in rows:
                if row['wallet'] in protected:continue
                db.execute('INSERT INTO candidate_retirements VALUES(?,?,?,?,?)',(self.network,row['wallet'],now,row['status'],'COLD_PUBLIC_OBSERVATION'))
                db.execute("UPDATE candidates SET status='ARCHIVED',next_eval=? WHERE network=? AND wallet=?",(now+self.operations.cold_recheck_ms,self.network,row['wallet']))
                retired+=1
            return retired

    def segment(self,address,sector,reason,evidence,now):
        from .discovery_operations import COLD
        if sector not in (*COLD,'NORMAL'):raise ValueError('RESEARCH_SEGMENT')
        wait=self.operations.bot_recheck_ms if sector=='HIGH_FREQUENCY' else self.operations.cold_recheck_ms
        with self.store.transaction() as db:
            old=db.execute('SELECT sector FROM candidate_segments WHERE network=? AND wallet=?',(self.network,address)).fetchone()
            db.execute('INSERT OR REPLACE INTO candidate_segments VALUES(?,?,?,?,?,?,?)',
                (self.network,address,sector,reason,now,now+wait,packed(evidence)))
            if sector!='NORMAL':db.execute('UPDATE candidates SET status=?,next_eval=? WHERE network=? AND wallet=?',(sector,now+wait,self.network,address))
            elif old and old['sector']!='NORMAL':db.execute("UPDATE candidates SET status='CANDIDATE',next_eval=? WHERE network=? AND wallet=?",(now,self.network,address))
            if old is None or old['sector']!=sector:self._record(db,'WALLET_SEGMENT_CHANGED',dict(wallet=address,sector=sector,reason=reason,evidence=evidence,at=now),now)

    def research_capacity(self,now):
        from .discovery_operations import resources
        state=resources(self.store.path,self.operations)
        with self.store.transaction() as db:
            count=db.execute("SELECT COUNT(*) FROM candidates WHERE network=? AND status IN ('ACTIVE','QUALIFIED') AND json_extract(analysis,'$.score.qualified')=1 AND json_extract(analysis,'$.score.computed_ms') BETWEEN ? AND ?",(self.network,now-self.policy.reevaluate_ms,now)).fetchone()[0]
        state.update(potential_count=count,potential_target=self.operations.potential_target,
            discovery_mode='IDLE_ONLY' if count>=self.operations.potential_target else 'BUILDING_POOL',
            registry_total_limit=None,operations=self.operations.model_dump(mode='json'))
        return state

    QUEUE_EXCLUDED="('ARCHIVED','INACTIVE','ZERO_SUPPORTED_CAPITAL','HIGH_FREQUENCY')"

    def candidate_queue(self,now):
        """Queue telemetry computed in SQL rather than by loading the queue.

        This ran every cycle and selected the ``analysis`` blob of every due
        candidate purely to count how many were non-null. At a few hundred
        wallets that is invisible; at the ten thousand this is meant to reach
        it is megabytes of JSON pulled out of the row store and discarded to
        produce four integers. The scan and the ordering are unchanged - only
        the payload is: a timestamp and a null test per row, both of which the
        candidate row already carries, so cost stops tracking analysis size.
        """
        with self.store.transaction() as db:
            rows=db.execute("SELECT next_eval,analysis IS NULL AS cheap FROM candidates "
                "WHERE network=? AND status NOT IN "+self.QUEUE_EXCLUDED+" AND next_eval<=? "
                "ORDER BY next_eval DESC",(self.network,now)).fetchall()
        size=len(rows)
        if not size:
            return dict(size=0,oldest_ms=0,p50_ms=0,p95_ms=0,cheap=0,deep=0,
                warning_ms=21600000,warning=False)
        # next_eval DESC is wait ASCENDING, which is the order the percentile
        # indices below are defined against.
        waits=[max(0,now-r['next_eval']) if now>=r['next_eval'] else now-r['next_eval'] for r in rows]
        cheap=sum(1 for r in rows if r['cheap'])
        return dict(size=size,oldest_ms=max(waits),
            p50_ms=waits[int((size-1)*.5)],p95_ms=waits[int((size-1)*.95)],
            cheap=cheap,deep=size-cheap,warning_ms=21600000,warning=waits[-1]>21600000)

    def deep_slots(self,capacity):
        """Deep-analysis slots this cycle, scaled by measured budget headroom.

        The base is what a constrained budget still guarantees so the queue
        never stalls; the ceiling is what an idle budget may use. Unknown or
        malformed headroom falls back to the base - spare capacity has to be
        proven before it is spent.
        """
        base=self.policy.deep_per_cycle
        if base<=0: return 0
        ceiling=max(base,self.policy.deep_max_per_cycle)
        headroom=(capacity or {}).get('headroom_fraction')
        if isinstance(headroom,bool) or not isinstance(headroom,(int,float)) or not math.isfinite(headroom):
            return base
        return max(base,min(ceiling,base+int(max(0.,min(1.,headroom))*(ceiling-base))))

    def scheduled_candidates(self,now,capacity=None):
        """Three fresh/quality slots then one oldest slot; durable fair rotation."""
        with self.store.transaction() as db:
            row=db.execute('SELECT turn FROM discovery_schedule WHERE network=?',(self.network,)).fetchone()
            turn=row[0] if row else 0
            protected=sorted(set(getattr(self,'position_owned_leaders',())))
            if len(protected)>32:raise ValueError('POSITION_WATCH_CAPACITY')
            slots=','.join('?' for _ in protected) or 'NULL'
            order='next_eval,wallet' if turn%4==3 else "CASE WHEN status='ACTIVE' THEN 0 WHEN wallet IN ("+slots+") THEN 1 WHEN status='QUALIFIED' THEN 2 ELSE 3 END,COALESCE(score,0) DESC,next_eval,wallet"
            restriction=" AND status NOT IN ('ARCHIVED','INACTIVE','ZERO_SUPPORTED_CAPITAL','HIGH_FREQUENCY')" if turn%8!=7 else ''
            if capacity and (not capacity['allow_research'] or (capacity['potential_count']>=self.operations.potential_target and not capacity['idle'])):
                restriction=" AND status IN ('ACTIVE','QUALIFIED')"
                if not capacity['allow_research']:return []
            rows=db.execute("SELECT wallet FROM candidates WHERE network=? AND next_eval<=?"+restriction+" ORDER BY "+order+' LIMIT ?',
                (self.network,now,*([] if turn%4==3 else protected),self.deep_slots(capacity))).fetchall()
            db.execute('INSERT OR REPLACE INTO discovery_schedule VALUES(?,?)',(self.network,turn+1))
            return rows

    def observe(self,trades,now,*,allow_new=True):
        if not isinstance(trades,list) or len(trades)>1280: raise ValueError('OBSERVATION_BOUND')
        addresses=set()
        for row in trades:
            if not isinstance(row,dict): continue
            if type(row.get('time')) is not int or not 0<=now-row['time']<=300000: continue
            users=row.get('users')
            if not isinstance(users,list) or len(users)!=2: continue
            for user in users:
                try: addresses.add(wallet(user))
                except ValueError: continue
        self.retire_candidates(now)
        # Bound work per cycle, not lifetime registry size. Archived records
        # keep identity; cold bot/zero-capital sectors never re-enter on a fill
        # alone and need their scheduled fresh evidence check.
        admitted=0
        with self.store.transaction() as db:
            for address in sorted(addresses):
                exists=db.execute('SELECT status FROM candidates WHERE network=? AND wallet=?',(self.network,address)).fetchone()
                if exists:
                    db.execute('UPDATE candidates SET last_seen=MAX(last_seen,?) WHERE network=? AND wallet=?',(now,self.network,address))
                    if exists['status']=='ARCHIVED':
                        retired=db.execute('SELECT MAX(retired) FROM candidate_retirements WHERE network=? AND wallet=?',(self.network,address)).fetchone()[0]
                        if allow_new and retired is not None and now-retired>=1800000 and admitted<self.operations.admissions_per_cycle:
                            db.execute("UPDATE candidates SET status='DISCOVERED',next_eval=? WHERE network=? AND wallet=?",(now,self.network,address))
                            admitted+=1
                elif allow_new and admitted<self.operations.admissions_per_cycle:
                    db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)',(self.network,address,now,now,'DISCOVERED',now,now,None,None,None))
                    self._record(db,'WALLET_DISCOVERED',{'wallet':address,'network':self.network,'observed_ms':now},now)
                    admitted+=1

    def analyze_one(self,address,info,now,clock=None):
        address=wallet(address)
        from .history import IncrementalHistory
        history=IncrementalHistory(self.store,self.network)
        from .discovery_operations import high_frequency,supported_account
        # One bounded recent sample before expensive pagination. Capped rows
        # establish only high-frequency lower bounds, never profitability.
        sample=info({'type':'userFillsByTime','user':address,'startTime':max(0,now-DAY),'endTime':now,'aggregateByTime':False})
        frequency=high_frequency(sample,now,self.operations)
        if frequency['suspected']:
            self.segment(address,'HIGH_FREQUENCY','HIGH_ORDER_RATE_NOT_COPY_TARGET',frequency,now)
            return None
        with self.store.transaction() as db:
            segment=db.execute('SELECT sector FROM candidate_segments WHERE network=? AND wallet=?',(self.network,address)).fetchone()
        # Reuse the exact sample in history retrieval; do not pay twice.
        cached=[sample]
        def screened_info(payload):
            if cached and payload.get('type')=='userFillsByTime' and payload.get('user')==address and payload.get('startTime')==max(0,now-DAY) and payload.get('endTime')==now:return cached.pop()
            return info(payload)
        cheap=history.fetch(screened_info,address,max(0,now-DAY),now,2,'cheap')
        # Current supported capital is checked before expensive deep history.
        evidence=supported_account(info,address,now,clock=clock)
        if evidence['zero_supported_capital']:
            self.segment(address,'ZERO_SUPPORTED_CAPITAL','NO_CAPITAL_IN_SUPPORTED_POOLS',evidence,now);return None
        with self.store.transaction() as db:
            observed=db.execute('SELECT last_seen FROM candidates WHERE network=? AND wallet=?',(self.network,address)).fetchone()
        if not cheap and not evidence['open_positions'] and observed and now-observed['last_seen']>=self.operations.inactive_ms:
            self.segment(address,'INACTIVE','NO_RECENT_FILLS_OR_POSITIONS',evidence,now);return None
        self.segment(address,'NORMAL','FRESH_RECHECK_PASSED',evidence,now)
        if len(cheap)<self.policy.cheap_min_fills:
            with self.store.transaction() as db:
                db.execute("UPDATE candidates SET status='CANDIDATE',next_eval=? WHERE network=? AND wallet=?",(now+self.policy.reevaluate_ms,self.network,address))
            return None
        fills=filter_perp_fills(history.fetch(info,address,max(0,now-180*DAY),now,self.policy.history_requests,'deep'))
        windows=reports(fills,now)
        ranked=score(address,self.network,windows,now,self.policy)
        analysis={'wallet':address,'network':self.network,'computed_ms':now,'score':ranked.model_dump(mode='json'),
            'windows':windows,'history_method':'incremental-validated-fill-history-v1','equity_drawdown_pct':None,
            'funding_included':False,'not_a_profit_probability':True}
        with self.store.transaction() as db:
            old=db.execute('SELECT status FROM candidates WHERE network=? AND wallet=?',(self.network,address)).fetchone()
            status=('ACTIVE' if old and old['status']=='ACTIVE' else 'QUALIFIED') if ranked.qualified else 'PROBATION'
            db.execute('UPDATE candidates SET status=?,next_eval=?,score=?,confidence=?,analysis=? WHERE network=? AND wallet=?',
                (status,now+self.policy.reevaluate_ms,ranked.score,ranked.confidence,packed(analysis),self.network,address))
            self._record(db,'LEADER_ANALYZED',analysis,now)
        return analysis

    def promote(self,now):
        with self.store.transaction() as db:
            winners=db.execute("SELECT wallet,status FROM candidates WHERE network=? AND status IN ('QUALIFIED','ACTIVE') AND next_eval>? AND json_extract(analysis,'$.score.computed_ms') BETWEEN ? AND ? ORDER BY score DESC,confidence DESC,wallet LIMIT ?",(self.network,now,now-self.policy.reevaluate_ms,now,self.policy.watch_limit)).fetchall()
            selected={r['wallet'] for r in winners}
            active=db.execute("SELECT wallet FROM candidates WHERE network=? AND status='ACTIVE'",(self.network,)).fetchall()
            for row in active:
                if row['wallet'] not in selected:
                    db.execute("UPDATE candidates SET status='PROBATION' WHERE network=? AND wallet=?",(self.network,row['wallet']))
                    self._record(db,'LEADER_DEGRADED',{'wallet':row['wallet'],'at':now},now)
            for row in winners:
                if row['status']!='ACTIVE':
                    db.execute("UPDATE candidates SET status='ACTIVE',cursor=? WHERE network=? AND wallet=?",(now,self.network,row['wallet']))
                    db.execute('INSERT OR REPLACE INTO watch_epochs VALUES(?,?,?)',(self.network,row['wallet'],now))
                    self._record(db,'LEADER_PROMOTED',{'wallet':row['wallet'],'at':now},now)

    def detect(self,address,info,now,*,position_owned=False,fills=None):
        with self.store.transaction() as db:
            saved=db.execute("SELECT cursor,status FROM candidates WHERE network=? AND wallet=?",(self.network,address)).fetchone()
        if not saved or (saved['status']!='ACTIVE' and not position_owned): return []
        cursor=saved['cursor']
        if now<cursor: raise ValueError('CLOCK_REGRESSION')
        # Re-read a bounded overlap for delayed visibility and same-millisecond
        # fills when no live user-fills stream is available. A durable fill
        # identity, not wall-clock cursor alone, dedupes. Live stream rows are
        # already incremental and therefore must not trigger another REST read.
        if fills is None:
            fills=filter_perp_fills(fetch_fills(info,address,max(0,cursor-60000),now,now_ms=now,max_requests=4))
        else:
            fills=filter_perp_fills([f for f in fills if isinstance(f,dict) and type(f.get('time')) is int and f['time']<=now])
        with self.store.transaction() as db:
            epoch=db.execute('SELECT started FROM watch_epochs WHERE network=? AND wallet=?',(self.network,address)).fetchone()
            if epoch is None: raise ValueError('WATCH_EPOCH_UNKNOWN')
            activation=epoch[0]
            keys=[identity([self.network,address,f['coin'],f['tid'],f['time']]) for f in fills]
            seen={key for key in keys if db.execute('SELECT 1 FROM observed_fills WHERE network=? AND wallet=? AND fill_id=?',(self.network,address,key)).fetchone()}
        events=[]
        for f in fills:
            key=identity([self.network,address,f['coin'],f['tid'],f['time']])
            if f['time']<=activation or key in seen: continue
            before=Decimal(str(f['startPosition'])); size=Decimal(str(f['sz']))
            if not before.is_finite() or not size.is_finite() or size<=0 or f['side'] not in {'A','B'}: raise ValueError('FILL_INVALID')
            after=before+(size if f['side']=='B' else -size)
            action='OPEN' if before==0 else 'CLOSE' if after==0 else 'REVERSE' if before*after<0 else 'ADD' if abs(after)>abs(before) else 'REDUCE'
            coin=f['coin']; dex=coin.split(':')[0] if ':' in coin else ''
            events.append(LeaderTradeEvent(event_id=key,wallet=address,instrument=InstrumentId(network=self.network,dex=dex,symbol=coin.split(':')[-1]),
                action=action,side='BUY' if f['side']=='B' else 'SELL',size=float(size),before_size=float(before),after_size=float(after),
                exchange_ms=f['time'],received_ms=now,fill_id=str(f['tid'])))
        with self.store.transaction() as db:
            latest=db.execute('SELECT cursor FROM candidates WHERE network=? AND wallet=?',(self.network,address)).fetchone()[0]
            if latest!=cursor: raise ValueError('CURSOR_CONFLICT')
            for event in events:
                self._record(db,'LEADER_TRADE',event.model_dump(mode='json'),now,event.instrument)
                db.execute('INSERT INTO observed_fills VALUES(?,?,?,?)',(self.network,address,event.event_id,event.event_id))
            db.execute('UPDATE candidates SET cursor=? WHERE network=? AND wallet=?',(now,self.network,address))
        return events

    FLOW_WINDOW_MS=300000
    FLOW_PER_MARKET=512

    def record_flow(self,trades,now):
        """Fold the public trade batch into a per-market taker-imbalance window.

        These prints already arrive over the discovery websocket, so a real
        order-flow reading costs nothing extra; the previous placeholder agent
        returned WAIT forever and contributed nothing to any decision. Only
        the aggressor side, notional and timestamp are kept - never a wallet,
        and never anything that could be mistaken for our own fills.
        """
        for row in trades or ():
            try:
                coin=str(row['coin']);stamp=row['time'];side=row['side']
                price=float(row['px']);size=float(row['sz'])
            except (KeyError,TypeError,ValueError): continue
            if type(stamp) is not int or side not in ('A','B'): continue
            if not (math.isfinite(price) and math.isfinite(size)) or price<=0 or size<=0: continue
            if not 0<=now-stamp<=self.FLOW_WINDOW_MS: continue
            window=self.flow.setdefault(coin,deque())
            window.append((stamp,price*size if side=='B' else -price*size,price*size))
            while len(window)>self.FLOW_PER_MARKET: window.popleft()
        if len(self.flow)>256:
            for coin in [c for c,w in self.flow.items() if not w or now-w[-1][0]>self.FLOW_WINDOW_MS]:
                self.flow.pop(coin,None)

    def flow_window(self,coin,now):
        """Summary of the current window, or None when there is nothing to say."""
        window=self.flow.get(str(coin))
        if not window: return None
        while window and now-window[0][0]>self.FLOW_WINDOW_MS: window.popleft()
        if not window: return None
        signed=math.fsum(x[1] for x in window);total=math.fsum(x[2] for x in window)
        if not (math.isfinite(signed) and math.isfinite(total)) or total<=0: return None
        return {'prints':len(window),'notional':total,'imbalance':max(-1.,min(1.,signed/total)),
                'exchange_ms':window[-1][0],'window_ms':self.FLOW_WINDOW_MS}

    def expired(self,event,now):
        """Past the window in which this action could still be acted on."""
        return not 0<=now-event.exchange_ms<=self.policy.signal_window_ms(event.action)

    def research_slots(self,depth):
        """Live market researches this cycle, scaled by how backed up we are.

        A fixed four per thirty-second cycle is eight events a minute. Eight
        watched leaders trading in bursts exceed that, the queue grows, and
        everything in it ages past its window - which is the mechanism that
        made the median signal 67 seconds old and the worst twelve minutes.
        Expired events are cleared without a market read and are not counted
        here, so this bound covers only work that can still produce an order.
        """
        base=self.policy.research_per_cycle
        ceiling=max(base,self.policy.research_max_per_cycle)
        if depth<=base: return base
        return min(ceiling,depth)

    def research(self,event,leader,info,now,clock=None):
        with self.store.transaction() as db:
            existing=db.execute('SELECT r.body FROM decision_links l JOIN intelligence_records r ON r.id=l.record_id WHERE l.event_id=?',(event.event_id,)).fetchone()
        if existing: return ConsensusDecision.model_validate(json.loads(existing[0])['consensus'])
        coin=event.instrument.market_key.split('|')[0]
        if self.expired(event,now):
            # Two market reads to reach a foregone refusal is throughput the
            # queue cannot spare - and the queue's own depth is why this event
            # expired. Score it on the evidence actually held (none), which
            # yields STALE_SIGNAL honestly, and let it leave the queue.
            book,candles=({},[])
        else:
            book=info({'type':'l2Book','coin':coin})
            candles=info({'type':'candleSnapshot','req':{'coin':coin,'interval':'15m','startTime':now-64*900000,'endTime':now}})
        if clock is not None: now=clock()
        if not isinstance(candles,list): candles=[]
        candles=[c for c in candles if type(c.get('T')) is int and c['T']<=now]
        # Carried in the body so the actionable replay scores the SAME tape the
        # research decision saw, instead of whatever the window holds later.
        flow=self.flow_window(coin,now)
        outputs=evaluate(event,leader,candles,book,now,self.policy,flow=flow)
        decision=consensus(event,outputs,now,self.policy)
        body={'event':event.model_dump(mode='json'),'leader':leader.model_dump(mode='json'),'policy':self.policy.model_dump(mode='json'),
            'candles':candles,'book':book,'flow':flow,'agents':[a.model_dump(mode='json') for a in outputs],
            'consensus':decision.model_dump(mode='json'),
            'mode':'OBSERVE','authorization':'NONE','executed':False}
        with self.store.transaction() as db:
            admission=db.execute('SELECT status FROM candidates WHERE network=? AND wallet=?',(self.network,event.wallet)).fetchone()
            body['admission_allowed']=bool(admission and admission['status']=='ACTIVE')
            if db.execute('SELECT 1 FROM decision_links WHERE event_id=?',(event.event_id,)).fetchone(): return decision
            record_id=self._record(db,'DECISION',body,now,event.instrument)
            db.execute('INSERT INTO decision_links VALUES(?,?)',(event.event_id,record_id))
        return decision

    def cycle(self,reader,trades,now,clock=None,on_decision=None,leader_stream=None,pressure=None):
        if reader.network!=self.network: raise ValueError('NETWORK_MISMATCH')
        clock=clock or (lambda:now)
        with self.store.transaction() as db:
            lease=db.execute('SELECT owner,until_ms FROM intelligence_lease WHERE network=?',(self.network,)).fetchone()
            if lease and lease['until_ms']>now: return False
            db.execute('INSERT OR REPLACE INTO intelligence_lease VALUES(?,?,?)',(self.network,self.owner,now+120000))
        info=RequestBudget(reader,self.policy.request_limit)
        # Fold the batch in before research runs, so a decision made this cycle
        # scores the tape that arrived with it rather than the previous window.
        try: self.record_flow(trades,now)
        except Exception: pass
        errors=[]
        try:
            with self.store.transaction() as db:
                active=db.execute("SELECT wallet,analysis FROM candidates WHERE network=? AND status='ACTIVE' ORDER BY wallet LIMIT ?",(self.network,self.policy.watch_limit)).fetchall()
            lifecycle=set(getattr(self,'position_owned_leaders',()))
            if len(lifecycle)>32: raise ValueError('POSITION_WATCH_CAPACITY')
            # Existing episodes have priority; demotion never ends their reads.
            watched=sorted(lifecycle)+[r['wallet'] for r in active if r['wallet'] not in lifecycle]
            with self.store.transaction() as db:
                scan_state={r['wallet']:dict(r) for r in db.execute('SELECT * FROM watch_scan_state WHERE network=?',(self.network,))}
            watched.sort(key=lambda address:(address not in lifecycle,scan_state.get(address,{}).get('last_success') or 0,address))
            leader_fills=None
            if leader_stream is not None and (watched or getattr(leader_stream,'users',())):
                try:
                    # A reconnect returns ``None`` once and deliberately
                    # selects the existing REST path for gap recovery.
                    leader_fills=leader_stream.poll(watched)
                except Exception:
                    errors.append('LEADER_STREAM_UNAVAILABLE')
            elif leader_stream is not None:
                leader_fills={}
            scanned=0
            for address in watched:
                error=None
                try:
                    # Position-owned monitoring is P1; active-watchlist
                    # detection is actionable P2 and may use the reserved
                    # 1050-1180 band. Historical discovery/deep analysis
                    # remains background and cannot consume that reserve.
                    with priority_scope('position_owned.detect',1) if address in lifecycle else priority_scope('service.detect',2):
                        # A missing entry means UNKNOWN, not "no fills": an
                        # address the stream does not cover must fall back to
                        # the REST cursor, never be treated as inactive.
                        self.detect(address,info,clock(),position_owned=address in lifecycle,
                            fills=(leader_fills.get(address) if leader_fills is not None else None))
                    scanned+=1
                except Exception as exc:
                    error='BUDGET_DEFERRED' if isinstance(exc,BudgetUnavailable) or isinstance(exc.__cause__,BudgetUnavailable) else type(exc).__name__
                    errors.append('WATCHLIST_OR_ANALYSIS_UNAVAILABLE')
                with self.store.transaction() as db:
                    old=scan_state.get(address,{})
                    db.execute('INSERT OR REPLACE INTO watch_scan_state VALUES(?,?,?,?,?)',
                        (self.network,address,old.get('last_success') if error else clock(),clock(),error))
            with self.store.transaction() as db:
                stamps={r['wallet']:r['last_success'] for r in db.execute('SELECT wallet,last_success FROM watch_scan_state WHERE network=?',(self.network,))}
            completed=min((stamps.get(a) or 0 for a in watched),default=clock())
            stream_details=leader_stream.metrics() if leader_stream is not None else {}
            for component in ('watchlist','leader_detection'):
                self.health_observation(component,clock(),error='WATCH_SCAN_INCOMPLETE' if scanned<len(watched) else None,
                    details={'watched':len(watched),'scanned':scanned,'last_completed_cycle_ms':completed or None,
                        'scan_lag_ms':max(0,clock()-completed) if completed else None,
                        'fill_stream':stream_details})
            with self.store.transaction() as db:
                # Exits first, then newest entries. Strict insertion order let a
                # backlog of dead entries starve a fresh exit for cycles, and an
                # exit is the one action whose lateness costs a position.
                queue=db.execute("SELECT r.body,c.analysis FROM intelligence_records r "
                    "JOIN candidates c ON c.network=r.network AND c.wallet=json_extract(r.body,'$.wallet') "
                    "LEFT JOIN decision_links l ON l.event_id=json_extract(r.body,'$.event_id') "
                    "WHERE r.network=? AND r.kind='LEADER_TRADE' AND l.event_id IS NULL "
                    "ORDER BY CASE WHEN json_extract(r.body,'$.action') IN ('REDUCE','CLOSE') THEN 0 ELSE 1 END,"
                    "json_extract(r.body,'$.exchange_ms') DESC LIMIT ?",
                    (self.network,self.policy.research_scan_limit)).fetchall()
            slots=self.research_slots(len(queue));cleared=0;researched=0
            for queued in queue:
                try:
                    event=LeaderTradeEvent.model_validate_json(queued['body'])
                    # Expired events are cleared for free, so they neither
                    # consume a slot nor keep the queue growing behind them.
                    dead=self.expired(event,clock())
                    if not dead and researched>=slots: continue
                    leader=LeaderScore.model_validate(json.loads(queued['analysis'])['score'])
                    self.research(event,leader,info,clock(),clock=clock)
                    if dead: cleared+=1
                    else:
                        researched+=1
                        if on_decision is not None:on_decision()
                except Exception: errors.append('RESEARCH_DEFERRED')
            self.health_observation('research_queue',clock(),details={'depth':len(queue),'slots':slots,
                'researched':researched,'expired_cleared':cleared,
                'scan_limit':self.policy.research_scan_limit})
            capacity=self.research_capacity(clock())
            # Backpressure: while the public buffer is backing up, stop
            # admitting NEW wallets. Discovery is the only optional consumer of
            # that queue, and its lag is paid by the lifecycle reads that hold
            # money. Existing candidates and every financial path continue.
            share=(pressure or {}).get('fraction')
            backpressure=(isinstance(share,(int,float)) and not isinstance(share,bool)
                          and math.isfinite(share) and share>=self.operations.backpressure_fraction)
            self.observe(trades,now,allow_new=capacity['allow_research'] and not backpressure and
                (capacity['potential_count']<self.operations.potential_target or capacity['idle']))
            self.health_observation('discovery',clock(),details={'queue':self.candidate_queue(clock()),
                'deep_slots':self.deep_slots(capacity),'stream_pressure':pressure,
                'admission_paused':bool(backpressure),**capacity})
            pending=self.scheduled_candidates(now,capacity)
            for row in pending:
                try:
                    analysis=self.analyze_one(row['wallet'],info,clock(),clock=clock)
                    self.health_observation('deep_analysis',clock(),details={'queue_checked':True,'screened':True,'analysis_completed':analysis is not None})
                except Exception as exc:
                    errors.append('HISTORY_INCOMPLETE')
                    causes=[];cause=exc
                    while cause is not None and len(causes)<4:
                        # Exact history-library reasons contain no keys/account data.
                        from core.fill_history import HistoryIncomplete
                        causes.append(str(cause) if isinstance(cause,(HistoryIncomplete,BudgetUnavailable)) else type(cause).__name__)
                        cause=cause.__cause__
                    self.health_observation('deep_analysis',clock(),error='HISTORY_INCOMPLETE',details={'queue_checked':True,'causes':causes})
                    with self.store.transaction() as db:
                        # Transient missing evidence is not disqualification;
                        # cold sectors remain cold, and qualify only on proof.
                        db.execute("UPDATE candidates SET next_eval=? WHERE network=? AND wallet=?",(now+60000,self.network,row['wallet']))
            if not pending:self.health_observation('deep_analysis',clock(),details={'idle':True,'queue_checked':True})
            self.promote(clock())
        except Exception: errors.append('DISCOVERY_UNAVAILABLE')
        finally:
            with self.store.transaction() as db:
                old=db.execute('SELECT last_success,errors FROM intelligence_health WHERE network=?',(self.network,)).fetchone()
                db.execute('INSERT OR REPLACE INTO intelligence_health VALUES(?,?,?,?,?)',
                    (self.network,clock() if not errors else (old['last_success'] if old else None),now,','.join(sorted(set(errors))) or None,(old['errors'] if old else 0)+len(errors)))
                db.execute('DELETE FROM intelligence_lease WHERE network=? AND owner=?',(self.network,self.owner))
        return not errors

    def snapshot(self,now):
        with self.store.transaction() as db:
            health=db.execute('SELECT * FROM intelligence_health WHERE network=?',(self.network,)).fetchone()
            leaders=db.execute("SELECT wallet,status,score,confidence,last_seen,analysis FROM candidates WHERE network=? AND status!='ARCHIVED' ORDER BY score DESC,wallet LIMIT 32",(self.network,)).fetchall()
            counts={r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM candidates WHERE network=? GROUP BY status',(self.network,))}
        healthy=bool(health and health['last_success'] is not None and 0<=now-health['last_success']<120000 and not health['error'])
        return {'network':self.network,'mode':'OBSERVE','execution_enabled':False,'health':'HEALTHY' if healthy else 'DEGRADED',
            'last_success':health['last_success'] if health else None,'last_error':health['error'] if health else 'WORKER_NOT_STARTED',
            'counts':counts,'leaders':[{**dict(r),'analysis':json.loads(r['analysis']) if r['analysis'] else None} for r in leaders],
            'policy':self.policy.model_dump(mode='json')}

    def records(self,after=0,limit=50):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100: raise ValueError('QUERY_BOUND')
        with self.store.transaction() as db:
            rows=db.execute('SELECT rowid AS seq,id,kind,created,body FROM intelligence_records WHERE network=? AND rowid>? ORDER BY rowid LIMIT ?',
                (self.network,after,limit)).fetchall()
        return [{**dict(r),'body':json.loads(r['body'])} for r in rows]


def replay_decision(record):
    """Pure analytical replay; intentionally no store, adapter or grant parameter."""
    policy=IntelligencePolicy.model_validate(record['policy'])
    event=LeaderTradeEvent.model_validate(record['event'])
    leader=LeaderScore.model_validate(record['leader'])
    now=record['consensus']['created_ms']
    outputs=evaluate(event,leader,record['candles'],record['book'],now,policy,flow=record.get('flow'))
    return consensus(event,outputs,now,policy).model_dump(mode='json')
