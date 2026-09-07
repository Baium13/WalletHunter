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
    """One bounded public socket; no account subscription or private data.

    WsTrade.users=[buyer,seller] is documented in Hyperliquid subscriptions.
    Socket snapshots are candidate discovery only, never executable signals.
    """
    def __init__(self, network, coins=('BTC','ETH','HYPE')):
        self.network=validated_network(network)
        if not 1<=len(coins)<=4: raise ValueError('SUBSCRIPTION_LIMIT')
        for coin in coins: InstrumentId(network=self.network,symbol=coin)
        self.coins,self.socket=coins,None
    def poll(self):
        import websocket
        try:
            if self.socket is None:
                host='api.hyperliquid.xyz' if self.network=='MAINNET' else 'api.hyperliquid-testnet.xyz'
                self.socket=websocket.create_connection('wss://'+host+'/ws',timeout=2,enable_multithread=True)
                for coin in self.coins:
                    self.socket.send(packed({'method':'subscribe','subscription':{'type':'trades','coin':coin}}))
            self.socket.settimeout(.2)
            found=[]
            for _ in range(5):
                try: message=self.socket.recv()
                except websocket.WebSocketTimeoutException: break
                if not message or len(message)>262144: raise ValueError('STREAM_DISCONNECTED_OR_OVERSIZED')
                message=json.loads(message)
                if message.get('channel')!='trades': continue
                rows=message['data']
                if not isinstance(rows,list) or len(rows)>256: raise ValueError('STREAM_BATCH_INVALID')
                found.extend(rows)
            return found
        except Exception:
            self.close()
            raise ValueError('PUBLIC_STREAM_UNAVAILABLE') from None
    def close(self):
        if self.socket is not None:
            try: self.socket.close()
            finally: self.socket=None


