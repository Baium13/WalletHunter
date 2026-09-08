"""Authenticated product contracts; no credentials, no generic 'confirm latest'."""
import asyncio
import json
import math
from typing import Literal
from collections import Counter
from fastapi import APIRouter,Header,HTTPException,Query,Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel,ConfigDict,Field,StrictBool
from core.product_control import ProductConfirmations


class ConfirmationInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    proposal_hash:str=Field(pattern='^[0-9a-f]{64}$')


class NotificationInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    OPEN:StrictBool|None=None
    ADD:StrictBool|None=None
    REDUCE:StrictBool|None=None
    REVERSE:StrictBool|None=None
    CLOSE:StrictBool|None=None
    CRITICAL:StrictBool|None=None
    LIVE_CONFIRM:StrictBool|None=None


def router(authenticate,resolve,event_service,backend_factory,market_reader=None):
    api=APIRouter();connections=Counter()
    def view(token):
        user=authenticate(token)
        try:return user,resolve(user['id'])
        except (ValueError,OSError):raise HTTPException(409,'PRODUCT_ACCOUNT_OR_RUNTIME_UNAVAILABLE') from None

    @api.get('/api/product')
    def home(x_telegram_init_data:str|None=Header(default=None)):
        _,v=view(x_telegram_init_data);return v.snapshot()

    @api.get('/api/product/{section}')
    def section(section:str,x_telegram_init_data:str|None=Header(default=None)):
        _,v=view(x_telegram_init_data)
        if section=='notification-preferences':return event_service().preferences(v.scope)
        s=v.snapshot()
        if section in {'health','discovery','manual-copy','account'}:return s[section.replace('-','_')]
        if section=='capital':return {'scope':s['scope'],'manual_copy':s['manual_copy'],
            'copy':(s['account'] or {}).get('source_allocations'),'real_account':(s['account'] or {}).get('portfolio'),
            'autonomous':{m['mode']:m.get('allocation') for m in s['runtimes']}}
        fields={'positions':'episodes','agents':'agents','consensus':'consensus','risk':'risk','execution':'execution',
            'analytics':'analytics','calibration':'calibration','capital':'allocation','mode':'runtime_mode'}
        if section=='leaders':return s['discovery']['leaders']
        if section not in fields:raise HTTPException(404,'PRODUCT_SECTION_NOT_FOUND')
        if section in {'risk','consensus'}:
            return {'scope':s['scope'],'readiness':s['health']['components'][section],
                'modes':{m['mode']:{'decision':m.get(fields[section]),'current_state':'PRESENT' if m.get(fields[section]) else 'NO_CURRENT_DECISION',
                    'execution_authority':False} for m in s['runtimes']},
                'analysis':s['analysis'] if section=='consensus' else None}
        if section=='agents':return {'scope':s['scope'],'modes':{m['mode']:m.get('agents') for m in s['runtimes']},'analysis':s['analysis']}
        return {'scope':s['scope'],'modes':{m['mode']:m.get(fields[section]) for m in s['runtimes']}}

    @api.put('/api/product/notification-preferences')
    def notifications(payload:NotificationInput,x_telegram_init_data:str|None=Header(default=None)):
        _,v=view(x_telegram_init_data)
        changes=payload.model_dump(exclude_unset=True)
        try:return event_service().preferences(v.scope,changes)
        except ValueError:raise HTTPException(422,'NOTIFICATION_PREFERENCES_INVALID') from None

    @api.get('/api/product/positions/{episode_id}')
    @api.get('/api/product/timeline/{episode_id}')
    def detail(episode_id:str,x_telegram_init_data:str|None=Header(default=None)):
        _,v=view(x_telegram_init_data)
        try:return v.episode(episode_id)
        except KeyError:raise HTTPException(404,'EPISODE_NOT_FOUND') from None

    @api.get('/api/product/market/candles')
    def market(coin:str=Query(pattern=r'^(?:[a-z0-9]+:)?[A-Za-z0-9._/-]{1,32}$'),
               network:Literal['MAINNET','TESTNET']=Query(),interval:Literal['1m','5m','15m','1h','4h','1d']='15m',
               hours:int=Query(24,ge=1,le=720),x_telegram_init_data:str|None=Header(default=None)):
        _,v=view(x_telegram_init_data)
        if network!=v.scope.network:raise HTTPException(409,'MARKET_NETWORK_MISMATCH')
        if market_reader is None:raise HTTPException(503,'MARKET_READ_UNAVAILABLE')
        try:
            data=market_reader(v.scope,coin,interval,hours,x_telegram_init_data)
            finite=lambda x:isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
            candles=data['candles']
            if len(candles)>500 or any(not all(finite(c.get(k)) for k in ('t','o','h','l','c')) or c['t']<=0 or c['l']<=0
                or c['h']<max(c['o'],c['c']) or c['l']>min(c['o'],c['c']) for c in candles):raise ValueError('MALFORMED_CANDLES')
            mark=data.get('mark')
            if mark is not None and (not finite(mark.get('price')) or mark['price']<=0 or not finite(mark.get('time'))):mark=None
            return {'version':'product-market-v1','scope':v.scope.model_dump(mode='json'),'coin':coin,'interval':interval,
                'candles':candles,'mark':mark,'exchange_timestamp':None,'timestamp_evidence':'REST_RECEIPT_NOT_EXCHANGE_TIME'}
        except HTTPException:raise
        except (ValueError,TypeError,KeyError,OSError):raise HTTPException(503,'MARKET_EVIDENCE_UNAVAILABLE') from None

    @api.get('/api/product/leaders/{wallet}')
    def leader(wallet:str,x_telegram_init_data:str|None=Header(default=None)):
        _,v=view(x_telegram_init_data)
        row=next((r for r in v.discovery()['leaders'] if r['wallet']==wallet),None)
        if row is None:raise HTTPException(404,'LEADER_NOT_FOUND')
        return row

    def control(v):
        binding=next((b for b in v.bindings if b.mode=='LIVE_CONFIRM'),None)
        if not binding:raise HTTPException(409,'LIVE_CONFIRM_NOT_CONFIGURED')
        return ProductConfirmations(v.scope,binding,v.root,backend_factory,v.clock)

    @api.get('/api/product/confirmations/pending')
    def pending(x_telegram_init_data:str|None=Header(default=None)):
        _,v=view(x_telegram_init_data);return {'proposals':[p for p in control(v).proposals() if p['status']=='PENDING']}

    @api.get('/api/product/confirmations/{proposal_id}')
    def proposal(proposal_id:str,x_telegram_init_data:str|None=Header(default=None)):
        _,v=view(x_telegram_init_data);row=next((p for p in control(v).proposals() if p['id']==proposal_id),None)
        if row is None:raise HTTPException(404,'PROPOSAL_NOT_FOUND')
        return row

    @api.post('/api/product/confirmations/{proposal_id}/{action}')
    def confirm(proposal_id:str,action:str,payload:ConfirmationInput,x_telegram_init_data:str|None=Header(default=None)):
        if action not in {'approve','reject'}:raise HTTPException(404,'ACTION_NOT_FOUND')
        user,v=view(x_telegram_init_data)
        try:return control(v).act(proposal_id,payload.proposal_hash,str(user['id']),action=='approve')
        except (ValueError,OSError):raise HTTPException(409,'CONFIRMATION_UNAVAILABLE_OR_CONFLICT') from None

    @api.get('/api/product/events/resume')
    def events(after:int=Query(0,ge=0),x_telegram_init_data:str|None=Header(default=None)):
        _,v=view(x_telegram_init_data);service=event_service();service.ingest(v);snapshot=v.snapshot();service.collect(snapshot)
        return {'snapshot':snapshot,**service.read(v.scope,after)}

    @api.get('/api/product/events/stream')
    async def stream(request:Request,after:int=Query(0,ge=0),x_telegram_init_data:str|None=Header(default=None)):
        user,v=view(x_telegram_init_data);uid=str(user['id'])
        if connections[uid]>=2 or sum(connections.values())>=64:raise HTTPException(429,'STREAM_LIMIT')
        connections[uid]+=1
        async def generate():
            cursor=after
            try:
                for n in range(15):
                    if await request.is_disconnected():break
                    _,current=view(x_telegram_init_data)
                    if current.scope!=v.scope:break
                    def page():
                        service=event_service();service.ingest(current);snapshot=current.snapshot();service.collect(snapshot)
                        return snapshot,service.read(v.scope,cursor)
                    snapshot,data=await asyncio.to_thread(page);cursor=data['cursor']
                    if n==0 or data['reset_required']:data['snapshot']=snapshot
                    yield 'id: '+str(cursor)+'\nevent: product\ndata: '+json.dumps(data,allow_nan=False,separators=(',',':'))+'\n\n'
                    await asyncio.sleep(2)
            finally:
                connections[uid]-=1
                if not connections[uid]:del connections[uid]
        return StreamingResponse(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
    return api
