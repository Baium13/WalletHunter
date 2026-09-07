"""Conservative replay of a leader's closing fills for a follower account."""
from dataclasses import dataclass
from math import sqrt


@dataclass
class Simulation:
    trades: int; net_pnl: float; profit_factor: float; max_drawdown: float
    win_rate: float; cost_usd: float; train_pf: float; test_pf: float
    score: int; eligible: bool; daily_pnl: dict


class FollowerSimulator:
    # Conservative base tier: 4.5 bps taker fee on every entry and exit.
    def __init__(self, taker_fee_bps=4.5, slippage_bps=3.0):
        self.taker_fee_bps = float(taker_fee_bps)
        self.slippage_bps = float(slippage_bps)

    @staticmethod
    def _pf(values):
        wins = sum(x for x in values if x > 0)
        losses = abs(sum(x for x in values if x < 0))
        return wins / losses if losses else (float("inf") if wins else 0.0)

    def simulate(self, fills, scale=1.0):
        """Use closing fills as observable trade outcomes and subtract follower costs.

        This is deliberately conservative: it treats each realized close as if
        follower paid taker fee and slippage on both entry and exit.
        """
        events = []
        daily = {}
        for fill in fills or []:
            try:
                pnl = float(fill.get("closedPnl", 0) or 0) * scale
                notional = abs(float(fill.get("sz", 0) or 0) * float(fill.get("px", 0) or 0)) * scale
                stamp = int(fill.get("time", 0) or 0)
            except (TypeError, ValueError):
                continue
            if abs(pnl) < 1e-12 or notional <= 0: continue
            cost = notional * (self.taker_fee_bps + self.slippage_bps) * 2 / 10_000
            net = pnl - cost
            events.append((stamp, net, cost))
            day = str(stamp // 86_400_000)
            daily[day] = daily.get(day, 0.0) + net
        events.sort()
        pnls = [x[1] for x in events]
        costs = sum(x[2] for x in events)
        equity = peak = drawdown = 0.0
        for value in pnls:
            equity += value; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
        split = max(1, int(len(pnls) * 2 / 3))
        train, test = pnls[:split], pnls[split:]
        train_pf, test_pf = self._pf(train), self._pf(test)
        net = sum(pnls); wr = sum(1 for x in pnls if x > 0) / len(pnls) * 100 if pnls else 0.0
        pf = self._pf(pnls)
        score = min(100, max(0, round((min(pf, 3) / 3) * 35 + (min(train_pf, 3) / 3) * 20 +
                                       (min(test_pf, 3) / 3) * 30 + min(len(pnls), 100) / 100 * 15)))
        eligible = len(pnls) >= 30 and net > 0 and train_pf >= 1.15 and test_pf >= 1.10 and drawdown <= max(net * 2, 1.0)
        return Simulation(len(pnls), net, pf, drawdown, wr, costs, train_pf, test_pf, score, eligible, daily)

    @staticmethod
    def correlation(left, right):
        keys = sorted(set(left) & set(right))
        if len(keys) < 5: return 0.0
        a, b = [left[k] for k in keys], [right[k] for k in keys]
        ma, mb = sum(a)/len(a), sum(b)/len(b)
        den = sqrt(sum((x-ma)**2 for x in a) * sum((x-mb)**2 for x in b))
        return sum((x-ma)*(y-mb) for x, y in zip(a,b)) / den if den else 0.0