class WalletDiscoveryEngine:
    def __init__(self, path, network, policy=None):
        self.network=validated_network(network)
        self.policy=policy or IntelligencePolicy()
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
        with self.store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS candidates(network TEXT,wallet TEXT,first_seen INTEGER,last_seen INTEGER,
                    status TEXT,next_eval INTEGER,cursor INTEGER,score REAL,confidence REAL,analysis TEXT,
                    PRIMARY KEY(network,wallet));
                CREATE TABLE IF NOT EXISTS intelligence_records(id TEXT PRIMARY KEY,network TEXT,kind TEXT,created INTEGER,body TEXT);
                CREATE INDEX IF NOT EXISTS intelligence_created ON intelligence_records(network,created);
                CREATE TABLE IF NOT EXISTS intelligence_health(network TEXT PRIMARY KEY,last_success INTEGER,last_attempt INTEGER,error TEXT,errors INTEGER);
                CREATE TABLE IF NOT EXISTS intelligence_lease(network TEXT PRIMARY KEY,owner TEXT,until_ms INTEGER);
                CREATE TABLE IF NOT EXISTS decision_links(event_id TEXT PRIMARY KEY,record_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observed_fills(network TEXT,wallet TEXT,fill_id TEXT,event_id TEXT,
                    PRIMARY KEY(network,wallet,fill_id));
                CREATE TABLE IF NOT EXISTS watch_epochs(network TEXT,wallet TEXT,started INTEGER,PRIMARY KEY(network,wallet));
                CREATE TRIGGER IF NOT EXISTS research_no_update BEFORE UPDATE ON intelligence_records BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS research_no_delete BEFORE DELETE ON intelligence_records BEGIN SELECT RAISE(ABORT,'append only'); END;
            ''')

    def _record(self,db,kind,body,now,instrument=None):
        record_id=identity([self.network,kind,body])
        old=db.execute('SELECT body FROM intelligence_records WHERE id=?',(record_id,)).fetchone()
        if old:
            if old[0]!=packed(body): raise ValueError('IDENTITY_COLLISION')
            return record_id
        if db.execute('SELECT COUNT(*) FROM intelligence_records').fetchone()[0]>=100000:
            raise ValueError('EVENT_STORAGE_BUDGET_ARCHIVE_REQUIRED')
        db.execute('INSERT INTO intelligence_records VALUES(?,?,?,?,?)',(record_id,self.network,kind,now,packed(body)))
        correlation=body.get('event_id') or body.get('event',{}).get('event_id') or record_id
        reference=AnalysisResult(instrument=instrument or InstrumentId(network=self.network,symbol='BTC'),
            correlation_id=correlation,created_ms=now,evidence_ids=(record_id,),conclusion='RESEARCH_ONLY')
        self.store.append_in(db,DomainEvent(event_id=record_id,event_type='LEADER_EVENT',correlation_id=correlation,
            scope=self.scope,event_ms=now,received_ms=now,payload=reference))
        return record_id

    def observe(self,trades,now):
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
        with self.store.transaction() as db:
            for address in sorted(addresses):
                exists=db.execute('SELECT 1 FROM candidates WHERE network=? AND wallet=?',(self.network,address)).fetchone()
                if exists:
                    db.execute('UPDATE candidates SET last_seen=MAX(last_seen,?) WHERE network=? AND wallet=?',(now,self.network,address))
                elif db.execute('SELECT COUNT(*) FROM candidates WHERE network=?',(self.network,)).fetchone()[0]<self.policy.registry_limit:
                    db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)',(self.network,address,now,now,'DISCOVERED',now,now,None,None,None))
                    self._record(db,'WALLET_DISCOVERED',{'wallet':address,'network':self.network,'observed_ms':now},now)

    def analyze_one(self,address,info,now):
        address=wallet(address)
        cheap=fetch_fills(info,address,max(0,now-DAY),now,now_ms=now,max_requests=2)
        if len(cheap)<self.policy.cheap_min_fills:
            with self.store.transaction() as db:
                db.execute("UPDATE candidates SET status='CANDIDATE',next_eval=? WHERE network=? AND wallet=?",(now+self.policy.reevaluate_ms,self.network,address))
            return None
        fills=filter_perp_fills(fetch_fills(info,address,max(0,now-180*DAY),now,now_ms=now,max_requests=self.policy.history_requests))
        windows=reports(fills,now)
        ranked=score(address,self.network,windows,now,self.policy)
        analysis={'wallet':address,'network':self.network,'computed_ms':now,'score':ranked.model_dump(mode='json'),
            'windows':windows,'history_method':'bounded-fill-history','equity_drawdown_pct':None,
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
            winners=db.execute("SELECT wallet,status FROM candidates WHERE network=? AND status IN ('QUALIFIED','ACTIVE') AND next_eval>? ORDER BY score DESC,confidence DESC,wallet LIMIT ?",(self.network,now,self.policy.watch_limit)).fetchall()
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

    def detect(self,address,info,now):
        with self.store.transaction() as db:
            saved=db.execute("SELECT cursor,status FROM candidates WHERE network=? AND wallet=?",(self.network,address)).fetchone()
        if not saved or saved['status']!='ACTIVE': return []
        cursor=saved['cursor']
        if now<cursor: raise ValueError('CLOCK_REGRESSION')
        # Re-read a bounded overlap for delayed visibility and same-millisecond
        # fills. A durable fill identity, not wall-clock cursor alone, dedupes.
        fills=filter_perp_fills(fetch_fills(info,address,max(0,cursor-60000),now,now_ms=now,max_requests=4))
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

    def research(self,event,leader,info,now,clock=None):
        with self.store.transaction() as db:
            existing=db.execute('SELECT r.body FROM decision_links l JOIN intelligence_records r ON r.id=l.record_id WHERE l.event_id=?',(event.event_id,)).fetchone()
        if existing: return ConsensusDecision.model_validate(json.loads(existing[0])['consensus'])
        coin=event.instrument.market_key.split('|')[0]
        book=info({'type':'l2Book','coin':coin})
        candles=info({'type':'candleSnapshot','req':{'coin':coin,'interval':'15m','startTime':now-64*900000,'endTime':now}})
        if clock is not None: now=clock()
        if not isinstance(candles,list): candles=[]
        candles=[c for c in candles if type(c.get('T')) is int and c['T']<=now]
        outputs=evaluate(event,leader,candles,book,now,self.policy)
        decision=consensus(event,outputs,now,self.policy)
        body={'event':event.model_dump(mode='json'),'leader':leader.model_dump(mode='json'),'policy':self.policy.model_dump(mode='json'),
            'candles':candles,'book':book,'agents':[a.model_dump(mode='json') for a in outputs],'consensus':decision.model_dump(mode='json'),
            'mode':'OBSERVE','authorization':'NONE','executed':False}
        with self.store.transaction() as db:
            if db.execute('SELECT 1 FROM decision_links WHERE event_id=?',(event.event_id,)).fetchone(): return decision
            record_id=self._record(db,'DECISION',body,now,event.instrument)
            db.execute('INSERT INTO decision_links VALUES(?,?)',(event.event_id,record_id))
        return decision

    def cycle(self,reader,trades,now,clock=None):
        if reader.network!=self.network: raise ValueError('NETWORK_MISMATCH')
        clock=clock or (lambda:now)
        with self.store.transaction() as db:
            lease=db.execute('SELECT owner,until_ms FROM intelligence_lease WHERE network=?',(self.network,)).fetchone()
            if lease and lease['until_ms']>now: return False
            db.execute('INSERT OR REPLACE INTO intelligence_lease VALUES(?,?,?)',(self.network,self.owner,now+120000))
        info=RequestBudget(reader,self.policy.request_limit)
        errors=[]
        try:
            with self.store.transaction() as db:
                active=db.execute("SELECT wallet,analysis FROM candidates WHERE network=? AND status='ACTIVE' ORDER BY wallet LIMIT ?",(self.network,self.policy.watch_limit)).fetchall()
            for row in active:
                try:
                    self.detect(row['wallet'],info,clock())
                except Exception: errors.append('WATCHLIST_OR_ANALYSIS_UNAVAILABLE')
            with self.store.transaction() as db:
                queue=db.execute("SELECT r.body,c.analysis FROM intelligence_records r JOIN candidates c ON c.network=r.network AND c.wallet=json_extract(r.body,'$.wallet') LEFT JOIN decision_links l ON l.event_id=json_extract(r.body,'$.event_id') WHERE r.network=? AND r.kind='LEADER_TRADE' AND l.event_id IS NULL ORDER BY r.rowid LIMIT 4",(self.network,)).fetchall()
            for queued in queue:
                try:
                    event=LeaderTradeEvent.model_validate_json(queued['body'])
                    leader=LeaderScore.model_validate(json.loads(queued['analysis'])['score'])
                    self.research(event,leader,info,clock(),clock=clock)
                except Exception: errors.append('RESEARCH_DEFERRED')
            self.observe(trades,now)
            with self.store.transaction() as db:
                pending=db.execute('SELECT wallet FROM candidates WHERE network=? AND next_eval<=? ORDER BY next_eval,wallet LIMIT ?',
                    (self.network,now,self.policy.deep_per_cycle)).fetchall()
            for row in pending:
                try: self.analyze_one(row['wallet'],info,clock())
                except Exception:
                    errors.append('HISTORY_INCOMPLETE')
                    with self.store.transaction() as db:
                        db.execute("UPDATE candidates SET status='PROBATION',next_eval=? WHERE network=? AND wallet=?",(now+60000,self.network,row['wallet']))
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
            leaders=db.execute('SELECT wallet,status,score,confidence,last_seen,analysis FROM candidates WHERE network=? ORDER BY score DESC,wallet LIMIT 32',(self.network,)).fetchall()
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
    outputs=evaluate(event,leader,record['candles'],record['book'],now,policy)
    return consensus(event,outputs,now,policy).model_dump(mode='json')
