"""Host-shared Hyperliquid transport accounting and bounded public-read cache.

Official limits verified 2026-09-09. Response block rounding is conservatively
ceiling-rounded (docs don't specify rounding). This is measured client usage,
not an exchange-issued usage counter. All processes share one owner-only DB.
No retries, no account/history/order-response cache, no stale timestamp renewal.

The public market cache is deliberately narrow. It coalesces identical
read-only requests from the API, intelligence worker and discovery process so
one busy minute is spent on useful evidence rather than duplicate snapshots.
Financial/account reads and every ``/exchange`` call always cross the network.
"""
import hashlib
import inspect
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit
import requests

LIGHT={'l2Book','allMids','clearinghouseState','orderStatus','spotClearinghouseState','exchangeStatus'}
LISTS={'recentTrades','historicalOrders','userFills','userFillsByTime','fundingHistory','userFunding',
       'nonUserFundingUpdates','twapHistory','userTwapSliceFills','userTwapSliceFillsByTime',
       'delegatorHistory','delegatorRewards','validatorStats'}
META={'meta','spotMeta','perpDexs'}

# Only public, bounded snapshots are eligible. TTLs are short enough for UI
# freshness and long enough to collapse the fan-out of agents/read models.
# Nothing account-, order- or history-shaped may ever be added to this map.
READ_CACHE_TTL={'allMids':1.0,'l2Book':1.0,'metaAndAssetCtxs':2.0,'candleSnapshot':5.0,
                'meta':300.0,'spotMeta':300.0,'perpDexs':300.0,'exchangeStatus':2.0}


