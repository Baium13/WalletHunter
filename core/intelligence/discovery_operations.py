"""Resource/admission operations, separate from frozen scoring/trading policy.

Segments are research queues, never financial quarantine or position ownership.
HIGH_FREQUENCY is a measured order-rate classification, not proof of a bot.
"""
import json
import math
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from pydantic import Field
from core.foundation.contracts import Contract
from core.fill_history import _key, HistoryIncomplete

DAY=86400000
COLD=('INACTIVE','ZERO_SUPPORTED_CAPITAL','HIGH_FREQUENCY')


class DiscoveryOperations(Contract):
    operations_version: str='continuous-discovery-v1'
    potential_target: int=Field(default=200,ge=1,le=10000)
    admissions_per_cycle: int=Field(default=8,ge=1,le=64)
    idle_rest_weight: int=Field(default=300,ge=1,le=600)
    max_cpu_load: float=Field(default=.75,gt=0,le=1,allow_inf_nan=False)
    min_disk_bytes: int=Field(default=2147483648,ge=1073741824)
    inactive_ms: int=Field(default=7*DAY,ge=DAY)
    cold_recheck_ms: int=Field(default=DAY,ge=3600000)
    bot_recheck_ms: int=Field(default=7*DAY,ge=DAY)
    high_frequency_orders: int=Field(default=1000,ge=100,le=2000)
    high_frequency_span_ms: int=Field(default=600000,ge=60000)
    high_frequency_active_minutes: int=Field(default=20,ge=2)
    # Above this share of the public trade buffer, admitting NEW candidates is
    # paused. Discovery is the only optional consumer of that queue, and its
    # lag is paid by the lifecycle reads that actually hold money.
    backpressure_fraction: float=Field(default=.75,gt=0,le=1,allow_inf_nan=False)


def resources(path,policy):
    """No upstream request. Unknown configured budget defers new research."""
    result={'idle':True,'allow_research':True,'reasons':[],'rest_weight_1m':None}
    try:
        free=shutil.disk_usage(Path(path).parent).free
        result['disk_free_bytes']=free
        if free<policy.min_disk_bytes:result['reasons'].append('DISK_PRESSURE')
        if hasattr(os,'getloadavg'):
            load=os.getloadavg()[0]/max(1,os.cpu_count() or 1);result['cpu_load_fraction']=load
            if not math.isfinite(load) or load>policy.max_cpu_load:result['reasons'].append('CPU_PRESSURE')
        budget_path=os.getenv('HL_API_BUDGET_DB')
        if budget_path:
            with closing(sqlite3.connect(Path(budget_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                now=time.time();weight=db.execute('SELECT COALESCE(SUM(weight),0) FROM requests WHERE at>?',(now-60,)).fetchone()[0]
                soft,hard,cooldown=db.execute('SELECT soft,hard,cooldown FROM limits WHERE id=1').fetchone()
                result['rest_weight_1m']=weight
                result['rest_weight_soft']=soft
                # Measured spare capacity, so deep analysis can use an idle
                # budget instead of crawling at one wallet per cycle forever.
                result['headroom_fraction']=max(0.,min(1.,(soft-weight)/max(1.,float(soft))))
                if now<cooldown or weight>=soft:result['reasons'].append('API_BUDGET_CONSTRAINED')
                if weight>=policy.idle_rest_weight:result['idle']=False
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='interactive_budget'").fetchone():
                    lease=db.execute('SELECT expires FROM interactive_budget WHERE id=1').fetchone()
                    if lease and lease[0]>now:result['reasons'].append('INTERACTIVE_REVIEW_PRIORITY')
    except (OSError,sqlite3.Error,ValueError,TypeError):result['reasons'].append('RESOURCE_EVIDENCE_UNAVAILABLE')
    if result['reasons']:result.update(idle=False,allow_research=False)
    return result


def high_frequency(rows,now,policy):
    """A capped response can prove a LOWER BOUND, not full history quality.

    Distinct exchange order IDs prevent fragmented fills counting as bots.
    Missing order IDs cannot establish the high-frequency classification.
    """
    if not isinstance(rows,list) or len(rows)>2000:raise HistoryIncomplete('INVALID_SCREENING_RESPONSE')
    orders=set();minutes=set();stamps=[]
    for row in rows:
        _key(row,max(0,now-DAY),now)
        if row.get('oid') is None:continue
        orders.add((row['coin'],str(row['oid'])))
        stamps.append(row['time']);minutes.add(row['time']//60000)
    span=max(stamps,default=0)-min(stamps,default=0)
    return dict(suspected=len(orders)>=policy.high_frequency_orders and span>=policy.high_frequency_span_ms
        and len(minutes)>=policy.high_frequency_active_minutes,distinct_orders=len(orders),
        active_minutes=len(minutes),observed_span_ms=span,evidence='EXCHANGE_ORDER_RATE_LOWER_BOUND',
        full_history_complete=len(rows)<2000)


def supported_account(info,address,now,clock=None):
    """Conservative current zero-capital evidence in supported main/xyz pools.

    Spot assets of any type, any position, unsupported/missing data prevent a
    zero-capital classification. Never claims total wealth across all DEXs.
    """
    from core.capital_snapshot import finite_amount
    totals=[];positions=0;stamps=[];clock=clock or (lambda:now)
    for dex in ('','xyz'):
        state=info({'type':'clearinghouseState','user':address,'dex':dex})
        now=clock()
        stamp=state.get('time') if isinstance(state,dict) else None
        if type(stamp) is not int or not 0<=now-stamp<=30000:raise ValueError('SCREEN_ACCOUNT_STALE')
        rows=state.get('assetPositions')
        if not isinstance(rows,list):raise ValueError('SCREEN_POSITIONS_UNAVAILABLE')
        for item in rows:
            size=float(item['position']['szi'])
            if not math.isfinite(size):raise ValueError('SCREEN_POSITION_INVALID')
            positions+=int(size!=0)
        totals.append(finite_amount(state['marginSummary']['accountValue'],'screen capital'));stamps.append(stamp)
    spot=info({'type':'spotClearinghouseState','user':address})
    now=clock()
    balances=spot.get('balances') if isinstance(spot,dict) else None
    if not isinstance(balances,list):raise ValueError('SCREEN_SPOT_UNAVAILABLE')
    spot_assets=any(finite_amount(row['total'],'spot total')!=0 for row in balances)
    if any(not 0<=now-stamp<=30000 for stamp in stamps):raise ValueError('SCREEN_ACCOUNT_STALE')
    return {'zero_supported_capital':all(v==0 for v in totals) and not spot_assets and not positions,
        'open_positions':positions,'supported_pools':['','xyz'],'pool_equity':totals,
        'spot_assets_present':spot_assets,'exchange_ms':min(stamps),'received_ms':now,
        'evidence':'SUPPORTED_POOL_ACCOUNT_SNAPSHOTS_NOT_TOTAL_WALLET_WEALTH'}
