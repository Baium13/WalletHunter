import time
import requests
from core.fill_history import fetch_fills, filter_perp_fills
from core.capital_snapshot import read_capital_snapshot, available_margin_usdc, strict_spot_usdc, finite_amount
from core.foundation.contracts import InstrumentId, OpenOrder, PortfolioSnapshot, Position


class HyperliquidReader:
    def __init__(self, mode="MAINNET"):
        from core.settings import validated_network
        self.network = mode = validated_network(mode)
        self.base_url = (
            "https://api.hyperliquid.xyz/info"
            if mode != "TESTNET"
            else "https://api.hyperliquid-testnet.xyz/info"
        )
        from core.hl_budget import BudgetSession
        self.s = BudgetSession()
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
            if not isinstance(item, dict):
                raise ValueError("Malformed position row")
            p = item.get("position", item)
            if not isinstance(p, dict) or not isinstance(p.get("coin"), str) or not p["coin"]:
                raise ValueError("Missing position identity")
            s = finite_amount(p.get("szi"), "position size")

            if s == 0:
                continue

            lev = p.get("leverage")
            lev = lev.get("value") if isinstance(lev, dict) else lev

            lev = finite_amount(lev, "position leverage")
            if lev < 1:
                raise ValueError("Invalid position leverage")

            position_value = finite_amount(p.get("positionValue"), "position value")
            entry = finite_amount(p.get("entryPx"), "entry price")
            if position_value <= 0 or entry <= 0:
                raise ValueError("Invalid open position value or entry")
            # marginUsed is the actual collateral reserved by Hyperliquid.
            # Older/API variants may omit it, so use notional / leverage only
            # as a transparent fallback rather than showing the leveraged sum.
            margin_used = finite_amount(p["marginUsed"], "margin used") if "marginUsed" in p else position_value / lev
            if margin_used < 0:
                raise ValueError("Negative position margin")
            out.append({
                "coin": str(p.get("coin") or ""),
                "size": abs(s),
                "signed_size": s,
                "side": "LONG" if s > 0 else "SHORT",
                "entry_price": entry,
                "position_value": position_value,
                "margin_used": margin_used,
                "unrealized_pnl": finite_amount(p.get("unrealizedPnl"), "unrealized PnL"),
                "leverage": lev,
                "market_max_leverage": float((leverage_limits or {}).get(str(p.get("coin") or "").split(":")[-1], 1) or 1),
                "roe": finite_amount(p.get("returnOnEquity"), "ROE") * 100,
                "liquidation_price": finite_amount(p["liquidationPx"], "liquidation price") if p.get("liquidationPx") is not None else None,
                "margin_mode": p["leverage"].get("type") if isinstance(p.get("leverage"), dict) else None,
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

    def frontend_open_orders(self, wallet, dex=""):
        """Read-only open-order evidence for a public account snapshot."""
        if dex not in ("", "xyz"):
            raise ValueError("Unsupported perpetual DEX")
        result = self._info({"type": "frontendOpenOrders", "user": wallet, "dex": dex})
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise ValueError("Invalid open-order snapshot")
        return result

    def account_snapshot(self, scope, revision=1, clock_ms=None):
        """Compose a fresh, credential-free account evidence snapshot.

        This uses the same bounded public ``info`` transport as the existing
        reader.  It intentionally does not infer ownership/provenance and does
        not create a signing client; callers must link any ownership from the
        journal separately.
        """
        from core.foundation.data import reader_account_snapshot
        return reader_account_snapshot(self, scope, revision, clock_ms or (lambda: int(time.time() * 1000)))

    def _market_meta(self, dex=""):
        """One cached ``meta`` read behind both leverage ceilings and lot steps.

        Lot size is per market: BTC quotes to 1e-5 while a cheap perp quotes to
        whole units. Sizing an order with one global step produces a quantity
        the venue cannot fill, so the advertised szDecimals travel with the
        leverage ceilings rather than costing a second metadata request.
        """
        cache_key = dex or "crypto"
        cached = self._leverage_cache.get(cache_key)
        if cached and time.time() - cached[0] < 300:
            return cached[1], cached[2]
        meta = self._info({"type": "meta", **({"dex": dex} if dex else {})})
        limits, steps = {}, {}
        for asset in meta.get("universe", []):
            name = str(asset.get("name") or "").split(":")[-1]
            try:
                limits[name] = max(1, int(asset.get("maxLeverage") or 1))
            except (TypeError, ValueError):
                pass
            try:
                decimals = int(asset["szDecimals"])
                if 0 <= decimals <= 6:
                    steps[name] = 10.0 ** -decimals
            except (KeyError, TypeError, ValueError):
                pass
        self._leverage_cache[cache_key] = (time.time(), limits, steps)
        return limits, steps

    def leverage_limits(self, dex=""):
        """Live per-market leverage ceilings advertised by Hyperliquid."""
        return self._market_meta(dex)[0]

    def size_steps(self, dex=""):
        """Live per-market lot steps (10**-szDecimals) advertised by Hyperliquid."""
        return self._market_meta(dex)[1]

    def market_size_step(self, coin, dex=""):
        """Lot step for one market, or None when the venue advertised none."""
        return self.size_steps(dex).get(str(coin).split(":")[-1])

    def leverage_choices(self):
        """Distinct current maximum leverage values, for the Telegram picker."""
        values = set(self.leverage_limits("").values())
        try:
            values.update(self.leverage_limits("xyz").values())
        except Exception as exc:
            print("[HL] xyz leverage metadata:", type(exc).__name__)
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
        if not isinstance(result, list) or len(result) != 2 or not isinstance(result[0], dict) or not isinstance(result[1], list):
            raise ValueError("Unavailable market context")
        universe = (result[0] or {}).get("universe", [])
        contexts = result[1] or []
        if not universe or len(universe) != len(contexts):
            raise ValueError("Incomplete market context")
        raw = str(coin).split(":")[-1]
        for asset, context in zip(universe, contexts):
            if str(asset.get("name", "")).split(":")[-1] == raw:
                funding = finite_amount(context.get("funding"), "funding") * 10_000
                mark = finite_amount(context.get("markPx"), "mark price")
                interest = finite_amount(context.get("openInterest"), "open interest")
                if mark <= 0 or interest < 0:
                    raise ValueError("Invalid market context")
                return {"funding_bps_hour": finite_amount(funding, "funding bps"), "mark_price": mark,
                        "open_interest": interest, "observed_monotonic": time.monotonic()}
        raise ValueError("Market context missing requested instrument")

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
