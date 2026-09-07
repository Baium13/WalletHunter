"""Independent AI modes; trader observes public data and trades PAPER only.

No signing client, API key, exchange order method or automatic mode promotion
is used here. Rules and accumulated paper results are not a calibrated model.
"""
import json
import math
import os
import re
import sqlite3
import time
from contextlib import closing

from core.ai_review import analyse
from core.ai_entry_policy import ENTRY_MARGIN_FRACTION, MAX_ENTRY_LEVERAGE
from core.ai_rescue_policy import RESCUE_TRIGGER_ROE_PCT


INTERVAL_MS = 900000
UNIVERSE = ("BTC", "ETH")
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")


def finite(value, name, minimum=0., positive=False):
    if isinstance(value, bool): raise ValueError(f"invalid_{name}")
    result = float(value)
    if not math.isfinite(result) or result < minimum or (positive and result <= 0):
        raise ValueError(f"invalid_{name}")
    return result


def account_address(profile):
    account = profile.get("account")
    address = account.get("address") if isinstance(account, dict) else None
    return address.lower() if isinstance(address, str) and ADDRESS.fullmatch(address) else None


class AiModes:
    def __init__(self, root, paper=None):
        if paper is None:
            from core.ai_paper_trader import AiPaperTrader
            paper = AiPaperTrader(root)
        self.paper = paper
        self._candles = {}
        self.path = os.path.join(os.path.abspath(root), "data", "ai_modes.sqlite3")
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        if os.path.islink(self.path): raise ValueError("AI mode state cannot be a symlink")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        os.chmod(self.path, 0o600)
        with closing(self._connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS observer_status (
                user_id TEXT NOT NULL, account TEXT NOT NULL, updated_ms INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(user_id, account))""")
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _remember(self, uid, account, status):
        status = dict(status)
        status.setdefault("asof_ms", int(time.time() * 1000))
        payload = json.dumps(status, ensure_ascii=False, allow_nan=False)
        with closing(self._connect()) as db:
            db.execute("""INSERT INTO observer_status VALUES(?,?,?,?)
                ON CONFLICT(user_id,account) DO UPDATE SET updated_ms=excluded.updated_ms,payload=excluded.payload
                WHERE excluded.updated_ms >= observer_status.updated_ms""",
                (str(uid), account or "", status["asof_ms"], payload))
            db.commit()
        return status

    def _last_tick(self, uid, account):
        with closing(self._connect()) as db:
            row = db.execute("SELECT payload FROM observer_status WHERE user_id=? AND account=?",
                             (str(uid), account or "")).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _trader_block(profile, include_disabled=True):
        if include_disabled and not profile.get("ai_trader_enabled", False): return "disabled"
        if not account_address(profile): return "account_missing"
        if not profile.get("ai_slot_selected", False): return "ai_slot_unavailable"
        leaders = profile.get("leaders", [])
        if not isinstance(leaders, list) or len(leaders) > 2: return "too_many_wallets"
        return None

    @staticmethod
    def set_enabled(profile, mode, enabled):
        """Validate and mutate only one flag on the caller's storage snapshot."""
        if mode not in {"trader", "rescue"}: raise ValueError("unknown_ai_mode")
        if not isinstance(enabled, bool): raise ValueError("enabled_must_be_boolean")
        if mode == "trader" and enabled:
            reason = AiModes._trader_block(profile, include_disabled=False)
            if reason: raise ValueError(reason)
        profile["ai_trader_enabled" if mode == "trader" else "ai_review_enabled"] = enabled
        return profile

    def summary(self, uid, profile):
        """Local state only. Never performs a market/network request."""
        address = account_address(profile)
        paper = self.paper.summary(uid, address) if address else {
            "mode": "PAPER", "status": "NOT_STARTED", "reason": "account_missing",
            "positions": [], "events": []}
        return {
            "trader": {
                "enabled": bool(profile.get("ai_trader_enabled", False)),
                "execution_mode": "PAPER", "real_execution_available": False,
                "slot_selected": bool(profile.get("ai_slot_selected", False)),
                "blocked_reason": self._trader_block(profile),
                "limits": {"entry_pct": ENTRY_MARGIN_FRACTION * 100, "max_leverage": MAX_ENTRY_LEVERAGE, "max_loss_pct": 10},
                "universe": list(UNIVERSE),
                "universe_reason": "initial_crypto_paper_universe_xyz_cost_model_unverified",
                "model": "rules_only_not_calibrated_probability",
                "last_tick": self._last_tick(uid, address),
                "paper": paper,
            },
            "rescue": {
                "enabled": bool(profile.get("ai_review_enabled", True)),
                "execution_mode": "CONFIRMATION_REQUIRED",
                "real_execution_available": False, "trigger_roe_pct": RESCUE_TRIGGER_ROE_PCT,
                "automatic_execution": False,
                "user_confirmation_available": bool(profile.get("account")) and bool(profile.get("ai_review_enabled", True)),
                "max_extra_slot_fraction": .5, "max_additions": 4,
            },
        }

    @staticmethod
    def _metadata(reader):
        result = reader._info({"type": "metaAndAssetCtxs", "dex": ""})
        if not isinstance(result, list) or len(result) != 2 or not isinstance(result[0], dict):
            raise ValueError("invalid_market_metadata")
        assets, contexts = result[0].get("universe"), result[1]
        if not isinstance(assets, list) or not isinstance(contexts, list) or len(assets) != len(contexts):
            raise ValueError("invalid_market_contexts")
        rows = {}
        for asset, context in zip(assets, contexts):
            if not isinstance(asset, dict): raise ValueError("invalid_asset_metadata")
            coin = asset.get("name")
            if coin not in UNIVERSE: continue
            if coin in rows: raise ValueError("duplicate_market_metadata")
            rows[coin] = (asset, context)
        return rows

    def _closed_candles(self, reader, coin, now_ms):
        last_open = (now_ms // INTERVAL_MS - 1) * INTERVAL_MS
        cached = self._candles.get(coin)
        if cached and cached[0] == last_open: return cached[1]
        response = reader._info({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "15m", "startTime": now_ms-120*INTERVAL_MS, "endTime": now_ms}})
        if not isinstance(response, list): raise ValueError("invalid_candle_response")
        rows = []
        for row in response:
            if not isinstance(row, dict): raise ValueError("invalid_candle")
            opened, closed = finite(row["t"], "candle_open"), finite(row["T"], "candle_close")
            if opened != int(opened) or closed != int(closed) or opened % INTERVAL_MS:
                raise ValueError("invalid_candle_timestamp")
            if closed >= now_ms: continue
            if closed-opened not in {INTERVAL_MS-1, INTERVAL_MS}: raise ValueError("invalid_candle_duration")
            low, high, close = (finite(row[k], f"candle_{k}", positive=True) for k in ("l", "h", "c"))
            if not low <= close <= high: raise ValueError("invalid_candle_ohlc")
            finite(row["v"], "candle_volume")
            rows.append(dict(row))
        rows.sort(key=lambda row: int(row["t"]))
        if len(rows) < 60 or int(rows[-1]["t"]) != last_open:
            raise ValueError("latest_closed_candle_unavailable")
        if any(int(b["t"])-int(a["t"]) != INTERVAL_MS for a, b in zip(rows, rows[1:])):
            raise ValueError("candle_history_has_gaps")
        self._candles[coin] = (last_open, rows)
        return rows

    def tick(self, uid, profile, public_reader):
        """Public snapshots -> virtual trader. Never opens a real position."""
        address = account_address(profile)
        reason = self._trader_block(profile)
        if reason:
            status = {"status": "PAUSED", "reason": reason, "execution_mode": "PAPER"}
            return self._remember(uid, address, status)
        try:
            balance = finite(public_reader.balance(address), "sizing_balance")
            budget = balance / 3.
            metadata = self._metadata(public_reader)
            mids = public_reader.mids("")
            if not isinstance(mids, dict): raise ValueError("invalid_quotes")
            quote_asof = int(time.time() * 1000)
            markets, errors = [], []
            for coin in UNIVERSE:
                try:
                    asset, context = metadata[coin]
                    if not isinstance(context, dict): raise ValueError("invalid_market_context")
                    decimals = finite(asset["szDecimals"], "size_decimals")
                    max_leverage = finite(asset["maxLeverage"], "max_leverage", positive=True)
                    if decimals != int(decimals) or decimals > 6 or max_leverage != int(max_leverage):
                        raise ValueError("invalid_market_precision_or_leverage")
                    configured = profile.get("max_leverage")
                    if configured is not None:
                        user_limit = finite(configured, "profile_leverage", positive=True)
                        if user_limit != int(user_limit):raise ValueError("invalid_profile_leverage")
                        max_leverage = min(max_leverage, user_limit)
                    price = finite(mids[coin], "quote", positive=True)
                    context = {
                        "funding_bps_hour": finite(context["funding"], "funding", minimum=-math.inf) * 10000,
                        "open_interest": finite(context["openInterest"], "open_interest"),
                    }
                    candles = self._closed_candles(public_reader, coin, quote_asof)
                    factors = analyse(candles, context, quote_asof)
                    markets.append({"coin": coin, "dex": "", "price": price,
                        "sz_decimals": int(decimals), "max_leverage": int(max_leverage),
                        "asof_ms": quote_asof, "candle_close_ms": factors["candle_close_ms"], "factors": factors})
                except Exception as exc:
                    errors.append({"coin": coin, "reason": str(exc) or type(exc).__name__})
            now_ms = int(time.time() * 1000)
            if now_ms-quote_asof > 120000: raise ValueError("quote_expired_during_market_read")
            result = self.paper.tick(uid, address, budget, markets, now_ms)
            status = {"status": "OK" if len(markets) == len(UNIVERSE) else "PARTIAL_DATA" if markets else "UNAVAILABLE",
                      "reason": None if not errors else "market_data_unavailable", "errors": errors,
                      "execution_mode": "PAPER", "asof_ms": now_ms, "markets": [m["coin"] for m in markets]}
            return dict(self._remember(uid, address, status), paper=result)
        except Exception as exc:
            status = {"status": "UNAVAILABLE", "reason": "public_data_unavailable",
                      "error": str(exc) or type(exc).__name__, "execution_mode": "PAPER"}
            return self._remember(uid, address, status)
