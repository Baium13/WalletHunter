"""Authenticated, read-only public research feed. Never opens financial stores."""
import asyncio
import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter
from core.foundation.contracts import Network


class ResearchView:
    def __init__(self,path,network):
        self.path=Path(path)
        self.network=TypeAdapter(Network).validate_python(network)

    def read(self,after=0,limit=20):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=50:
            raise ValueError('QUERY_BOUND')
        empty={'mode':'OBSERVE','network':self.network,'execution_enabled':False,
            'health':'DEGRADED','status':'WAITING','reason':'WORKER_NOT_STARTED','counts':{},
            'initialized':False,'leaders':[],'events':[],'cursor':after,'private_account_data':False,
            'agent_status':{'status':'OFFLINE','ready':0,'active':0,'total':7},
            'components':{'public_data':{'status':'WAITING','reason':'WORKER_NOT_STARTED'},
                'discovery':{'status':'WAITING','reason':'WORKER_NOT_STARTED'},
                'deep_analysis':{'status':'WAITING','reason':'WORKER_NOT_STARTED'},
                'watchlist':{'status':'WAITING','reason':'WORKER_NOT_STARTED'},
                'leader_detection':{'status':'WAITING','reason':'WORKER_NOT_STARTED'},
                'agents':{'status':'OFFLINE','ready':0,'active':0,'total':7},
                'consensus':{'status':'READY','detail':'WAITING FOR LEADER EVENT'},
                'risk':{'status':'READY','detail':'CANONICAL GATEWAY READY'},
                'paper_auto':{'status':'WAITING','detail':'EXPLICIT PAPER CONFIGURATION REQUIRED'}}}
        if not self.path.exists(): return empty
        if self.path.is_symlink() or self.path.parent.is_symlink():
            return dict(empty,reason='RESEARCH_PATH_UNSAFE')
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                db.row_factory=sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                health=db.execute('SELECT last_success,last_attempt,error FROM intelligence_health WHERE network=?',(self.network,)).fetchone()
                counts={r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM candidates WHERE network=? GROUP BY status',(self.network,))}
                leaders=[]
                for row in db.execute('SELECT wallet,status,score,confidence,last_seen,analysis FROM candidates WHERE network=? ORDER BY score DESC,wallet LIMIT 32',(self.network,)):
                    item={k:row[k] for k in ('wallet','status','score','confidence','last_seen')}
                    # Analysis is public research evidence.  Keep only the
                    # immutable, non-account fields needed by the product
                    # detail view; never broadcast raw database columns or
                    # private runtime state.
                    try:
                        raw=json.loads(row['analysis']) if row['analysis'] else None
                    except (TypeError,ValueError,json.JSONDecodeError):
                        raw=None
                    if isinstance(raw,dict):
                        item['analysis']={k:raw[k] for k in (
                            'wallet','network','computed_ms','score','windows',
                            'history_method','equity_drawdown_pct',
                            'funding_included','not_a_profit_probability') if k in raw}
                    else:
                        item['analysis']=None
                    leaders.append(item)
                events=[]
                for row in db.execute('SELECT rowid AS seq,kind,created,body FROM intelligence_records WHERE network=? AND rowid>? ORDER BY rowid LIMIT ?',(self.network,after,limit)):
                    body=json.loads(row['body'])
                    # Raw books/candles are retained for replay, not broadcast
                    # to every slow mobile client on every event.
                    if row['kind']=='DECISION':
                        body={k:body[k] for k in ('event','agents','consensus','mode','authorization','executed')}
                    elif row['kind']=='LEADER_ANALYZED':
                        body={k:body[k] for k in ('wallet','network','computed_ms','score','equity_drawdown_pct','funding_included','not_a_profit_probability')}
                    events.append({'seq':row['seq'],'kind':row['kind'],'created':row['created'],'body':body})
            now=int(time.time()*1000)
            running=bool(health and health['last_attempt'] is not None and 0<=now-health['last_attempt']<120000)
            error_text=str(health['error'] or '') if health else ''
            fatal_public=any(token in error_text for token in ('PUBLIC_STREAM_UNAVAILABLE','DISCOVERY_UNAVAILABLE'))
            public_active=bool(running and not fatal_public)
            healthy=bool(public_active and not error_text)
            reason=health['error'] if health and health['error'] else ('READY_NO_CURRENT_EVENT' if running else 'WORKER_NOT_STARTED')
            worker_status='ACTIVE' if public_active else 'DEGRADED' if running else 'WAITING'
            deep_status='DEGRADED' if any(token in error_text for token in ('HISTORY_INCOMPLETE','RESEARCH_DEFERRED')) else 'READY' if running else 'WAITING'
            agent_status={'status':'READY' if running else 'OFFLINE','ready':7 if running else 0,'active':0,'total':7}
            counts=dict(counts)
            counts.setdefault('OBSERVED',sum(counts.values()))
            components={'public_data':{'status':worker_status,'last_success':health['last_success'] if health else None,'reason':reason},
                'discovery':{'status':worker_status,'last_success':health['last_success'] if health else None,'reason':reason},
                'deep_analysis':{'status':deep_status,'last_success':health['last_success'] if health else None,'reason':'WAITING FOR CANDIDATE' if running and deep_status == 'READY' else reason},
                'watchlist':{'status':'READY' if running else 'WAITING','detail':f"{counts.get('ACTIVE',0)} ACTIVE"},
                'leader_detection':{'status':'READY' if running else 'WAITING','detail':'WAITING FOR FRESH FILL' if running else reason},
                'agents':agent_status,'consensus':{'status':'READY' if running else 'WAITING','detail':'NO CURRENT DECISION' if running else reason},
                'risk':{'status':'READY','detail':'CANONICAL GATEWAY READY'},
                'paper_auto':{'status':'READY' if running else 'WAITING','detail':'PAPER CONFIGURATION ISOLATED; NO LIVE ORDERS'}}
            return dict(empty,health='HEALTHY' if healthy else 'DEGRADED',status=worker_status,reason=reason,
                initialized=bool(health),last_success=health['last_success'] if health else None,
                last_attempt=health['last_attempt'] if health else None,counts=counts,leaders=leaders,events=events,
                cursor=events[-1]['seq'] if events else after,agent_status=agent_status,components=components)
        except (sqlite3.Error,ValueError,KeyError,TypeError):
            return dict(empty,status='DEGRADED',health='DEGRADED',reason='RESEARCH_UNAVAILABLE',
                components={**empty['components'],'public_data':{'status':'DEGRADED','reason':'RESEARCH_UNAVAILABLE'},
                    'discovery':{'status':'DEGRADED','reason':'RESEARCH_UNAVAILABLE'}})


def router(path,network,authenticate):
    api=APIRouter()
    view=ResearchView(path,network)
    connections=Counter()

    @api.get('/api/intelligence')
    def intelligence(after:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=50),x_telegram_init_data:str|None=Header(default=None)):
        authenticate(x_telegram_init_data)
        return view.read(after,limit)

    @api.get('/api/intelligence/stream')
    async def stream(request:Request,after:int=Query(0,ge=0),x_telegram_init_data:str|None=Header(default=None)):
        user=authenticate(x_telegram_init_data)
        uid=user['id']
        if connections[uid]>=2 or sum(connections.values())>=64:
            raise HTTPException(429,'STREAM_LIMIT')
        connections[uid]+=1
        async def generate():
            cursor=after
            try:
                # Deliberately short-lived: authentication is revalidated on
                # reconnect; no execution queue waits on this generator.
                for _ in range(15):
                    if await request.is_disconnected(): break
                    try: authenticate(x_telegram_init_data)
                    except HTTPException: break
                    row=await asyncio.to_thread(view.read,cursor,20)
                    cursor=row['cursor']
                    yield 'id: '+str(cursor)+'\nevent: research\ndata: '+json.dumps(row,allow_nan=False,separators=(',',':'))+'\n\n'
                    await asyncio.sleep(2)
            finally:
                connections[uid]-=1
                if not connections[uid]: del connections[uid]
        return StreamingResponse(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
    return api
