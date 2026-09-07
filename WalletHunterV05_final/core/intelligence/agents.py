"""Pure restricted agents: no IO, execution imports, credentials or mutations."""
import math
import statistics
from .models import AgentResult, ConsensusDecision


def evaluate(event, leader, candles, book, now, policy):
    def result(name, direction='WAIT', confidence=0., score=0., evidence=('INSUFFICIENT_EVIDENCE',), freshness='UNKNOWN'):
        return AgentResult(agent_id=name,instrument=event.instrument,created_ms=now,direction=direction,
            confidence=confidence,score=score,evidence=evidence,freshness=freshness)
    output = []
    def number(v):
        if isinstance(v,bool): raise ValueError('Invalid number')
        x=float(v)
        if not math.isfinite(x) or x <= 0: raise ValueError('Invalid number')
        return x
    try:
        if len(candles) < 32 or len(candles) > 128: raise ValueError('Window unavailable')
        times = [c['T'] for c in candles]
        if any(type(t) is not int for t in times) or times != sorted(set(times)) or times[-1] > now or now-times[-1] > 900000:
            raise ValueError('Stale candles')
        closes=[number(c['c']) for c in candles]
        highs=[number(c['h']) for c in candles]
        lows=[number(c['l']) for c in candles]
        if any(l > c or c > h for l,c,h in zip(lows,closes,highs)): raise ValueError('Malformed OHLC')
        structural = 1 if max(highs[-8:]) > max(highs[-16:-8]) and min(lows[-8:]) > min(lows[-16:-8]) else -1 if max(highs[-8:]) < max(highs[-16:-8]) and min(lows[-8:]) < min(lows[-16:-8]) else 0
        output.append(result('structure','LONG' if structural>0 else 'SHORT' if structural<0 else 'WAIT',.7 if structural else 0.,float(structural),('SWING_WINDOWS',),'FRESH'))
        def ema(period):
            e=closes[0]
            for c in closes[1:]: e += 2/(period+1)*(c-e)
            return e
        momentum=math.tanh((ema(8)/ema(21)-1)*100)
        output.append(result('momentum','LONG' if momentum>0 else 'SHORT',abs(momentum),momentum,('EMA_8_21',),'FRESH'))
        returns=[math.log(b/a) for a,b in zip(closes,closes[1:])]
        volatility=statistics.pstdev(returns)
        abnormal=abs(returns[-1]) > max(volatility*4,.03)
        output.append(result('volatility','CAUTION' if abnormal else 'PASS',.8, -1. if abnormal else .5,('REALIZED_VOLATILITY',),'FRESH'))
    except (ValueError,KeyError,TypeError,OverflowError):
        output=[result(n) for n in ('structure','momentum','volatility')]
    try:
        if type(book['time']) is not int or not 0 <= now-book['time'] <= policy.max_market_age_ms: raise ValueError('Stale book')
        bids, asks=book['levels']
        if not bids or not asks or max(len(bids),len(asks)) > 100: raise ValueError('Depth unavailable')
        bid_prices=[number(x['px']) for x in bids]
        ask_prices=[number(x['px']) for x in asks]
        if bid_prices!=sorted(set(bid_prices),reverse=True) or ask_prices!=sorted(set(ask_prices)):
            raise ValueError('Unordered depth')
        bid,ask=number(bids[0]['px']),number(asks[0]['px'])
        depth=min(sum(number(x['px'])*number(x['sz']) for x in side[:5]) for side in (bids,asks))
        if not math.isfinite(depth): raise ValueError('Depth overflow')
        spread=(ask-bid)/((ask+bid)/2)*10000
        good=0 <= spread <= policy.max_spread_bps and depth >= policy.minimum_depth_usd
        output.append(result('liquidity','PASS' if good else 'CAUTION',1.,1. if good else -1.,('BOOK_SPREAD_DEPTH',),'FRESH'))
    except (ValueError,KeyError,TypeError,OverflowError): output.append(result('liquidity'))
    # Depth is not aggressive order flow. No fabricated neutral flow score.
    output.append(result('order_flow'))
    direction='LONG' if event.side=='BUY' else 'SHORT'
    output.append(result('leader',direction if leader.qualified else 'CAUTION',leader.confidence,
        leader.score*(1 if direction=='LONG' else -1),('VERSIONED_LEADER_SCORE',),
        'FRESH' if 0 <= now-leader.computed_ms <= policy.reevaluate_ms else 'STALE'))
    fresh=0 <= now-event.exchange_ms <= policy.max_signal_age_ms
    output.append(result('risk_context','PASS' if fresh else 'CAUTION',1.,1. if fresh else -1.,('SIGNAL_AGE',),'FRESH' if fresh else 'STALE'))
    return tuple(output)


def consensus(event, agents, now, policy):
    by_id={a.agent_id:a for a in agents}
    blockers=[]
    if len(by_id)!=len(agents): blockers.append('DUPLICATE_AGENT')
    if any(a.instrument!=event.instrument or a.created_ms>now or now-a.created_ms>policy.max_market_age_ms for a in agents):
        blockers.append('AGENT_SCOPE_OR_AGE')
    if not 0<=now-event.exchange_ms<=policy.max_signal_age_ms: blockers.append('STALE_SIGNAL')
    if event.action in {'REDUCE','CLOSE','REVERSE'}:
        # Directional entry research cannot authorize an exit or interpret a
        # reducing SELL as a new SHORT. Position-aware lifecycle is required.
        blockers.append('POSITION_LIFECYCLE_REQUIRED')
    for key in ('liquidity','risk_context','leader','volatility'):
        a=by_id.get(key)
        if a is None or a.freshness!='FRESH' or a.direction in {'WAIT','CAUTION'}: blockers.append(key+'_UNAVAILABLE_OR_BLOCKING')
    # Correlated structure/momentum form ONE trend group, not independent votes.
    trend=[a for a in agents if a.agent_id in {'structure','momentum'} and a.freshness=='FRESH']
    trend_score=sum(a.score*a.confidence for a in trend)/max(sum(a.confidence for a in trend),1e-12)
    lead=by_id.get('leader')
    lead_score=lead.score if lead and lead.freshness=='FRESH' else 0.
    value=(trend_score+lead_score)/2
    disagreement=trend_score*lead_score < 0
    if disagreement: blockers.append('STRONG_DISAGREEMENT')
    confidence=abs(value)*(lead.confidence if lead else 0.)
    desired=1 if event.side=='BUY' else -1
    if value*desired < policy.consensus_threshold: blockers.append('CONSENSUS_BELOW_THRESHOLD')
    return ConsensusDecision(event_id=event.event_id,policy_id=policy.policy_id,created_ms=now,
        decision='WAIT' if blockers else ('COPY_LONG' if desired>0 else 'COPY_SHORT'),score=value,
        confidence=confidence,participants=tuple(sorted(by_id)),blockers=tuple(blockers),
        supporting=tuple(a.agent_id for a in agents if a.score*desired>0 and a.agent_id in {'structure','momentum','leader'}),
        opposing=tuple(a.agent_id for a in agents if a.score*desired<0 and a.agent_id in {'structure','momentum','leader'}))
