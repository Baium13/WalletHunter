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
from core.settings import validated_network


class ResearchView:
    def __init__(self,path,network):
        self.path=Path(path)
        self.network=validated_network(network)

    def read(self,after=0,limit=20):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=50:
            raise ValueError('QUERY_BOUND')
        empty={'mode':'OBSERVE','network':self.network,'execution_enabled':False,
            'health':'DEGRADED','reason':'WORKER_NOT_STARTED','counts':{},'leaders':[],
            'events':[],'cursor':after,'private_account_data':False}
        if not self.path.exists(): return empty
        if self.path.is_symlink() or self.path.parent.is_symlink():
            return dict(empty,reason='RESEARCH_PATH_UNSAFE')
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                db.row_factory=sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                health=db.execute('SELECT last_success,error FROM intelligence_health WHERE network=?',(self.network,)).fetchone()
                counts={r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM candidates WHERE network=? GROUP BY status',(self.network,))}
                leaders=[dict(r) for r in db.execute('SELECT wallet,status,score,confidence,last_seen FROM candidates WHERE network=? ORDER BY score DESC,wallet LIMIT 32',(self.network,))]
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
            healthy=bool(health and health['last_success'] is not None and 0<=now-health['last_success']<120000 and not health['error'])
            return dict(empty,health='HEALTHY' if healthy else 'DEGRADED',reason=health['error'] if health else 'WORKER_NOT_STARTED',
                last_success=health['last_success'] if health else None,counts=counts,leaders=leaders,events=events,cursor=events[-1]['seq'] if events else after)
        except (sqlite3.Error,ValueError,KeyError,TypeError):
            return dict(empty,reason='RESEARCH_UNAVAILABLE')


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
