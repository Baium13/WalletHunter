"""Descriptive realised-fill accounting, not an equity curve or copy backtest.

All USDC execution fees (including opening fees) affect net cashflow. Funding,
unrealised PnL, deposits and withdrawals are absent from fills and are excluded.
No current balance is used to fabricate past capital, capital ROI or Sharpe.
"""
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import math


class AnalysisUnavailable(ValueError):
    """Missing or contradictory facts must not become a profitable report."""


@dataclass
class Report:
    trades: int; wins: int; losses: int; win_rate: float
    gross_profit: float; gross_loss: float; net_pnl: float; profit_factor: float
    avg_win: float; avg_loss: float; best: float; worst: float
    max_drawdown: float; max_drawdown_pct: float | None
    avg_roi: float | None; median_roi: float | None; expectancy: float; sharpe: float | None
    consistency_score: float; risk_score: float | None; quality_score: float; rating: int
    positive_days: int; active_days: int; top_coin_share: float
    _metadata: dict = field(default_factory=dict, repr=False)

    @property
    def methodology(self):
        return deepcopy(self._metadata)

    @property
    def recommendation(self):
        if self.trades < 15:
            return "⚪ МАЛО ЗАКРЫВАЮЩИХ ИСПОЛНЕНИЙ ДЛЯ ОЦЕНКИ"
        if self.rating >= 75:
            return "🟢 ВЫСОКИЙ ИСТОРИЧЕСКИЙ БАЛЛ · НЕ ДОПУСК К КОПИРОВАНИЮ"
        if self.rating >= 55:
            return "🟡 СМЕШАННЫЕ ИСТОРИЧЕСКИЕ ПОКАЗАТЕЛИ"
        return "🔴 СЛАБЫЕ ИСТОРИЧЕСКИЕ ПОКАЗАТЕЛИ"


def _number(value, name):
    if value is None or isinstance(value, bool): raise AnalysisUnavailable(f"Missing or invalid fill {name}")
    try: result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError): raise AnalysisUnavailable(f"Missing or invalid fill {name}") from None
    if not result.is_finite() or not math.isfinite(float(result)):
        raise AnalysisUnavailable(f"Non-finite fill {name}")
    return result


def _float(value, name):
    result = float(value)
    if not math.isfinite(result): raise AnalysisUnavailable(f"Computed {name} exceeds supported finite range")
    return result


def _integer(value, name):
    number = _number(value, name)
    if number != int(number) or number < 0: raise AnalysisUnavailable(f"Invalid integer {name}")
    return int(number)


def _normalise(fill, allow_missing_fee_token_for_zero_fee=False):
    if not isinstance(fill, dict): raise AnalysisUnavailable("Fill must be an object")
    coin, side, direction = fill.get("coin"), fill.get("side"), fill.get("dir")
    if not isinstance(coin, str) or not coin.strip(): raise AnalysisUnavailable("Missing fill coin")
    if side is not None and side not in {"A", "B"}: raise AnalysisUnavailable("Invalid fill side")
    known = {"Open Long", "Open Short", "Close Long", "Close Short", "Long > Short", "Short > Long"}
    if direction is not None and direction not in known: raise AnalysisUnavailable("Unsupported perpetual fill direction")
    time = _integer(fill.get("time"), "time")
    pnl, fee, size, price = (_number(fill.get(name), name) for name in ("closedPnl", "fee", "sz", "px"))
    if size <= 0 or price <= 0: raise AnalysisUnavailable("Fill price and size must be positive")
    _float(size*price, "fill_notional")
    token = fill.get("feeToken")
    token_unspecified_zero = token is None and fee == 0 and allow_missing_fee_token_for_zero_fee is True
    if token != "USDC" and not token_unspecified_zero:
        raise AnalysisUnavailable("USDC feeToken is required; currency conversion is unavailable")
    before = None if "startPosition" not in fill else _number(fill["startPosition"], "startPosition")
    has_closed_quantity = before is not None and side is not None and before * (1 if side == "B" else -1) < 0
    if direction is not None and side is not None:
        buys = {"Open Long", "Close Short", "Short > Long"}
        if (side == "B") != (direction in buys): raise AnalysisUnavailable("Fill direction conflicts with side")
    if direction is not None and before is not None and side is not None:
        delta = size*(1 if side == "B" else -1)
        after = before+delta
        if before == 0 or before*delta > 0: expected = "Open Long" if delta > 0 else "Open Short"
        elif before*after < 0: expected = "Long > Short" if before > 0 else "Short > Long"
        else: expected = "Close Long" if before > 0 else "Close Short"
        if expected != direction: raise AnalysisUnavailable("Fill direction conflicts with signed transition")
    # Breakeven closes must remain in the denominator, even with closedPnl=0.
    closing = direction in {"Close Long", "Close Short", "Long > Short", "Short > Long"} or has_closed_quantity
    if pnl != 0 and not closing:
        if direction in {"Open Long", "Open Short"} or (before is not None and side is not None):
            raise AnalysisUnavailable("Non-closing fill has realised PnL")
        # Older fixtures may omit dir/startPosition. A nonzero closedPnl itself
        # establishes realised closing activity, but zero does not establish it.
        closing = True
    if direction in {"Open Long", "Open Short"} and has_closed_quantity:
        raise AnalysisUnavailable("Fill direction conflicts with signed position")
    if direction in {"Close Long", "Close Short", "Long > Short", "Short > Long"} and before is not None and side is not None and not has_closed_quantity:
        raise AnalysisUnavailable("Closing direction conflicts with signed position")
    if direction is None and pnl == 0 and not (before == 0 or (before is not None and side is not None)):
        raise AnalysisUnavailable("Zero-PnL fill needs direction or startPosition to identify breakeven closes")
    if fill.get("builderFee") is not None: _number(fill["builderFee"], "builderFee")
    ids = {name: _integer(fill[name], name) for name in ("tid", "id", "oid") if fill.get(name) is not None}
    identity_kind = "tid" if "tid" in ids else "id" if "id" in ids else None
    identity = (identity_kind, ids[identity_kind], coin, side) if identity_kind else None
    canonical = {"coin": coin, "side": side, "dir": direction, "time": time,
                 "closedPnl": str(pnl.normalize()), "fee": str(fee.normalize()), "feeToken": token,
                 "sz": str(size.normalize()), "px": str(price.normalize()),
                 "startPosition": str(before.normalize()) if before is not None else None, "ids": ids}
    return {"time": time, "coin": coin, "pnl": pnl, "fee": fee, "cashflow": pnl-fee,
            "notional": size*price, "closing": closing, "identity": identity,
            "canonical": json.dumps(canonical, sort_keys=True), "zero_fee_token_unspecified": token_unspecified_zero}


