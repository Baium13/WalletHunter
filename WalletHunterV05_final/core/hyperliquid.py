import time
import requests
from core.fill_history import fetch_fills, filter_perp_fills
from core.capital_snapshot import read_capital_snapshot, strict_spot_usdc


class HyperliquidReader:
    def __init__(self, mode="MAINNET"):
        self.base_url = (
            "https://api.hyperliquid.xyz/info"
            if mode != "TESTNET"
            else "https://api.hyperliquid-testnet.xyz/info"
        )
        self.s = requests.Session()
        self._leverage_cache = {}

    def _info(self, payload):
        r = self.s.post(self.base_url, json=payload, timeout=20)
        r.raise_for_status()
        return r.json()

    def state(self, wallet, dex=""):
        return self._info({
            "type": "clearinghouseState",
            "user": wallet,
            "dex": dex
        })

    def spot_state(self, wallet):
        return self._info({
            "type": "spotClearinghouseState",
            "user": wallet
        })

    def spot_usdc_balance(self, wallet):
        """Unreserved USDC, explicitly separate from the sizing balance."""
        state = self.spot_state(wallet)
        return strict_spot_usdc(state).available

    @staticmethod
    def _positions(state, market_type, dex="", leverage_limits=None):
        out = []
        if not isinstance(state, dict) or not isinstance(state.get("assetPositions"), list):
            raise ValueError("Invalid or incomplete position snapshot")

        for item in state.get("assetPositions", []):
            p = item.get("position", item)
            s = float(p.get("szi", 0) or 0)

            if abs(s) < 1e-15:
                continue

            lev = p.get("leverage")
            lev = lev.get("value") if isinstance(lev, dict) else lev

            try:
                lev = float(lev or 1)
            except Exception:
                lev = 1.0

            position_value = abs(float(p.get("positionValue", 0) or 0))
            # marginUsed is the actual collateral reserved by Hyperliquid.
            # Older/API variants may omit it, so use notional / leverage only
            # as a transparent fallback rather than showing the leveraged sum.
            margin_used = abs(float(p.get("marginUsed", 0) or 0))
            if margin_used <= 0:
                margin_used = position_value / lev if lev > 0 else position_value
            out.append({
                "coin": str(p.get("coin") or ""),
                "size": abs(s),
                "signed_size": s,
                "side": "LONG" if s > 0 else "SHORT",
                "entry_price": float(p.get("entryPx", 0) or 0),
                "position_value": position_value,
                "margin_used": margin_used,
                "unrealized_pnl": float(p.get("unrealizedPnl", 0) or 0),
                "leverage": lev,
                "market_max_leverage": float((leverage_limits or {}).get(str(p.get("coin") or "").split(":")[-1], 1) or 1),
                "roe": float(p.get("returnOnEquity", 0) or 0) * 100,
                "liquidation_price": float(p.get("liquidationPx", 0) or 0),
                "market_type": market_type,
                "dex": dex or None,
            })

        return out

    def positions(self, wallet, crypto=True, stocks=True):
        out = []

        if crypto:
            try:
                out += self._positions(
                    self.state(wallet, ""),
                    "CRYPTO",
                    "",
                    self.leverage_limits(""),
                )
            except Exception as e:
                raise RuntimeError("Cannot read leader crypto positions") from e

        if stocks:
            try:
                out += self._positions(
                    self.state(wallet, "xyz"),
                    "STOCKS",
                    "xyz",
                    self.leverage_limits("xyz"),
                )
            except Exception as e:
                raise RuntimeError("Cannot read leader xyz positions") from e

        return out

    def leverage_limits(self, dex=""):
        """Live per-market leverage ceilings advertised by Hyperliquid."""
        cache_key = dex or "crypto"
        cached = self._leverage_cache.get(cache_key)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        meta = self._info({"type": "meta", **({"dex": dex} if dex else {})})
        limits = {}
        for asset in meta.get("universe", []):
            name = str(asset.get("name") or "").split(":")[-1]
            try:
                limits[name] = max(1, int(asset.get("maxLeverage") or 1))
            except (TypeError, ValueError):
                continue
        self._leverage_cache[cache_key] = (time.time(), limits)
        return limits

    def leverage_choices(self):
        """Distinct current maximum leverage values, for the Telegram picker."""
        values = set(self.leverage_limits("").values())
        try:
            values.update(self.leverage_limits("xyz").values())
        except Exception as exc:
            print("[HL] xyz leverage metadata:", exc)
        return sorted(values)

    def account_value(self, wallet, dex=""):
        m = self.state(wallet, dex).get("marginSummary") or {}

        return float(
            m.get("accountValue")
            or m.get("totalRawUsd")
            or 0
        )

    def balance(self, wallet):
        """Mode-aware USDC sizing base, not full portfolio/historical equity."""
        return self.capital_snapshot(wallet).sizing_base_usdc

    def capital_snapshot(self, wallet):
        return read_capital_snapshot(self._info, wallet)

    def mids(self, dex=""):
        return self._info({
            "type": "allMids",
            "dex": dex
        })

    def market_context(self, coin, dex=""):
        """Current funding and mark data used only as an entry risk filter."""
        result = self._info({"type": "metaAndAssetCtxs", "dex": dex})
        if not isinstance(result, list) or len(result) < 2:
            return {}
        universe = (result[0] or {}).get("universe", [])
        contexts = result[1] or []
        raw = str(coin).split(":")[-1]
        for asset, context in zip(universe, contexts):
            if str(asset.get("name", "")).split(":")[-1] == raw:
                try: funding = float(context.get("funding", 0) or 0) * 10_000
                except (TypeError, ValueError): funding = 0.0
                return {"funding_bps_hour": funding, "mark_price": float(context.get("markPx", 0) or 0),
                        "open_interest": float(context["openInterest"]) if context.get("openInterest") is not None else None}
        return {}

    def mid(self, coin, dex=""):
        m = self.mids(dex)

        keys = [coin]

        if dex:
            keys += [
                f"{dex}:{coin}",
                coin.split(":")[-1]
            ]

        for k in keys:
            if k in m:
                return float(m[k])

        raise ValueError(f"Market not found: {coin}/{dex}")

    def fills_90d(self, wallet, dex=None):
        """Requested 90-day perp window, or HistoryIncomplete on truncation.

        None includes all perpetual DEXes; explicit '' selects core perps.
        The API already returns every market, so filtering is local only.
        """
        end = int(time.time() * 1000)
        start = max(0, end - 90 * 24 * 3600 * 1000)
        return filter_perp_fills(fetch_fills(self._info, wallet, start, end), dex)
