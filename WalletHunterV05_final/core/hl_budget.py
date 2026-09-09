"""Host-shared Hyperliquid transport accounting; never stores request payloads.

Official limits verified 2026-09-09. Response block rounding is conservatively
ceiling-rounded (docs don't specify rounding). This is measured client usage,
not an exchange-issued usage counter. All processes share one owner-only DB.
No retries, no financial-response cache, no stale timestamp renewal.
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
# Hyperliquid's documented IP budget is 1200 weighted units/minute. Keep a
# small accounting margin and plan no more than 1180.
NORMAL_CEILING=850
ELEVATED_CEILING=1050
HARD_PLANNED_CEILING=1180
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
        n=(5000 if endpoint=='candleSnapshot' else 2000 if endpoint in
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
              CREATE TABLE IF NOT EXISTS ws(id TEXT PRIMARY KEY,pid INTEGER,heartbeat REAL,subscriptions INTEGER);
              CREATE TABLE IF NOT EXISTS ws_events(at REAL,kind TEXT,n INTEGER);
              CREATE TABLE IF NOT EXISTS admission_waiters(id TEXT PRIMARY KEY,service TEXT,source TEXT,priority INTEGER,cost INTEGER,enqueued REAL,last_seen REAL);
            ''')
            # Migrate only the exact legacy defaults. Operator-tuned values
            # must not be overwritten by a process restart.
            db.execute('UPDATE limits SET soft=?,hard=? WHERE id=1 AND soft=600 AND hard=840',
                       (NORMAL_CEILING,HARD_PLANNED_CEILING))
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
                interactive_ceiling,fair_defer=interactive
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
            db.execute('DELETE FROM requests WHERE at<?',(now-86400,))
            db.execute('DELETE FROM ws_events WHERE at<?',(now-86400,))
            db.execute('DELETE FROM metadata WHERE expires<? AND lease<?',(now-3600,now))
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
        key=None;lease=None;cache_body=None
        if endpoint in META and set(payload)<=({'type','dex'} if endpoint=='meta' else {'type'}):
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
                    if (endpoint=='perpDexs' and isinstance(body,list)) or (isinstance(body,dict) and isinstance(body.get('universe'),list)):
                        cache_body=json.dumps(body,allow_nan=False)
                except (ValueError,TypeError):pass
            return response
        finally:
            if key:budget.metadata_complete(key,lease,cache_body)


def install_sdk():
    """Cover SDK constructor metadata AND nested Exchange.info without signing changes."""
    from hyperliquid.api import API
    if getattr(API,'_wh_budget',False):return
    original=API.__init__
    def init(self,*args,**kwargs):
        original(self,*args,**kwargs)
        old=self.session;self.session=BudgetSession();self.session.headers.update(old.headers);old.close()
    API.__init__=init;API._wh_budget=True


def snapshot(path):
    """Aggregate IP operational metrics only. No addresses/payloads exposed."""
    now=time.time()
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        rows=db.execute('SELECT at,weight,status FROM requests ORDER BY at').fetchall()
        current=sum(r[1] for r in rows if r[0]>now-60);peak=0;lo=0;running=0
        for hi,r in enumerate(rows):
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
        return dict(version='hl-budget-v1',checked_ms=int(now*1000),window_start_ms=int(rows[0][0]*1000) if rows else None,
            retention_seconds=86400,weight_evidence='DOCUMENTED_RESPONSE_WEIGHT_CONSERVATIVE_ROUNDING',
            rest_limit=1200,soft_limit=normal,elevated_limit=elevated,
            safety_limit=hard,hard_planned_ceiling=HARD_PLANNED_CEILING,
            rest_requests_total=len(rows),rest_weight_total=sum(r[1] for r in rows),rest_weight_1m=current,
            rest_weight_peak_1m=peak,http_429=sum(r[2]==429 for r in rows),transport_errors=sum(r[2]==-1 for r in rows),
            timeouts=sum(r[2]==-2 for r in rows),
            state='RATE_LIMITED' if now<cooldown else 'CONSTRAINED' if current>=hard else 'ELEVATED' if current>=soft else 'HEALTHY',
            ws_connections=len(alive),ws_subscriptions=sum(x[1] for x in alive),ws_limit=10,subscription_limit=1000,
            ws_new_1m=db.execute("SELECT COUNT(*) FROM ws_events WHERE kind='connect' AND at>?",(now-60,)).fetchone()[0],
            ws_messages_1m=db.execute("SELECT COALESCE(SUM(n),0) FROM ws_events WHERE kind='send' AND at>?",(now-60,)).fetchone()[0],
            counters=dict(db.execute('SELECT * FROM counters')))