def _unique(fills, allow_missing_fee_token_for_zero_fee):
    if fills is None: fills = []
    if not isinstance(fills, (list, tuple)): raise AnalysisUnavailable("Fills must be a supplied list")
    found, identities, unidentified, duplicate_count = [], {}, set(), 0
    for fill in fills:
        row = _normalise(fill, allow_missing_fee_token_for_zero_fee)
        identity, canonical = row["identity"], row["canonical"]
        if identity is not None:
            if identity in identities:
                if identities[identity] != canonical: raise AnalysisUnavailable("Conflicting data for the same stable fill ID")
                duplicate_count += 1; continue
            identities[identity] = canonical
        else:
            # oid/time/hash alone do not distinguish identical partial fills.
            # An ambiguous duplicate must not be silently discarded or doubled.
            if canonical in unidentified: raise AnalysisUnavailable("Ambiguous duplicate fill without stable trade/fill ID")
            unidentified.add(canonical)
        found.append(row)
    return found, len(fills), duplicate_count


class TradeAnalyzer:
    @staticmethod
    def _median(values):
        values = sorted(values)
        if not values: return 0.0
        mid = len(values) // 2
        return values[mid] if len(values) % 2 else (values[mid-1]+values[mid])/2

    def report(self, fills, reference_balance=0.0, *, allow_missing_fee_token_for_zero_fee=False):
        """Account for supplied fills without pretending to know full history.

        trades/win_rate classify closing fills AFTER each closing fill's fee;
        entry fees cannot be assigned to their closed lots without a lifecycle
        ledger. All fees still affect net_pnl, the cashflow profit factor and
        realised cashflow drawdown. Funding is not present and is not guessed.

        Missing feeToken is rejected by default, including synthetic fixtures.
        Tests/legacy importers may explicitly permit it ONLY for a known zero
        fee; the assumption count is exposed. A nonzero unknown-token fee can
        never be assumed USDC. reference_balance is validated but NOT used to
        fabricate historical capital or a drawdown percentage.
        """
        reference = _number(reference_balance, "reference_balance")
        if reference < 0: raise AnalysisUnavailable("Reference balance must be nonnegative")
        if not isinstance(allow_missing_fee_token_for_zero_fee, bool): raise AnalysisUnavailable("Zero-fee token fixture policy must be explicit boolean")
        rows, supplied, duplicates = _unique(fills, allow_missing_fee_token_for_zero_fee)
        by_time, by_day, coin_notional = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
        for row in rows:
            by_time[row["time"]] += row["cashflow"]
            by_day[row["time"] // 86_400_000] += row["cashflow"]
            coin_notional[row["coin"]] += row["notional"]
        closing = [row["cashflow"] for row in rows if row["closing"]]
        wins, losses = [p for p in closing if p > 0], [p for p in closing if p < 0]
        n = len(closing)
        flows = [row["cashflow"] for row in rows]
        gross_profit = sum((p for p in flows if p > 0), Decimal(0))
        gross_loss = sum((p for p in flows if p < 0), Decimal(0))
        net = sum(flows, Decimal(0))
        pf = _float(gross_profit / -gross_loss, "profit_factor") if gross_loss else (float("inf") if gross_profit else 0.)
        cashflow = peak = max_dd = Decimal(0)
        for stamp in sorted(by_time):
            cashflow += by_time[stamp]
            peak = max(peak, cashflow)
            max_dd = max(max_dd, peak-cashflow)
        # Millisecond groups avoid inventing an intrablock order of fills.
        positive_days = sum(value > 0 for value in by_day.values())
        active_days = len(by_day)
        consistency = positive_days/active_days*100 if active_days else 0.
        gross_notional = sum(coin_notional.values(), Decimal(0))
        top_share = _float(max(coin_notional.values())/gross_notional*100, "top_coin_share") if gross_notional else 0.
        expectancy = net/n if n else Decimal(0)
        pf_score = 100 if pf >= 2 else 85 if pf >= 1.5 else 65 if pf >= 1.2 else 40 if pf >= 1 else 10
        sample_score = min(100., n/50*100)
        concentration_score = max(0., min(100., 120-top_share))
        # This is a descriptive heuristic, not calibrated risk, backtesting or
        # permission to copy. Unknown equity drawdown contributes no fake score.
        quality = (pf_score*.30 + consistency*.15 + sample_score*.15 +
                   (100 if net > 0 else 0)*.10 + concentration_score*.05)/.75 if n else 0.
        metadata = {
            "method": "realized-fill-cashflow-v2", "currency": "USDC",
            "input_fills": supplied, "unique_fills": len(rows), "duplicate_fills_removed": duplicates,
            "fills_without_stable_id": sum(row["identity"] is None for row in rows),
            "closing_fills": n, "breakeven_closing_fills": sum(p == 0 for p in closing),
            "opening_or_nonclosing_fills": len(rows)-n,
            "zero_fee_missing_token_explicitly_allowed": sum(row["zero_fee_token_unspecified"] for row in rows),
            "fees_signed_usdc": str(sum((row["fee"] for row in rows), Decimal(0))),
            "funding_included": False, "unrealized_pnl_included": False,
            "deposits_withdrawals_included": False, "history_completeness": "caller_must_verify",
            "net_pnl": "sum(closedPnl) minus all supplied USDC fill fees, including opening fees; rebates add cashflow",
            "win_rate": "positive net closing fills / all closing fills, including net breakevens; entry fees not allocated to closing lots",
            "win_rate_available": bool(n),
            "profit_factor": "positive realised fill cashflows / absolute negative fill cashflows, including opening fees; not completed-trade PF",
            "profit_factor_available": bool(gross_profit or gross_loss),
            "max_drawdown": "drawdown of cumulative realised fill cashflow grouped by millisecond, not account equity drawdown",
            "max_drawdown_pct_available": False, "capital_roi_available": False, "sharpe_available": False,
            "reference_balance_used": False, "days": "UTC days with any supplied fill, including net-zero days",
            "top_coin_share": "largest gross executed notional / total executed notional; not current portfolio exposure",
            "expectancy": "net supplied-period cashflow / closing-fill count; not complete-trade expected profit",
            "expectancy_available": bool(n),
            "rating": "descriptive heuristic only; not a probability, risk guarantee, follower backtest or admission decision",
        }
        return Report(
            trades=n, wins=len(wins), losses=len(losses), win_rate=len(wins)/n*100 if n else 0.,
            gross_profit=_float(gross_profit, "gross_profit"), gross_loss=_float(gross_loss, "gross_loss"),
            net_pnl=_float(net, "net_pnl"), profit_factor=pf,
            avg_win=_float(sum(wins, Decimal(0))/len(wins), "avg_win") if wins else 0.,
            avg_loss=_float(sum(losses, Decimal(0))/len(losses), "avg_loss") if losses else 0.,
            best=_float(max(wins, default=Decimal(0)), "best"), worst=_float(min(losses, default=Decimal(0)), "worst"),
            max_drawdown=_float(max_dd, "max_drawdown"), max_drawdown_pct=None,
            avg_roi=None, median_roi=None, expectancy=_float(expectancy, "expectancy"), sharpe=None,
            consistency_score=consistency, risk_score=None, quality_score=quality,
            rating=int(round(max(0, min(100, quality)))), positive_days=positive_days,
            active_days=active_days, top_coin_share=top_share, _metadata=metadata)
