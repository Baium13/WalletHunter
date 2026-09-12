"""Pure restricted agents: no IO, execution imports, credentials or mutations."""
import math
import statistics
from .models import AgentResult, ConsensusDecision


def evaluate(event, leader, candles, book, now, policy, *, context=None, actionable=False, flow=None):
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
        if any(b-a!=900000 for a,b in zip(times,times[1:])): raise ValueError('Candle gap')
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
        if not math.isfinite(bid+ask): raise ValueError('Price arithmetic overflow')
        depth=min(sum(number(x['px'])*number(x['sz']) for x in side[:5]) for side in (bids,asks))
        if not math.isfinite(depth): raise ValueError('Depth overflow')
        spread=(ask-bid)/((ask+bid)/2)*10000
        spread_limit,depth_limit=policy.liquidity_for(event.instrument)
        wide=not 0 <= spread <= spread_limit
        thin=depth < depth_limit
        # A wide spread is an execution-cost problem that a smaller order does
        # not fix, so it BLOCKs. Thin depth only bounds how much can be filled,
        # which is a sizing question: it reports CAUTION so weighted consensus
        # shrinks the order rather than refusing the market outright.
        state='BLOCK' if wide else 'CAUTION' if thin else 'PASS'
        output.append(result('liquidity',state,1.,-1. if wide or thin else 1.,
            ('SPREAD_LIMIT',) if wide else ('DEPTH_LIMIT',) if thin else ('BOOK_SPREAD_DEPTH',),'FRESH'))
    except (ValueError,KeyError,TypeError,OverflowError): output.append(result('liquidity'))
    # Depth is not aggressive order flow. No fabricated neutral flow score:
    # without a window of real taker prints this stays INSUFFICIENT_EVIDENCE,
    # and a zero-confidence agent changes no decision.
    try:
        if not isinstance(flow,dict): raise ValueError('No flow window')
        prints=flow['prints'];notional=number(flow['notional']);imbalance=float(flow['imbalance'])
        stamp=flow['exchange_ms']
        if type(prints) is not int or prints<policy.flow_min_prints: raise ValueError('Thin sample')
        if type(stamp) is not int or not 0<=now-stamp<=policy.flow_max_age_ms: raise ValueError('Stale flow')
        if not math.isfinite(imbalance) or not -1<=imbalance<=1: raise ValueError('Invalid imbalance')
        # Confidence is sample adequacy, not conviction: a handful of small
        # prints must not speak as loudly as a sustained one-sided tape.
        weight=min(1.,notional/policy.flow_reference_usd) if policy.flow_reference_usd>0 else 1.
        output.append(result('order_flow','LONG' if imbalance>0 else 'SHORT' if imbalance<0 else 'PASS',
            min(1.,max(0.,weight)),imbalance,('TAKER_IMBALANCE',),'FRESH'))
    except (ValueError,KeyError,TypeError,OverflowError):
        output.append(result('order_flow'))
    direction='LONG' if event.side=='BUY' else 'SHORT'
    output.append(result('leader',direction if leader.qualified else 'CAUTION',leader.confidence,
        leader.score*(1 if direction=='LONG' else -1),('VERSIONED_LEADER_SCORE',),
        'FRESH' if 0 <= now-leader.computed_ms <= policy.reevaluate_ms else 'STALE'))
    fresh=0 <= now-event.exchange_ms <= policy.signal_window_ms(event.action)
    output.append(result('risk_context','PASS' if fresh else 'CAUTION',1.,1. if fresh else -1.,('SIGNAL_AGE',),'FRESH' if fresh else 'STALE'))
    if actionable or context is not None:
        from .risk_context import analyze
        output[-1]=analyze(event,output,context,now,policy)
    return tuple(output)


def _entry_gates(by_id, soft_caution):
    """Availability gates an entry must clear before conviction is even scored.

    ``leader`` must be qualified and FRESH: copying a wallet we do not rate is
    the one thing this system must never do, and an unqualified leader reports
    CAUTION. With ``soft_caution`` (WEIGHTED entries) a CAUTION from
    ``volatility``, ``liquidity`` or ``risk_context`` stops vetoing and becomes
    an attenuation instead. That is not a hole: a wide spread reports BLOCK
    from the liquidity agent, every genuine financial failure inside
    risk_context reports BLOCK, and BLOCK still vetoes here. Only thin depth
    and abnormal volatility - both of which a smaller order answers - become
    sizing input.
    """
    blockers=[]
    for key in ('liquidity','risk_context','leader','volatility'):
        veto={'WAIT','BLOCK'} if key in soft_caution else {'WAIT','CAUTION','BLOCK'}
        a=by_id.get(key)
        if a is None or a.freshness!='FRESH' or a.direction in veto:
            blockers.append(key+'_UNAVAILABLE_OR_BLOCKING')
    return blockers


