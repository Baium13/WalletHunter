"""Deterministic descriptive leader research, never copy admission."""
import math
from dataclasses import asdict
from core.trade_analyzer import TradeAnalyzer
from .models import LeaderScore

DAY = 86400000


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
