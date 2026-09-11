"""Deterministic descriptive leader research, never copy admission."""
import math
import json
from decimal import Decimal
from dataclasses import asdict
from core.trade_analyzer import TradeAnalyzer, _unique, _float
from .models import LeaderScore

DAY = 86400000


def behavior(fills, report):
    """Reuse validated/de-duplicated fills; never infer leverage from notional.

    An execution fragment is not a complete position episode. Sizing metrics
    therefore explicitly describe fills, not percentage of historical equity.
    """
    rows,_,_=_unique(fills,False)
    values=sorted(r['notional'] for r in rows)
    by_symbol={}
    long=short=unknown=Decimal(0)
    for row in rows:
        by_symbol[row['coin']]=by_symbol.get(row['coin'],Decimal(0))+row['notional']
        raw=json.loads(row['canonical'])
        before=raw['startPosition']
        if before is not None and raw['side'] in {'A','B'}:
            before=Decimal(before)
            size=Decimal(raw['sz']); px=Decimal(raw['px'])
            delta=size if raw['side']=='B' else -size
            # Reversal fills contain both a closing leg and a new opening leg.
            closing=min(abs(before),size) if before*delta<0 else Decimal(0)
            opening=size-closing
            long += ((closing if before>0 else 0)+(opening if delta>0 else 0))*px
            short += ((closing if before<0 else 0)+(opening if delta<0 else 0))*px
        elif raw['dir'] in {'Open Long','Close Long'}: long+=row['notional']
        elif raw['dir'] in {'Open Short','Close Short'}: short+=row['notional']
        else: unknown+=row['notional']
    total=sum(values,Decimal(0)); count=len(values)
    median=values[count//2] if count%2 else (values[count//2-1]+values[count//2])/2
    return {
        'evidence_type':'VALIDATED_REALIZED_FILL_CASHFLOWS',
        'trade_count_unit':'CLOSING_FILLS_NOT_POSITION_EPISODES',
        'drawdown_proxy':{'value_usdc':report.max_drawdown,'kind':'CUMULATIVE_FILL_CASHFLOW_PEAK_TO_TROUGH',
                          'includes_execution_fees':True,'includes_funding':False,'account_equity_drawdown_pct':None},
        'payoff_ratio':_float(Decimal(str(report.avg_win))/abs(Decimal(str(report.avg_loss))),'payoff_ratio') if report.losses and report.avg_loss else None,
        'leverage_behavior':{'value':None,'reason':'LEVERAGE_NOT_PRESENT_IN_FILL_HISTORY'},
        'position_sizing_behavior':{'unit':'FILL_NOTIONAL_USDC','count':count,
            'mean':_float(total/count,'mean_fill_notional'),'median':_float(median,'median_fill_notional'),
            'minimum':_float(values[0],'minimum_fill_notional'),'maximum':_float(values[-1],'maximum_fill_notional'),
            'historical_equity_fraction':None,'whole_position_size':None},
        'symbol_notional_share':{k:_float(v/total,'symbol_share') for k,v in sorted(by_symbol.items())},
        'long_short_bias':{'unit':'TRADED_NOTIONAL_BY_POSITION_DIRECTION',
            'long':_float(long/total,'long_share'),'short':_float(short/total,'short_share'),
            'unknown':_float(unknown/total,'unknown_share')},
    }


def reports(fills, now):
    result = {}
    for days in (30,90,180):
        window = [f for f in fills if now-days*DAY <= f['time'] <= now]
        if not window:
            result[str(days)] = None
            continue
        report = TradeAnalyzer().report(window)
        row = asdict(report)
        row['methodology'] = row.pop('_metadata')
        # Infinity is a mathematical PF with no losing cashflow, not a JSON number.
        row['profit_factor_unbounded'] = math.isinf(report.profit_factor)
        row['profit_factor'] = report.profit_factor if math.isfinite(report.profit_factor) else None
        row['deep_analysis']=behavior(window,report)
        result[str(days)] = row
    return result


def score(wallet, network, windows, now, policy):
    historical = windows.get('180') or windows.get('90') or windows.get('30')
    if not historical: raise ValueError('HISTORY_UNAVAILABLE')
    h, recent = historical, windows.get('30')
    reasons = []
    if h['trades'] < policy.min_closes: reasons.append('SMALL_SAMPLE')
    if h['active_days'] < policy.min_activity_days: reasons.append('INSUFFICIENT_DAYS')
    if h['expectancy'] <= 0 or h['net_pnl'] <= 0: reasons.append('NEGATIVE_EXPECTANCY')
    pf = 3. if h['profit_factor_unbounded'] else h['profit_factor']
    if pf is None: raise ValueError('PF_UNAVAILABLE')
    profit = min(1., max(0., (pf-1)/2)) if h['expectancy'] > 0 else 0.
    concentration = min(1., max(0., h['top_coin_share']/100))
    lucky = min(1., h['best']/h['gross_profit']) if h['gross_profit'] > 0 else 1.
    anomaly = max(lucky, max(0., concentration-.8))
    if lucky > .5: reasons.append('DOMINANT_WIN')
    # Cashflow drawdown ratio, explicitly NOT a fabricated account-equity DD%.
    dd_quality = 1/(1+h['max_drawdown']/max(h['gross_profit'], 1e-12))
    if dd_quality < .5: reasons.append('CASHFLOW_DRAWDOWN')
    sample = min(1., h['trades']/(policy.min_closes*3))
    consistency = h['positive_days']/h['active_days'] if h['active_days'] else 0.
    recent_quality = min(1., max(0., recent['expectancy']/max(h['expectancy'], 1e-12))) if recent else 0.
    if not recent or recent['net_pnl'] <= 0: reasons.append('RECENT_DETERIORATION')
    components = (profit, consistency, dd_quality, sample, recent_quality)
    value = max(0., sum(w*x for w,x in zip(policy.weights, components))*(1-anomaly*.5))
    confidence = sample*min(1., h['active_days']/30)*(1-lucky)
    return LeaderScore(wallet=wallet,network=network,policy_id=policy.policy_id,computed_ms=now,
        score=value,confidence=confidence,profit_quality=profit,consistency=consistency,
        drawdown_quality=dd_quality,sample_quality=sample,recent_quality=recent_quality,anomaly=anomaly,
        qualified=not reasons and value >= policy.promotion_score and confidence >= policy.promotion_confidence,
        reasons=tuple(reasons))