def _exit_gates(by_id):
    """Gates for a reduction of a position we have already proven we hold.

    Closing is not a new opinion about the market, so it cannot depend on the
    leader's CURRENT qualification score, and it must not depend on volatility
    being calm - abnormal volatility is a reason to get out, not a reason to be
    trapped in. What remains are the two checks needed to place the reduction
    itself: a usable book and a financial context that does not BLOCK.
    """
    blockers=[]
    # A usable book is an ABILITY to place the reduction, so liquidity still
    # has to be fresh and not blocking.
    book=by_id.get('liquidity')
    if book is None or book.freshness!='FRESH' or book.direction in {'WAIT','BLOCK'}:
        blockers.append('liquidity_UNAVAILABLE_OR_BLOCKING')
    # risk_context is different, and the docstring above already said so: only
    # a real financial failure (BLOCK) may stop an exit. Blocking on WAIT, on a
    # missing reading or on staleness trapped the operator in the position
    # whenever the context feed lagged - measured as the single most common
    # exit refusal on a live PAPER run. Shedding exposure cannot breach an
    # allocation limit, so an unknown context is not a reason to keep holding.
    context=by_id.get('risk_context')
    if context is not None and context.direction=='BLOCK':
        blockers.append('risk_context_UNAVAILABLE_OR_BLOCKING')
    return blockers