def _read_cache_payload(endpoint,payload):
    """Canonicalize only closed-candle windows for short read coalescing."""
    if endpoint!='candleSnapshot' or not isinstance(payload,dict):return payload
    req=payload.get('req')
    if not isinstance(req,dict):return payload
    intervals={'1m':60000,'5m':300000,'15m':900000,'1h':3600000,'4h':14400000,'1d':86400000}
    interval=intervals.get(req.get('interval'))
    try:start=int(req['startTime']);end=int(req['endTime'])
    except (KeyError,TypeError,ValueError):return payload
    if not interval or end<start:return payload
    # A moving ``endTime=now`` should not create a new upstream request every
    # second. Closed bars are stable until the next interval boundary.
    bucket=(end//interval)*interval
    bars=max(1,(end-start)//interval)
    normalized=dict(req,startTime=bucket-bars*interval,endTime=bucket)
    return dict(payload,req=normalized)
# Hyperliquid's documented IP budget is 1200 weighted units/minute. Plan no
# more than 1150: the 150 units between the elevated band and this ceiling are
# a critical reserve that only P0 execution/reconciliation may reach, so
# discovery and history work can never crowd out order safety.
NORMAL_CEILING=850
ELEVATED_CEILING=1000
HARD_PLANNED_CEILING=1150
FAIR_CONTENTION_FRACTION=.5  # Scheduling only; never raises any band ceiling.
HOSTS={'api.hyperliquid.xyz','api.hyperliquid-testnet.xyz'}
_priority=ContextVar('hl_resource_priority',default=None)


@contextmanager
def priority_scope(source,priority):
    token=_priority.set((source,priority))
    try:yield
    finally:_priority.reset(token)


def weights(endpoint,payload,response=None,maximum=False):
    if endpoint=='exchange':
        action=payload.get('action',{})
        batch=max([len(v) for v in action.values() if isinstance(v,list)]+[0])
        return 1+batch//40
    base=2 if endpoint in LIGHT else 60 if endpoint=='userRole' else 20
    if endpoint in LISTS or endpoint=='candleSnapshot':
        if endpoint == 'candleSnapshot' and maximum:
            # A planned candleSnapshot is bounded by the requested interval
            # and time range.  Reserving the protocol maximum (5,000 rows)
            # for every Home chart made a valid 24h/15m read look like a
            # 1180-budget breach even though the exchange could return only
            # ~97 rows.  Keep the conservative 5,000 cap for malformed
            # requests, but charge the actual bounded request shape.
            req = payload.get('req', {}) if isinstance(payload, dict) else {}
            interval_ms = {'1m': 60_000, '5m': 300_000, '15m': 900_000,
                           '1h': 3_600_000, '4h': 14_400_000,
                           '1d': 86_400_000}
            interval = interval_ms.get(req.get('interval'))
            try:
                start, end = float(req.get('startTime')), float(req.get('endTime'))
            except (TypeError, ValueError):
                start = end = None
            if interval and start is not None and end is not None and math.isfinite(start) and math.isfinite(end) and end >= start:
                n = min(5000, max(1, math.ceil((end - start) / interval) + 1))
            else:
                n = 5000
        else:
            n=(2000 if endpoint in
               {'userFills','userFillsByTime','historicalOrders','recentTrades'} else 10000) if maximum else len(response) if isinstance(response,list) else 0
        base+=math.ceil(n/(60 if endpoint=='candleSnapshot' else 20))
    return base


def origin():
    if _priority.get() is not None:return _priority.get()
    frames=[];f=inspect.currentframe().f_back
    try:
        for _ in range(30):
            if f is None:break
            frames.append((Path(f.f_code.co_filename).stem,f.f_code.co_name));f=f.f_back
    finally:del f
    for name,priority in [('recover',0),('reconcile_order',0),('recover_pending_manual_leader',0),
        ('_cycle',1),('execute',0),('research',2),('detect',2),('analyze_one',5),('analyse_wallet',6)]:
        for file,fn in frames:
            if fn==name:return file+'.'+fn,priority
    for file,fn in frames:
        # User/account/position reads are P1. Generic metadata and cold data
        # paths stay background so they cannot consume the critical reserve.
        if file in {'server','manual_copy_worker','capital_snapshot'}:return file+'.'+fn,1
        if file in {'hyperliquid','data'}:return file+'.'+fn,3
    return 'sdk_metadata',3


class BudgetUnavailable(RuntimeError):pass


def band_ceilings(soft,hard):
    """Return normal/elevated/critical ceilings for one persisted policy.

    ``soft`` and ``hard`` are retained for compatibility with existing
    operators/read models.  The middle band is derived, which keeps old
    custom test or operator limits coherent while the default policy is
    850/1050/1180.
    """
    hard=max(1,int(hard));normal=min(hard,max(1,int(soft)))
    # Small operator/test limits are intentionally respected as a single
    # band; only the shipped 850/1180 policy expands into 1050 elevated.
    elevated=normal if normal<NORMAL_CEILING else min(hard,max(normal,ELEVATED_CEILING))
    return normal,elevated,hard


class Budget:
    def __init__(self,path):
        self.path=str(path)
        self._retention_at=0.
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS limits(id INTEGER PRIMARY KEY,soft INTEGER,hard INTEGER,enforce INTEGER,cooldown REAL);
              INSERT OR IGNORE INTO limits VALUES(1,850,1180,1,0);
              CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,at REAL,service TEXT,source TEXT,endpoint TEXT,priority INTEGER,weight INTEGER,status INTEGER,elapsed REAL,items INTEGER);
              CREATE INDEX IF NOT EXISTS request_time ON requests(at);
              CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY,value INTEGER);
              CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,body TEXT,expires REAL,lease REAL);
              CREATE TABLE IF NOT EXISTS read_cache(key TEXT PRIMARY KEY,body TEXT,expires REAL,lease REAL);
              CREATE TABLE IF NOT EXISTS ws(id TEXT PRIMARY KEY,pid INTEGER,heartbeat REAL,subscriptions INTEGER);
              CREATE TABLE IF NOT EXISTS ws_events(at REAL,kind TEXT,n INTEGER);
              CREATE TABLE IF NOT EXISTS admission_waiters(id TEXT PRIMARY KEY,service TEXT,source TEXT,priority INTEGER,cost INTEGER,enqueued REAL,last_seen REAL);
            ''')
            # Migrate only the exact legacy defaults. Operator-tuned values
            # must not be overwritten by a process restart.
            db.execute('UPDATE limits SET soft=?,hard=? WHERE id=1 AND soft=600 AND hard=840',
                       (NORMAL_CEILING,HARD_PLANNED_CEILING))
            # band_ceilings() reads the PERSISTED hard limit, so lowering the
            # constant alone would leave a live database planning to 1180.
            # Migrate that one exact shipped default; anything an operator
            # tuned by hand is left alone, exactly as above.
            db.execute('UPDATE limits SET hard=? WHERE id=1 AND hard=1180',(HARD_PLANNED_CEILING,))
        if os.name!='nt':os.chmod(path,0o600)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path,timeout=2)) as db:
            with db:yield db

    def count(self,name):
        with self.db() as db:db.execute('INSERT INTO counters VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1',(name,))

    def metadata_acquire(self,key):
        """Only public instrument metadata. Fixed 30s TTL, no sliding renewal.

        A cross-process lease coalesces constructor calls. Waiting is bounded;
        an occupied lease defers rather than launching duplicate upstream work.
        Account, prices, history, orders and exchange actions NEVER enter here.
        """
        deadline=time.monotonic()+1
        while True:
            now=time.time()
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT body,expires,lease FROM metadata WHERE key=?',(key,)).fetchone()
                if row and row[0] is not None and row[1]>now:
                    result=('hit',row[0]);break
                if not row or row[2]<=now:
                    lease=now+30
                    db.execute('INSERT INTO metadata VALUES(?,NULL,0,?) ON CONFLICT(key) DO UPDATE SET lease=excluded.lease',(key,lease))
                    return 'owner',lease
            if time.monotonic()>=deadline:
                self.count('metadata_coalesced_deferred')
                raise BudgetUnavailable('HL_METADATA_INFLIGHT')
            time.sleep(.025)
        self.count('metadata_cache_hits')
        return result

    def read_acquire(self,key,ttl):
        """Return a cached public snapshot or a short cross-process lease.

        A waiter never launches a duplicate upstream request. If the owner
        disappears, its lease expires and the next caller becomes the owner.
        The body is JSON only; the endpoint allow-list above is what keeps
        credentials, account state and order payloads out of this table.
        """
        deadline=time.monotonic()+1.5
        while True:
            now=time.time();hit=None
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT body,expires,lease FROM read_cache WHERE key=?',(key,)).fetchone()
                if row and row[0] is not None and row[1]>now:
                    hit=row[0]
                elif not row or row[2]<=now:
                    lease=now+max(5.,float(ttl)*4.)
                    db.execute('INSERT INTO read_cache VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET body=NULL,expires=0,lease=excluded.lease',(key,None,0,lease))
                    return 'owner',lease
            # Counting opens a second connection; never do it inside the
            # exclusive transaction above (important on SQLite/WAL).
            if hit is not None:
                self.count('read_cache_hits')
                return 'hit',hit
            if time.monotonic()>=deadline:
                self.count('read_cache_deferred')
                raise BudgetUnavailable('HL_READ_INFLIGHT')
            time.sleep(.025)

    def read_complete(self,key,lease,body,ttl):
        with self.db() as db:
            db.execute('UPDATE read_cache SET body=?,expires=?,lease=0 WHERE key=? AND lease=?',
                (body,time.time()+float(ttl) if body is not None else 0,key,lease))

    def metadata_complete(self,key,lease,body):
        with self.db() as db:
            db.execute('UPDATE metadata SET body=?,expires=?,lease=0 WHERE key=? AND lease=?',
                (body,time.time()+30 if body is not None else 0,key,lease))

    def begin(self,endpoint,payload,source,priority):
        now=time.time();identity=uuid.uuid4().hex;cost=weights(endpoint,payload,maximum=True)
        service=os.getenv('HL_API_SOURCE','unspecified')
        waiter=hashlib.sha256((service+'|'+source+'|'+endpoint).encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            soft,hard,enforce,cooldown=db.execute('SELECT soft,hard,enforce,cooldown FROM limits WHERE id=1').fetchone()
            normal,elevated,hard=band_ceilings(soft,hard)
            # Requests are inserted before the transport call. ``used`` thus
            # includes completed and in-flight reservations, and this read +
            # projected check is atomic under BEGIN IMMEDIATE.
            used=db.execute('SELECT COALESCE(SUM(weight),0) FROM requests WHERE at>?',(now-60,)).fetchone()[0]
            fair_defer=False
            if priority>=2:
                db.execute('DELETE FROM admission_waiters WHERE last_seen<?',(now-120,))
                db.execute('INSERT INTO admission_waiters VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,cost=excluded.cost',
                    (waiter,service,source,priority,cost,now,now))
                # Age low tiers toward P3, never above actionable account/Risk
                # evidence. Within tiers the least recently served weight wins.
                waiter_ceiling=hard if priority<=2 else elevated
                winner=db.execute('''SELECT w.id FROM admission_waiters w WHERE w.cost<=? ORDER BY
                    MAX(3,w.priority-CAST((?-w.enqueued)/60 AS INTEGER))-(w.priority=2)*2,
                    (SELECT COALESCE(SUM(r.weight),0) FROM requests r WHERE r.service=w.service AND r.source=w.source AND r.at>?),
                    w.enqueued,w.id LIMIT 1''',(max(0,waiter_ceiling-used),now,now-60)).fetchone()
                # A waiter is a demand hint, not a running coroutine. Sequential
                # SDK reads may have abandoned its later endpoint after an
                # earlier read deferred. Do not idle free capacity for that
                # phantom turn: enforce fairness only under actual contention.
                fair_defer=bool(winner and winner[0]!=waiter and
                                used+cost>soft*FAIR_CONTENTION_FRACTION*.9)
            from core.interactive_budget import admission
            # P0/P1 and actionable P2 may use the reserved 1050-1180 band.
            # Ordinary discovery/history work is capped at 1050 and can never
            # consume the critical reserve.
            ceiling=hard if priority<=2 else elevated
            interactive=admission(db,now,priority)
            if interactive is not None:
                interactive_ceiling,interactive_defer=interactive
                # Combine rather than replace: an interactive lease must not
                # silently switch off fair contention between background
                # callers that the waiter table just decided.
                fair_defer=fair_defer or interactive_defer
                # A temporary lease can narrow admission, never widen the
                # persisted operator hard ceiling or the 1180 plan.
                ceiling=min(ceiling,interactive_ceiling)
            # 429 opens a shared cooldown. Preserve P0/P1 access for
            # reconciliation and execution safety; defer lower priorities.
            rate_limited=now<cooldown and priority>1
            if enforce and (rate_limited or used+cost>ceiling or fair_defer):
                db.execute("INSERT INTO counters VALUES('budget_deferred',1) ON CONFLICT(name) DO UPDATE SET value=value+1")
                db.commit();raise BudgetUnavailable('HL_API_BUDGET_DEFERRED')
            db.execute('DELETE FROM admission_waiters WHERE id=?',(waiter,))
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',(identity,now,
                service,source,endpoint,priority,cost,0,0,0))
            # Bounded metrics retention; no financial evidence is stored here.
            # This used to run on every admission, holding the exclusive write
            # transaction that all processes share for three extra DELETEs.
            # A 24h window does not need per-request pruning; once a minute
            # per process keeps the shared hot path short.
            if now-self._retention_at>=60:
                self._retention_at=now
                db.execute('DELETE FROM requests WHERE at<?',(now-86400,))
                db.execute('DELETE FROM ws_events WHERE at<?',(now-86400,))
                db.execute('DELETE FROM metadata WHERE expires<? AND lease<?',(now-3600,now))
                db.execute('DELETE FROM read_cache WHERE expires<? AND lease<?',(now-3600,now))
        return identity

    def finish(self,identity,endpoint,payload,response,elapsed,error=False):
        status=(-2 if error=='TIMEOUT' else -1) if error else response.status_code
        body=None
        if not error:
            try:body=response.json()
            except ValueError:pass
        # Unknown acceptance/timeout reserves worst documented response cost.
        cost=weights(endpoint,payload,body,maximum=error)
        with self.db() as db:
            db.execute('UPDATE requests SET weight=?,status=?,elapsed=?,items=? WHERE id=?',
                (cost,status,elapsed,len(body) if isinstance(body,list) else 0,identity))
            if status==429:
                try:delay=max(1,min(300,float(response.headers.get('Retry-After',60))))
                except ValueError:delay=60
                db.execute('UPDATE limits SET cooldown=MAX(cooldown,?) WHERE id=1',(time.time()+delay,))

    def websocket(self,identity,kind,n=0):
        now=time.time()
        with self.db() as db:
            if kind=='close':db.execute('DELETE FROM ws WHERE id=?',(identity,))
            elif kind not in {'attempt','disconnect'}:db.execute('INSERT INTO ws VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat,subscriptions=excluded.subscriptions',
                            (identity,os.getpid(),now,n))
            if kind!='heartbeat':db.execute('INSERT INTO ws_events VALUES(?,?,?)',(now,kind,n))


_budgets={};_lock=threading.Lock()
def configured():
    path=os.getenv('HL_API_BUDGET_DB')
    if not path:return None
    with _lock:
        if path not in _budgets:_budgets[path]=Budget(path)
        return _budgets[path]


class BudgetSession(requests.Session):
    def request(self,method,url,**kwargs):
        target=urlsplit(url);budget=configured()
        if not budget or method.upper()!='POST' or target.hostname not in HOSTS or target.path not in {'/info','/exchange'}:
            return super().request(method,url,**kwargs)
        payload=kwargs.get('json') or {};endpoint='exchange' if target.path=='/exchange' else payload.get('type','UNKNOWN')
        key=None;lease=None;cache_body=None;read_ttl=None
        # Read-through cache is restricted to the public snapshots named in
        # READ_CACHE_TTL. Account, history, order status and every /exchange
        # action are uncacheable and always cross the network. Instrument
        # metadata keeps its own longer-lived table below.
        if target.path=='/info' and endpoint not in META:read_ttl=READ_CACHE_TTL.get(endpoint)
        if read_ttl is not None:
            # The whole request and host are in the key, so TESTNET/MAINNET and
            # different instruments can never share evidence.
            cache_payload=_read_cache_payload(endpoint,payload)
            key=hashlib.sha256((url+'|'+json.dumps(cache_payload,sort_keys=True,separators=(',',':'))).encode()).hexdigest()
            status,value=budget.read_acquire(key,read_ttl)
            if status=='hit':
                response=requests.Response();response.status_code=200;response.url=url
                response._content=value.encode();response.headers['Content-Type']='application/json'
                return response
            lease=value
        elif endpoint in META and set(payload)<=({'type','dex'} if endpoint=='meta' else {'type'}):
            key=hashlib.sha256((url+'|'+json.dumps(payload,sort_keys=True)).encode()).hexdigest()
            status,value=budget.metadata_acquire(key)
            if status=='hit':
                response=requests.Response();response.status_code=200;response.url=url
                response._content=value.encode();response.headers['Content-Type']='application/json'
                return response
            lease=value
        try:
            source,priority=origin();identity=budget.begin(endpoint,payload,source,priority);started=time.monotonic()
            try:response=super().request(method,url,**kwargs)
            except Exception as exc:
                budget.finish(identity,endpoint,payload,None,time.monotonic()-started,
                              error='TIMEOUT' if isinstance(exc,requests.Timeout) else 'TRANSPORT')
                raise
            budget.finish(identity,endpoint,payload,response,time.monotonic()-started)
            if key and response.status_code==200:
                try:
                    body=response.json()
                    # allow_nan=False keeps malformed evidence from becoming
                    # reusable: a non-finite number raises and nothing is cached.
                    if read_ttl is not None:
                        if isinstance(body,(dict,list)):cache_body=json.dumps(body,allow_nan=False)
                    elif (endpoint=='perpDexs' and isinstance(body,list)) or (isinstance(body,dict) and isinstance(body.get('universe'),list)):
                        cache_body=json.dumps(body,allow_nan=False)
                except (ValueError,TypeError):pass
            return response
        finally:
            if key:
                if read_ttl is not None:budget.read_complete(key,lease,cache_body,read_ttl)
                else:budget.metadata_complete(key,lease,cache_body)


def install_sdk():
    """Cover SDK constructor metadata AND nested Exchange.info without signing changes."""
    from hyperliquid.api import API
    if getattr(API,'_wh_budget',False):return
    original=API.__init__
    def init(self,*args,**kwargs):
        original(self,*args,**kwargs)
        old=self.session;self.session=BudgetSession();self.session.headers.update(old.headers);old.close()
    API.__init__=init;API._wh_budget=True


_snapshots={};_snapshot_lock=threading.Lock()


def snapshot(path,max_age=5.):
    """Aggregate IP operational metrics only. No addresses/payloads exposed.

    The scan itself is a single ordered pass, which measurement showed is
    cheaper than asking SQLite to group the journal. What was expensive was
    running that pass on EVERY health poll over a ~110k-row retention window,
    so the result is memoised for a few seconds. The value is display-only
    (core/product_read.py is the sole caller), so a small staleness in
    rest_weight_1m is acceptable; admission control never reads this.
    """
    now=time.time();key=str(path)
    with _snapshot_lock:cached=_snapshots.get(key)
    if cached and 0<=now-cached[0]<max_age:
        return dict(cached[1],checked_ms=int(now*1000),cache_age_ms=int((now-cached[0])*1000))
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        rows=db.execute('SELECT at,weight,status FROM requests ORDER BY at').fetchall()
        current=0;total_weight=0;http429=0;transport=0;timeouts=0
        peak=0;lo=0;running=0;floor=now-60
        for hi,r in enumerate(rows):
            total_weight+=r[1]
            if r[0]>floor:current+=r[1]
            if r[2]==429:http429+=1
            elif r[2]==-1:transport+=1
            elif r[2]==-2:timeouts+=1
            running+=r[1]
            while rows[lo][0]<=r[0]-60:running-=rows[lo][1];lo+=1
            peak=max(peak,running)
        soft,hard,enforce,cooldown=db.execute('SELECT soft,hard,enforce,cooldown FROM limits').fetchone()
        normal,elevated,hard=band_ceilings(soft,hard)
        sockets=db.execute('SELECT pid,heartbeat,subscriptions FROM ws').fetchall()
        alive=[]
        for pid,heartbeat,subscriptions in sockets:
            try:os.kill(pid,0);alive.append((heartbeat,subscriptions))
            except OSError:pass
        value=dict(version='hl-budget-v1',checked_ms=int(now*1000),window_start_ms=int(rows[0][0]*1000) if rows else None,
            retention_seconds=86400,weight_evidence='DOCUMENTED_RESPONSE_WEIGHT_CONSERVATIVE_ROUNDING',
            rest_limit=1200,soft_limit=normal,elevated_limit=elevated,enforced=bool(enforce),
            safety_limit=hard,hard_planned_ceiling=HARD_PLANNED_CEILING,
            rest_requests_total=len(rows),rest_weight_total=total_weight,rest_weight_1m=current,
            rest_weight_peak_1m=peak,http_429=http429,transport_errors=transport,
            timeouts=timeouts,
            state='RATE_LIMITED' if now<cooldown else 'CONSTRAINED' if current>=hard else 'ELEVATED' if current>=soft else 'HEALTHY',
            ws_connections=len(alive),ws_subscriptions=sum(x[1] for x in alive),ws_limit=10,subscription_limit=1000,
            ws_new_1m=db.execute("SELECT COUNT(*) FROM ws_events WHERE kind='connect' AND at>?",(now-60,)).fetchone()[0],
            ws_messages_1m=db.execute("SELECT COALESCE(SUM(n),0) FROM ws_events WHERE kind='send' AND at>?",(now-60,)).fetchone()[0],
            counters=dict(db.execute('SELECT * FROM counters')))
    with _snapshot_lock:_snapshots[key]=(now,value)
    return value
