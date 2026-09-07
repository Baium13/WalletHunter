"""Persistent rule-based PAPER trader. No exchange client or order capability.

Inputs are already-fetched public observations. This module never accesses a
wallet, private key, network, or real position. Rules are not a trained model.
All fills are virtual market-price assumptions, not executable-order promises.
"""
from contextlib import closing
from copy import deepcopy
import json
import math
import os
import re
import sqlite3

from core.order_precision import normalize_perp_size
from core.ai_entry_policy import ENTRY_MARGIN_FRACTION, MAX_ENTRY_LEVERAGE


CANDLE_MS = 900_000
DAY_MS = 86_400_000
FEE_RATE = .0005
SLIPPAGE_RATE = .0005
MAX_EVENTS = 80


def _number(value, label, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"Invalid {label}")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"Invalid {label}") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"Invalid {label}")
    return result


def _integer(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Invalid {label}")
    return value


def _identity(uid, account):
    user = str(uid).strip()
    if not user or len(user) > 128 or not isinstance(account, str) or not account.strip() or len(account) > 128:
        raise ValueError("Missing paper account identity")
    return user, account.strip().lower()


def _market(raw, now):
    if not isinstance(raw, dict):
        raise ValueError("Invalid paper market")
    coin, dex = raw.get("coin"), raw.get("dex") or ""
    if not isinstance(coin, str) or not re.fullmatch(r"(?:[a-z0-9_-]+:)?[A-Za-z0-9_.-]+", coin):
        raise ValueError("Invalid paper coin")
    if dex not in ("", "xyz"):
        raise ValueError("Unsupported paper DEX")
    if ":" in coin:
        if coin.split(":", 1)[0] != dex:
            raise ValueError("Inconsistent paper market identity")
    elif dex:
        coin = f"{dex}:{coin}"
    price = _number(raw.get("price"), "quote", minimum=1e-12)
    digits = _integer(raw.get("sz_decimals"), "size precision")
    if digits > 6:
        raise ValueError("Invalid size precision")
    maximum = _integer(raw.get("max_leverage"), "maximum leverage", 1)
    asof = _integer(raw.get("asof_ms"), "quote time", 1)
    candle = _integer(raw.get("candle_close_ms"), "candle time", 1)
    if not 0 <= now - asof <= 120_000:
        raise ValueError("Stale or future paper quote")
    if not 0 < now - candle <= 35 * 60_000 or candle % CANDLE_MS not in (0, CANDLE_MS - 1):
        raise ValueError("Need a recent closed 15m candle")
    factors = raw.get("factors")
    if not isinstance(factors, dict):
        raise ValueError("Missing paper factors")
    observed = _integer(factors.get("asof_ms"), "factor time", 1)
    if not 0 <= now - observed <= 120_000 or factors.get("candle_close_ms") != candle:
        raise ValueError("Stale or mismatched paper factors")
    trend, levels = factors.get("trend_ema20_50"), factors.get("levels20")
    if not isinstance(trend, dict) or not isinstance(levels, dict):
        raise ValueError("Incomplete paper factors")
    e20 = _number(trend.get("ema20"), "EMA20", minimum=1e-12)
    e50 = _number(trend.get("ema50"), "EMA50", minimum=1e-12)
    rsi = _number(factors.get("rsi14"), "RSI", minimum=0)
    if rsi > 100:
        raise ValueError("Invalid RSI")
    macd = _number(factors.get("macd_hist"), "MACD")
    _number(factors.get("atr14_pct"), "ATR", minimum=0)
    _number(factors.get("volume_ratio20"), "volume ratio", minimum=0)
    support = _number(levels.get("support"), "support", minimum=1e-12)
    resistance = _number(levels.get("resistance"), "resistance", minimum=1e-12)
    if support > resistance:
        raise ValueError("Invalid support/resistance")
    _number(factors.get("funding_bps_hour"), "funding")
    _number(factors.get("open_interest"), "open interest", minimum=0)
    signal = "LONG" if e20 > e50 and macd > 0 and 52 <= rsi <= 70 else (
        "SHORT" if e20 < e50 and macd < 0 and 30 <= rsi <= 48 else None)
    return {"coin": coin, "dex": dex, "key": f"{coin}|{dex}", "price": price,
            "sz_decimals": digits, "leverage": min(MAX_ENTRY_LEVERAGE, maximum), "asof_ms": asof,
            "candle_close_ms": candle, "signal": signal}


def _new_state():
    return {"version": 1, "status": "NOT_STARTED", "reason": "not_started",
            "initial_budget_usdc": None, "budget_usdc": 0.0, "funded_base_usdc": 0.0,
            "external_funding_adjustment_usdc": 0.0, "realized_pnl_usdc": 0.0,
            "positions": {}, "events": [], "seen_candles": {}, "last_tick_ms": 0,
            "day": None, "day_start_pnl_usdc": 0.0, "daily_loss_locked": False,
            "slot_loss_locked": False, "counters": {"opened": 0, "closed": 0,
                "additions": 0, "skipped": 0, "fees_usdc": 0.0, "slippage_usdc": 0.0}}


def _mark(position, market):
    direction = 1 if position["side"] == "LONG" else -1
    price, size = market["price"], position["size"]
    gross = (price - position["entry_price"]) * size * direction
    exit_price = price * (1 - direction * SLIPPAGE_RATE)
    exit_fee = size * exit_price * FEE_RATE
    liquidation_pnl = ((exit_price - position["entry_price"]) * size * direction
                       - exit_fee - position["entry_fees_usdc"])
    position.update(mark_price=price, last_mark_ms=market["asof_ms"],
                    notional_usdc=size * price, unrealized_pnl_usdc=gross,
                    estimated_exit_pnl_usdc=liquidation_pnl,
                    roe_pct=liquidation_pnl / position["margin_usdc"] * 100)


def _unrealized(state):
    return math.fsum(p["unrealized_pnl_usdc"] for p in state["positions"].values())


def _net_pnl(state):
    return state["realized_pnl_usdc"] + _unrealized(state)


def _equity(state):
    return state["funded_base_usdc"] + _net_pnl(state)


def _reserved(state):
    return math.fsum(p["margin_usdc"] for p in state["positions"].values())


def _event(state, now, action, coin="", reason="", **fields):
    state["events"].append({"action": action, "status": "PAPER", "time_ms": now,
                            "created_ms": now, "coin": coin, "reason": reason,
                            "pnl_usdc": 0.0, **fields})
    state["events"] = state["events"][-MAX_EVENTS:]


def _close(state, market, now, reason):
    position = state["positions"].pop(market["key"])
    direction = 1 if position["side"] == "LONG" else -1
    fill = market["price"] * (1 - direction * SLIPPAGE_RATE)
    notional = position["size"] * fill
    fee = notional * FEE_RATE
    gross = (fill - position["entry_price"]) * position["size"] * direction
    state["realized_pnl_usdc"] += gross - fee
    state["counters"]["closed"] += 1
    state["counters"]["fees_usdc"] += fee
    state["counters"]["slippage_usdc"] += abs(fill - market["price"]) * position["size"]
    _event(state, now, "CLOSE", position["coin"], reason,
           side=position["side"], size=position["size"], price=fill,
           pnl_usdc=gross - fee - position["entry_fees_usdc"], fee_usdc=fee,
           leverage=position["leverage"], margin_usdc=position["margin_usdc"])


def _enter(state, market, now, side, *, add=False):
    key = market["key"]
    position = state["positions"].get(key)
    base = max(0.0, min(state["funded_base_usdc"], _equity(state)))
    margin = base * ENTRY_MARGIN_FRACTION
    leverage = position["leverage"] if add else market["leverage"]
    if add and leverage > market["leverage"]:
        reason = "market_leverage_limit_changed"
    else:
        direction = 1 if side == "LONG" else -1
        fill = market["price"] * (1 + direction * SLIPPAGE_RATE)
        size = normalize_perp_size(margin * leverage / fill, market["sz_decimals"])
        notional = size * fill
        actual_margin = notional / leverage
        fee = notional * FEE_RATE
        reserved = _reserved(state)
        if size <= 0 or notional < 10:
            reason = "below_minimum_notional"
        elif reserved + actual_margin > .5 * state["funded_base_usdc"] + 1e-12:
            reason = "reserved_cap"
        elif actual_margin + fee > max(0., _equity(state) - reserved) + 1e-12:
            reason = "insufficient_virtual_capacity"
        else:
            state["realized_pnl_usdc"] -= fee
            state["counters"]["fees_usdc"] += fee
            state["counters"]["slippage_usdc"] += abs(fill - market["price"]) * size
            if add:
                combined = position["size"] + size
                position["entry_price"] = (position["entry_price"] * position["size"] + fill * size) / combined
                position["size"] = combined
                position["margin_usdc"] += actual_margin
                position["entry_fees_usdc"] += fee
                position["adds"] += 1
                state["counters"]["additions"] += 1
            else:
                position = {"coin": market["coin"], "dex": market["dex"], "side": side,
                    "size": size, "entry_price": fill, "leverage": leverage,
                    "margin_usdc": actual_margin, "entry_fees_usdc": fee,
                    "opened_ms": now, "adds": 0}
                state["positions"][key] = position
                state["counters"]["opened"] += 1
            position["last_action_candle_ms"] = market["candle_close_ms"]
            _mark(position, market)
            _event(state, now, "AVERAGE" if add else "OPEN", market["coin"], "supportive_signal" if add else "trend_macd_rsi",
                   side=side, size=size, price=fill, leverage=leverage,
                   margin_usdc=actual_margin, notional_usdc=notional, fee_usdc=fee)
            return
    state["counters"]["skipped"] += 1
    _event(state, now, "SKIP", market["coin"], reason)


def _loss_limits(state):
    base = state["funded_base_usdc"]
    if base > 0:
        pnl = _net_pnl(state)
        if pnl <= -.10 * base:
            state["slot_loss_locked"] = True
        if pnl - state["day_start_pnl_usdc"] <= -.10 * base:
            state["daily_loss_locked"] = True
    if state["slot_loss_locked"]:
        return "slot_loss_limit"
    if state["daily_loss_locked"]:
        return "daily_loss_limit"
    return None


class AiPaperTrader:
    def __init__(self, root):
        self.path = os.path.join(root, "data", "ai_trader.sqlite3")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("CREATE TABLE IF NOT EXISTS paper_accounts (user_id TEXT NOT NULL, account TEXT NOT NULL, "
                       "state TEXT NOT NULL, PRIMARY KEY(user_id,account))")
            db.commit()
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _read(db, uid, account):
        row = db.execute("SELECT state FROM paper_accounts WHERE user_id=? AND account=?", (uid, account)).fetchone()
        if row is None:
            return _new_state()
        state = json.loads(row["state"], parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Corrupt paper state")))
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("Unsupported paper state version")
        return state

    @staticmethod
    def _summary(state):
        result = {key: deepcopy(state[key]) for key in (
            "status", "reason", "budget_usdc", "initial_budget_usdc", "funded_base_usdc",
            "external_funding_adjustment_usdc", "realized_pnl_usdc", "last_tick_ms", "counters")}
        result.update(mode="PAPER", equity_usdc=_equity(state), unrealized_pnl_usdc=_unrealized(state),
                      day_pnl_usdc=_net_pnl(state) - state["day_start_pnl_usdc"],
                      reserved_margin_usdc=_reserved(state),
                      positions=[deepcopy(p) for _, p in sorted(state["positions"].items())],
                      events=list(reversed(deepcopy(state["events"]))),
                      strategy="rules-v1; closed 15m; EMA20/50 + MACD + RSI; no trained model",
                      cost_model={"fee_bps_each_side": 5, "slippage_bps_each_side": 5,
                                  "funding_included": False, "execution": "assumed_virtual_fills"},
                      policy={"initial_margin_fraction": ENTRY_MARGIN_FRACTION, "max_leverage": MAX_ENTRY_LEVERAGE,
                              "slot_loss_limit_pct": 10, "daily_loss_limit_pct": 10,
                              "stop_roe_pct": -50, "take_profit_roe_pct": 10,
                              "average_trigger_roe_pct": -20, "max_additions_per_position": 4,
                              "max_reserved_slot_fraction": .5, "max_positions": 3,
                              "minimum_notional_usdc": 10, "actions": "once_per_closed_15m_candle"})
        # Even an unexpected arithmetic overflow must never leak invalid JSON.
        json.dumps(result, allow_nan=False)
        return result

    def summary(self, uid, account):
        uid, account = _identity(uid, account)
        with closing(self._connect()) as db:
            return self._summary(self._read(db, uid, account))

    def tick(self, uid, account, budget_usdc, markets, now_ms):
        uid, account = _identity(uid, account)
        budget = _number(budget_usdc, "slot budget", minimum=0)
        now = _integer(now_ms, "tick time", 1)
        if not isinstance(markets, list) or len(markets) > 100:
            raise ValueError("Invalid paper market list")
        # Validate the whole observation before any persistent mutation.
        parsed = [_market(raw, now) for raw in markets]
        if len({market["key"] for market in parsed}) != len(parsed):
            raise ValueError("Duplicate paper market")
        parsed.sort(key=lambda market: market["key"])
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._read(db, uid, account)
            if now < state["last_tick_ms"]:
                raise ValueError("Out-of-order paper tick")
            if now == state["last_tick_ms"]:
                return self._summary(state)
            previous_pnl = _net_pnl(state)
            if state["initial_budget_usdc"] is None and budget > 0:
                state["initial_budget_usdc"] = budget
            state["budget_usdc"] = budget
            state["funded_base_usdc"] = min(budget, state["initial_budget_usdc"] or 0.0)
            state["external_funding_adjustment_usdc"] = state["funded_base_usdc"] - (state["initial_budget_usdc"] or 0.0)
            if state["day"] != now // DAY_MS:
                state["day"] = now // DAY_MS
                state["day_start_pnl_usdc"] = previous_pnl
                state["daily_loss_locked"] = False
            for market in parsed:
                if market["key"] in state["positions"]:
                    _mark(state["positions"][market["key"]], market)
            # A fresh quote for BTC does not make an omitted ETH position fresh.
            # Old marks remain visible but cannot justify any additional risk.
            observed_keys = {market["key"] for market in parsed}
            incomplete_positions = any(key not in observed_keys for key in state["positions"])
            loss = _loss_limits(state)
            for market in parsed:
                key, candle = market["key"], market["candle_close_ms"]
                if candle <= state["seen_candles"].get(key, 0):
                    continue
                state["seen_candles"][key] = candle
                position = state["positions"].get(key)
                if position:
                    reason = loss
                    if reason is None and position["roe_pct"] <= -50:
                        reason = "stop_roe"
                    if reason is None and position["roe_pct"] >= 10:
                        reason = "take_profit_roe"
                    if reason is None and market["signal"] and market["signal"] != position["side"]:
                        reason = "contrary_signal"
                    if reason:
                        _close(state, market, now, reason)
                    elif (position["roe_pct"] <= -20 and market["signal"] == position["side"]
                          and position["adds"] < 4 and state["funded_base_usdc"] > 0
                          and not incomplete_positions):
                        _enter(state, market, now, position["side"], add=True)
                elif loss is None and state["funded_base_usdc"] > 0 and market["signal"] and not incomplete_positions:
                    if len(state["positions"]) >= 3:
                        state["counters"]["skipped"] += 1
                        _event(state, now, "SKIP", market["coin"], "position_limit")
                    else:
                        _enter(state, market, now, market["signal"])
                loss = _loss_limits(state)
            state["last_tick_ms"] = now
            if loss:
                state.update(status="LOSS_LIMIT", reason=loss)
            elif state["funded_base_usdc"] <= 0:
                state.update(status="WAITING_BUDGET", reason="no_allocated_budget")
            elif not parsed or incomplete_positions:
                state.update(status="WAITING_DATA", reason="no_market_data")
            elif _reserved(state) > .5 * state["funded_base_usdc"]:
                state.update(status="BUDGET_PAUSED", reason="reserved_exceeds_budget")
            else:
                state.update(status="ACTIVE", reason="rules_paper_only")
            result = self._summary(state)
            payload = json.dumps(state, allow_nan=False, separators=(",", ":"))
            db.execute("INSERT INTO paper_accounts(user_id,account,state) VALUES(?,?,?) "
                       "ON CONFLICT(user_id,account) DO UPDATE SET state=excluded.state", (uid, account, payload))
            db.commit()
            return result