def consensus(event, agents, now, policy, *, position=None):
    weighted=policy.consensus_mode=='WEIGHTED'
    by_id={a.agent_id:a for a in agents}
    blockers=[];attenuation=[]
    if len(by_id)!=len(agents): blockers.append('DUPLICATE_AGENT')
    if any(a.instrument!=event.instrument or a.created_ms>now or now-a.created_ms>policy.max_market_age_ms for a in agents):
        blockers.append('AGENT_SCOPE_OR_AGE')
    # An exit keeps its own, much wider window: see max_exit_signal_age_ms.
    # An unproven reduction is still refused by POSITION_LIFECYCLE_REQUIRED
    # below, so the wider window can never authorize an unowned exit.
    reducing=event.action in {'REDUCE','CLOSE'}
    # A CLOSE on a position we have PROVEN we hold is never refused for age.
    # Signal age says whether a market opportunity has passed; it says nothing
    # about whether we should still be carrying exposure the leader has already
    # abandoned. The risk is one-way - this can only reduce a position, never
    # open one - and an unproven exit is still refused by
    # POSITION_LIFECYCLE_REQUIRED below. REDUCE keeps its window: a partial
    # trim IS a sizing opinion, and a stale one is worth refusing.
    if not (event.action=='CLOSE' and position is not None):
        if not 0<=now-event.exchange_ms<=policy.signal_window_ms(event.action):
            blockers.append('STALE_SIGNAL')
    if reducing and position is not None:
        if (position.instrument!=event.instrument or position.evidence!='VERIFIED' or
                (position.side=='LONG')==(event.side=='BUY')):
            blockers.append('REDUCTION_OWNERSHIP_OR_SIDE')
    elif event.action in {'REDUCE','CLOSE','REVERSE'}:
        # Directional entry research cannot authorize an exit or interpret a
        # reducing SELL as a new SHORT. Position-aware lifecycle is required.
        blockers.append('POSITION_LIFECYCLE_REQUIRED')
    proven=reducing and position is not None
    if weighted and proven: blockers+=_exit_gates(by_id)
    else: blockers+=_entry_gates(by_id,{'volatility','risk_context','liquidity'} if weighted else set())
    if reducing and position is not None:
        # A reducing SELL is not a SHORT entry. Entry trend votes cannot turn
        # a proven exit into new exposure; fresh contextual checks still apply.
        confidence=min((by_id[k].confidence for k in ('leader','risk_context','liquidity') if k in by_id),default=0.)
        return ConsensusDecision(event_id=event.event_id,policy_id=policy.policy_id,created_ms=now,
            decision='WAIT' if blockers else ('COPY_LONG' if event.side=='BUY' else 'COPY_SHORT'),
            score=confidence*(1 if event.side=='BUY' else -1),confidence=confidence,
            participants=tuple(sorted(by_id)),blockers=tuple(blockers),supporting=('leader','risk_context','liquidity'),opposing=())
    # Correlated structure/momentum form ONE trend group, not independent votes.
    trend=[a for a in agents if a.agent_id in {'structure','momentum'} and a.freshness=='FRESH']
    trend_score=sum(a.score*a.confidence for a in trend)/max(sum(a.confidence for a in trend),1e-12)
    lead=by_id.get('leader')
    lead_score=lead.score if lead and lead.freshness=='FRESH' else 0.
    desired=1 if event.side=='BUY' else -1
    if not weighted:
        value=(trend_score+lead_score)/2
        if trend_score*lead_score < 0: blockers.append('STRONG_DISAGREEMENT')
        confidence=abs(value)*(lead.confidence if lead else 0.)
        if value*desired < policy.consensus_threshold: blockers.append('CONSENSUS_BELOW_THRESHOLD')
    else:
        # The agents size the trade instead of refusing it. The leader's own
        # rated quality is the premise; 15m trend agreement, abnormal realized
        # volatility and a cautionary financial context each scale conviction
        # DOWN and never up, so a contrarian or noisy entry becomes a small
        # position rather than no position. A leader entering against the
        # short-term trend is a lower-conviction trade, not an impossible one.
        alignment=max(-1.,min(1.,trend_score*desired))
        conviction=max(0.,min(1.,1.+policy.trend_weight*(alignment-1.)))
        if alignment<1.: attenuation.append('TREND_DISAGREEMENT' if alignment<0 else 'TREND_PARTIAL')
        vol=by_id.get('volatility')
        if vol is not None and vol.direction=='CAUTION':
            conviction*=1.-policy.volatility_penalty;attenuation.append('VOLATILITY_CAUTION')
        book=by_id.get('liquidity')
        if book is not None and book.direction=='CAUTION':
            # Thin depth bounds the size, so it takes size away rather than the
            # whole market. A wide spread arrives as BLOCK and never gets here.
            conviction*=1.-policy.depth_penalty;attenuation.append('DEPTH_CAUTION')
        # Aggressive taker flow is an independent read on the same entry, so it
        # scales conviction on its own rather than joining the trend group. Its
        # confidence is sample adequacy, so a thin or absent tape fades to zero
        # influence instead of inventing a neutral vote.
        stream=by_id.get('order_flow')
        if stream is not None and stream.freshness=='FRESH' and stream.confidence>0:
            agreement=max(-1.,min(1.,stream.score*desired))
            conviction*=max(0.,min(1.,1.+policy.flow_weight*stream.confidence*(agreement-1.)))
            if agreement<1.: attenuation.append('FLOW_DISAGREEMENT' if agreement<0 else 'FLOW_PARTIAL')
        context=by_id.get('risk_context')
        if context is not None and context.direction=='CAUTION':
            conviction*=1.-policy.context_caution_penalty;attenuation.append('CONTEXT_CAUTION')
        # The allocator already multiplies by the leader's confidence, so it is
        # deliberately NOT applied a second time here; squaring it was what made
        # every sized entry collapse under the minimum notional.
        confidence=max(0.,min(1.,abs(lead_score)*conviction))
        value=desired*confidence
        # A floor, not a trend test: the worst combination an attenuated entry
        # can reach still lands below it and is refused.
        if confidence < policy.min_entry_confidence: blockers.append('CONSENSUS_BELOW_THRESHOLD')
    return ConsensusDecision(event_id=event.event_id,policy_id=policy.policy_id,created_ms=now,
        decision='WAIT' if blockers else ('COPY_LONG' if desired>0 else 'COPY_SHORT'),score=value,
        confidence=confidence,participants=tuple(sorted(by_id)),blockers=tuple(blockers),
        attenuation=tuple(attenuation),
        supporting=tuple(a.agent_id for a in agents if a.score*desired>0 and a.agent_id in {'structure','momentum','leader'}),
        opposing=tuple(a.agent_id for a in agents if a.score*desired<0 and a.agent_id in {'structure','momentum','leader'}))
