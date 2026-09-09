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
from pathlib import Path
from urllib.parse import urlsplit
import requests

LIGHT={'l2Book','allMids','clearinghouseState','orderStatus','spotClearinghouseState','exchangeStatus'}
LISTS={'recentTrades','historicalOrders','userFills','userFillsByTime','fundingHistory','userFunding',
       'nonUserFundingUpdates','twapHistory','userTwapSliceFills','userTwapSliceFillsByTime',
       'delegatorHistory','delegatorRewards','validatorStats'}
META={'meta','spotMeta','perpDexs'}
HOSTS={'api.hyperliquid.xyz','api.hyperliquid-testnet.xyz'}


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
    frames=[];f=inspect.currentframe().f_back
    try:
        for _ in range(30):
            if f is None:break
            frames.append((Path(f.f_code.co_filename).stem,f.f_code.co_name));f=f.f_back
    finally:del f
    for name,priority in [('recover',0),('reconcile_order',0),('recover_pending_manual_leader',0),
        ('_cycle',1),('execute',1),('research',2),('detect',3),('analyze_one',4),('analyse_wallet',5)]:
        for file,fn in frames:
            if fn==name:return file+'.'+fn,priority
    for file,fn in frames:
        if file in {'server','hyperliquid','data','manual_copy_worker','capital_snapshot'}:return file+'.'+fn,2
    return 'sdk_metadata',3


class BudgetUnavailable(RuntimeError):pass


class Budget:
    def __init__(self,path):
        self.path=str(path)
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS limits(id INTEGER PRIMARY KEY,soft INTEGER,hard INTEGER,enforce INTEGER,cooldown REAL);
              INSERT OR IGNORE INTO limits VALUES(1,600,840,0,0);
              CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,at REAL,service TEXT,source TEXT,endpoint TEXT,priority INTEGER,weight INTEGER,status INTEGER,elapsed REAL,items INTEGER);
              CREATE INDEX IF NOT EXISTS request_time ON requests(at);
              CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY,value INTEGER);
              CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,body TEXT,expires REAL,lease REAL);
              CREATE TABLE IF NOT EXISTS ws(id TEXT PRIMARY KEY,pid INTEGER,heartbeat REAL,subscriptions INTEGER);
              CREATE TABLE IF NOT EXISTS ws_events(at REAL,kind TEXT,n INTEGER);
            ''')
        if os.name!='nt':os.chmod(path,0o600)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path,timeout=2)) as db:
            with db:yield db

    def count(self,name):
        with self.db() as db:db.execute('INSERT INTO counters VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1',(name,))

    def begin(self,endpoint,payload,source,priority):
        now=time.time();identity=uuid.uuid4().hex;cost=weights(endpoint,payload,maximum=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            soft,hard,enforce,cooldown=db.execute('SELECT soft,hard,enforce,cooldown FROM limits WHERE id=1').fetchone()
            used=db.execute('SELECT COALESCE(SUM(weight),0) FROM requests WHERE at>?',(now-60,)).fetchone()[0]
            if enforce and (now<cooldown or used+cost>(hard if priority<=1 else soft)):
                db.execute("INSERT INTO counters VALUES('budget_deferred',1) ON CONFLICT(name) DO UPDATE SET value=value+1")
                db.commit();raise BudgetUnavailable('HL_API_BUDGET_DEFERRED')
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',(identity,now,
                os.getenv('HL_API_SOURCE','unspecified'),source,endpoint,priority,cost,0,0,0))
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
            else:db.execute('INSERT INTO ws VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat,subscriptions=excluded.subscriptions',
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
        source,priority=origin();identity=budget.begin(endpoint,payload,source,priority);started=time.monotonic()
        try:response=super().request(method,url,**kwargs)
        except Exception as exc:
            budget.finish(identity,endpoint,payload,None,time.monotonic()-started,
                          error='TIMEOUT' if isinstance(exc,requests.Timeout) else 'TRANSPORT')
            raise
        budget.finish(identity,endpoint,payload,response,time.monotonic()-started)
        return response


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
        sockets=db.execute('SELECT pid,heartbeat,subscriptions FROM ws').fetchall()
        alive=[]
        for pid,heartbeat,subscriptions in sockets:
            try:os.kill(pid,0);alive.append((heartbeat,subscriptions))
            except OSError:pass
        return dict(version='hl-budget-v1',checked_ms=int(now*1000),window_start_ms=int(rows[0][0]*1000) if rows else None,
            rest_limit=1200,soft_limit=soft,safety_limit=hard,enforced=bool(enforce),
            rest_requests_total=len(rows),rest_weight_total=sum(r[1] for r in rows),rest_weight_1m=current,
            rest_weight_peak_1m=peak,http_429=sum(r[2]==429 for r in rows),transport_errors=sum(r[2]==-1 for r in rows),
            timeouts=sum(r[2]==-2 for r in rows),
            state='RATE_LIMITED' if now<cooldown else 'CONSTRAINED' if current>=hard else 'ELEVATED' if current>=soft else 'HEALTHY',
            ws_connections=len(alive),ws_subscriptions=sum(x[1] for x in alive),ws_limit=10,subscription_limit=1000,
            ws_new_1m=db.execute("SELECT COUNT(*) FROM ws_events WHERE kind='connect' AND at>?",(now-60,)).fetchone()[0],
            ws_messages_1m=db.execute("SELECT COALESCE(SUM(n),0) FROM ws_events WHERE kind='send' AND at>?",(now-60,)).fetchone()[0],
            counters=dict(db.execute('SELECT * FROM counters')))
