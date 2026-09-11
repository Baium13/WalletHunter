from dataclasses import dataclass
from contextlib import contextmanager
import math
import re
import time
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid
from core.order_precision import normalize_perp_price, normalize_perp_size
from core.fill_history import fetch_fills, filter_perp_fills
from core.capital_snapshot import read_capital_snapshot, available_margin_usdc, strict_spot_usdc

@dataclass
class Snapshot:
    account_value:float; withdrawable:float

def verify_account_control(address, private_key, info):
    """Derive the signer locally; verify delegated authority without a trade.

    Vault/subaccount routing is not supported by this account-binding contract.
    Do not confuse an API signer with the account whose collateral is traded.
    """
    try:
        if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            raise ValueError()
        signer = Account.from_key(private_key).address.lower()
    except Exception:
        raise ValueError("Invalid signing credential or account address") from None
    target = address.lower()
    if signer == target:
        return {"signer_address": signer, "trading_address": target, "account_type": "DIRECT"}
    try:
        role = info({"type": "userRole", "user": signer})
        if (isinstance(role, dict) and role.get("role") == "agent" and
                str((role.get("data") or {}).get("user", "")).lower() == target):
            return {"signer_address": signer, "trading_address": target, "account_type": "API_AGENT"}
    except Exception:
        raise ValueError("Account authority verification unavailable") from None
    raise ValueError("Signer does not control this account; vault/subaccount routing is unsupported")

class HyperliquidAccount:
    def __init__(self, address, private_key, mode="MAINNET", slippage_pct=0.5):
        from core.hl_budget import install_sdk
        install_sdk()
        from core.settings import validated_network
        self.network = mode = validated_network(mode)
        self.close_slippage_pct = slippage_pct
        self.address=address.strip(); self.base=constants.TESTNET_API_URL if mode=="TESTNET" else constants.MAINNET_API_URL
        self.info=Info(self.base,skip_ws=True,timeout=20)
        self.exchange=None
        self._xyz_sz_decimals={}
        if private_key:
            wallet=Account.from_key(private_key)
            self.exchange=Exchange(
                wallet,
                self.base,
                account_address=self.address,
                # Load both the original crypto perp DEX and XYZ metadata.
                # Without the empty DEX, SDK name_to_asset("BTC") raises
                # KeyError before update_leverage can submit a crypto order.
                perp_dexs=["", "xyz"], timeout=20
            )
    def snapshot(self,dex=""):
        state=self.info.user_state(self.address,dex=dex); m=state.get("marginSummary") or {}
        return Snapshot(float(m.get("accountValue") or m.get("totalRawUsd") or 0),float(state.get("withdrawable") or 0))
    def spot_usdc_balance(self):
        state = self.info.post("/info", {
            "type": "spotClearinghouseState",
            "user": self.address
        })

        return strict_spot_usdc(state).available

    def balance(self):
        """USDC sizing base, not free funds or full historical portfolio equity."""
        return self.capital_snapshot().sizing_base_usdc

    def capital_snapshot(self):
        # The network namespaces the account-mode cache; balances stay fresh.
        return read_capital_snapshot(lambda payload: self.info.post("/info", payload), self.address,
                                     venue=self.network)

    def available_margin(self, dex=""):
        """Fresh new-order capacity in the requested market's collateral pool."""
        return available_margin_usdc(lambda payload: self.info.post("/info", payload), self.address, dex,
                                     venue=self.network)

    def meta(self, dex=""):
        """Public market metadata; does not require an API signing key."""
        if dex not in ("", "xyz"):
            raise ValueError("Unsupported perpetual DEX")
        result = self.info.meta(dex=dex)
        if not isinstance(result, dict) or not isinstance(result.get("universe"), list):
            raise ValueError("Invalid perpetual metadata")
        return result

    def frontend_open_orders(self, dex=""):
        """Fresh public open orders, including trigger orders, for this account."""
        if dex not in ("", "xyz"):
            raise ValueError("Unsupported perpetual DEX")
        result = self.info.frontend_open_orders(self.address, dex=dex)
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise ValueError("Invalid open-order snapshot")
        return result

    @staticmethod
    def _user_cloid(cloid):
        if not isinstance(cloid, str) or re.fullmatch(r"0x[0-9a-fA-F]{32}", cloid) is None:
            raise ValueError("Client order ID must contain exactly 16 hexadecimal bytes")
        return Cloid.from_str(cloid)

    def query_order_by_cloid(self, cloid):
        """Read-only reconciliation; callers must not infer permission to retry."""
        result = self.info.query_order_by_cloid(self.address, self._user_cloid(cloid))
        if not isinstance(result, dict):
            raise ValueError("Invalid client order status")
        return result

    def submit_user_ioc(self, coin, is_buy, size, limit_price, leverage, cloid, *, expires_ms=None):
        """Send ONE exact user-confirmed BTC/ETH CROSS IOC, without retries.

        This primitive is not an authorization boundary: its caller must enforce
        the user/account-bound confirmation, expiry, budget and empty-instrument
        checks under the account lock. It neither rounds nor reprices an order.
        A leverage update is a separate exchange mutation. Any later exception
        may therefore leave leverage changed and must not trigger an auto-retry.
        The raw order response is returned, not a claim of full execution.
        """
        if expires_ms is not None and (type(expires_ms) is not int or expires_ms <= 0):
            raise ValueError("Confirmed order expiry must be a positive integer timestamp")
        previous_expiry = getattr(self.exchange, "expires_after", None)
        deadline = expires_ms
        if deadline is not None and previous_expiry is not None:
            if type(previous_expiry) is not int or previous_expiry <= 0:
                raise ValueError("Invalid existing exchange expiry")
            deadline = min(deadline, previous_expiry)

        def require_unexpired():
            if deadline is not None and int(time.time() * 1000) >= deadline:
                raise TimeoutError("Confirmed order expired; no further exchange action allowed")

        require_unexpired()
        if self.exchange is None:
            raise RuntimeError("User-confirmed order requires a signing client")
        if coin not in ("BTC", "ETH") or type(is_buy) is not bool:
            raise ValueError("User-confirmed IOC supports BTC/ETH and an explicit side only")
        if type(leverage) is not int or not 1 <= leverage <= 40:
            raise ValueError("User-confirmed leverage must be an integer from 1 to 40")
        client_id = self._user_cloid(cloid)
        for value in (size, limit_price):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Order size and limit must be numeric")
            try:
                converted = float(value)
            except (OverflowError, ValueError) as exc:
                raise ValueError("Order numeric value cannot be represented exactly") from exc
            if not math.isfinite(converted) or converted <= 0 or converted != value:
                raise ValueError("Order size and limit must be finite and positive")
        size, limit_price = float(size), float(limit_price)
        if not math.isfinite(size * limit_price):
            raise ValueError("Order notional exceeds supported numeric range")
        assets = [row for row in self.meta("")["universe"]
                  if isinstance(row, dict) and row.get("name") == coin]
        if len(assets) != 1:
            raise ValueError("Missing or ambiguous confirmed market metadata")
        asset = assets[0]
        digits, maximum = asset.get("szDecimals"), asset.get("maxLeverage")
        if type(digits) is not int or not 0 <= digits <= 6:
            raise ValueError("Invalid confirmed market size precision")
        if type(maximum) is not int or maximum < leverage:
            raise ValueError("Confirmed leverage exceeds verified market limit")
        if (asset.get("isDelisted") or asset.get("onlyIsolated") or
                asset.get("marginMode") in ("noCross", "strictIsolated")):
            raise ValueError("Confirmed CROSS market is unavailable")
        if normalize_perp_size(size, digits) != size:
            raise ValueError("Confirmed size does not match the market lot size")
        if normalize_perp_price(limit_price, digits) != limit_price:
            raise ValueError("Confirmed limit does not match the market tick size")

        require_unexpired()
        set_expiry = getattr(self.exchange, "set_expires_after", None)
        expiry_is_set = False
        try:
            if deadline is not None and callable(set_expiry):
                # Signed expiresAfter bounds arrival at the exchange as well as
                # the local checks. Restore shared SDK state even on a timeout.
                set_expiry(deadline)
                expiry_is_set = True
            require_unexpired()
            # Do not use set_leverage(): that legacy adapter rounds and may fall
            # back to another margin mode. The confirmation here is CROSS.
            ack = self.exchange.update_leverage(leverage, coin, is_cross=True)
            if (not isinstance(ack, dict) or ack.get("status") != "ok" or
                    not isinstance(ack.get("response"), dict) or
                    ack["response"].get("type") != "default" or self.response_error(ack)):
                raise RuntimeError("Confirmed leverage update was not acknowledged")
            # activeAssetData verifies configured leverage on flat instruments.
            configured = self.info.post("/info", {
                "type": "activeAssetData", "user": self.address, "coin": coin,
            })
            configured_lev = configured.get("leverage") if isinstance(configured, dict) else None
            if (not isinstance(configured, dict) or configured.get("coin") != coin or
                    str(configured.get("user") or "").lower() != self.address.lower() or
                    not isinstance(configured_lev, dict) or configured_lev.get("type") != "cross" or
                    type(configured_lev.get("value")) is not int or configured_lev["value"] != leverage):
                raise RuntimeError("Confirmed leverage could not be verified; no IOC was sent")
            require_unexpired()
            return self.exchange.order(
                coin, is_buy, size, limit_price, {"limit": {"tif": "Ioc"}},
                reduce_only=False, cloid=client_id,
            )
        finally:
            if expiry_is_set:
                set_expiry(previous_expiry)

    @staticmethod
    def _position_number(value, name):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Invalid confirmed position {name}")
        try:
            converted = float(value)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid confirmed position {name}") from exc
        if not math.isfinite(converted) or converted <= 0 or converted != value:
            raise ValueError(f"Invalid confirmed position {name}")
        return converted

    @staticmethod
    def _position_coin(coin, dex):
        if dex not in ("", "xyz") or not isinstance(coin, str) or coin != coin.strip():
            raise ValueError("Unsupported confirmed position market")
        if not dex:
            if coin not in ("BTC", "ETH"):
                raise ValueError("Only BTC/ETH or verified XYZ positions are supported")
            return coin
        if ":" in coin and not coin.startswith("xyz:"):
            raise ValueError("Confirmed position DEX mismatch")
        raw = coin.removeprefix("xyz:")
        if not raw or re.fullmatch(r"[A-Za-z0-9._-]+", raw) is None:
            raise ValueError("Invalid confirmed XYZ market")
        return f"xyz:{raw}"

    @contextmanager
    def _position_expiry(self, expires_ms):
        """Mandatory local and signed deadline for an individual confirmation."""
        if self.exchange is None:
            raise RuntimeError("Confirmed position action requires a signing client")
        if type(expires_ms) is not int or expires_ms <= 0:
            raise ValueError("Confirmed position expiry must be a positive integer timestamp")
        previous = getattr(self.exchange, "expires_after", None)
        if previous is not None and (type(previous) is not int or previous <= 0):
            raise ValueError("Invalid existing exchange expiry")
        deadline = min(expires_ms, previous) if previous is not None else expires_ms

        def require_unexpired():
            if int(time.time() * 1000) >= deadline:
                raise TimeoutError("Confirmed position action expired")

        require_unexpired()
        setter = getattr(self.exchange, "set_expires_after", None)
        applied = False
        try:
            if callable(setter):
                setter(deadline)
                applied = True
            yield require_unexpired
        finally:
            if applied:
                setter(previous)

    def _matching_confirmed_position(self, coin, dex, expected):
        if not isinstance(expected, dict):
            raise ValueError("An exact confirmed position snapshot is required")
        if (self._position_coin(expected.get("coin"), expected.get("dex") or "") != coin or
                (expected.get("dex") or "") != dex):
            raise ValueError("Confirmed position identity does not match the market")
        if expected.get("side") not in ("LONG", "SHORT") or expected.get("margin_mode") not in ("cross", "isolated"):
            raise ValueError("Confirmed position side or margin mode is unavailable")
        for field in ("size", "entry_price", "leverage"):
            self._position_number(expected.get(field), field)
        if not float(expected["leverage"]).is_integer():
            raise ValueError("Confirmed position leverage is invalid")
        # The per-DEX snapshot is fresh and contains all positions on that DEX.
        rows = self.positions(not bool(dex), bool(dex))
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("coin"), str) for row in rows):
            raise ValueError("Invalid fresh position snapshot")
        matching = [row for row in rows if (row.get("dex") or "") == dex and
                    (f"{dex}:{row['coin']}" if dex and ":" not in row["coin"] else row["coin"]) == coin]
        if len(matching) != 1:
            raise ValueError("Confirmed position is missing or ambiguous")
        position = matching[0]
        for field in ("size", "entry_price", "leverage"):
            value = self._position_number(position.get(field), field)
            if value != expected[field]:
                raise ValueError("Position changed after confirmation")
        if (position.get("side") != expected["side"] or position.get("margin_mode") != expected["margin_mode"] or
                (position.get("dex") or "") != dex):
            raise ValueError("Position changed after confirmation")
        return position

    def submit_position_ioc(self, coin, is_buy, size, limit_price, reduce_only, cloid, dex="", *, expires_ms, expected_position):
        """One confirmed REDUCE/AVERAGE IOC; never changes leverage or margin.

        Account authorization, source allocation, ROE, one-shot intent and
        post-fill reconciliation belong to the calling confirmation service.
        All same-market resting/trigger orders block this primitive; it never
        cancels them. An external manual action after the last public snapshot
        cannot be atomically excluded by Hyperliquid's non-reduce-only IOC API.
        Any uncertain response must stay UNKNOWN, never be automatically retried.
        """
        coin = self._position_coin(coin, dex)
        if type(is_buy) is not bool or type(reduce_only) is not bool:
            raise ValueError("Explicit order side and reduce-only flag are required")
        size = self._position_number(size, "order size")
        limit_price = self._position_number(limit_price, "limit price")
        if not math.isfinite(size * limit_price):
            raise ValueError("Confirmed position notional exceeds supported numeric range")
        client_id = self._user_cloid(cloid)
        with self._position_expiry(expires_ms) as require_unexpired:
            assets = [row for row in self.meta(dex)["universe"]
                      if isinstance(row, dict) and row.get("name") == coin]
            if len(assets) != 1:
                raise ValueError("Missing or ambiguous confirmed position metadata")
            asset = assets[0]
            digits, maximum = asset.get("szDecimals"), asset.get("maxLeverage")
            if type(digits) is not int or not 0 <= digits <= 6 or type(maximum) is not int or maximum < 1:
                raise ValueError("Invalid confirmed position market precision or leverage")
            if asset.get("isDelisted"):
                raise ValueError("Confirmed position market is delisted")
            if normalize_perp_size(size, digits) != size or normalize_perp_price(limit_price, digits) != limit_price:
                raise ValueError("Confirmed position order must already match exact lot and tick sizes")
            require_unexpired()
            orders = self.frontend_open_orders(dex)
            if any(not isinstance(row.get("coin"), str) for row in orders):
                raise ValueError("Invalid fresh open-order snapshot")
            if any((f"{dex}:{row['coin']}" if dex and ":" not in row["coin"] else row["coin"]) == coin for row in orders):
                raise ValueError("Open or trigger order conflicts with the position action")
            require_unexpired()
            position = self._matching_confirmed_position(coin, dex, expected_position)
            same_side = is_buy == (position["side"] == "LONG")
            if reduce_only:
                if same_side or size >= position["size"]:
                    raise ValueError("Confirmed reduction must be opposite-side and strictly partial")
            elif not same_side:
                raise ValueError("Confirmed averaging must keep the position direction")
            if not reduce_only and position["leverage"] > maximum:
                raise ValueError("Existing leverage exceeds the current market limit")
            if (position["margin_mode"] == "cross" and
                    (asset.get("onlyIsolated") or asset.get("marginMode") in ("noCross", "strictIsolated"))):
                raise ValueError("Confirmed position margin mode conflicts with metadata")
            require_unexpired()
            return self.exchange.order(coin, is_buy, size, limit_price, {"limit": {"tif": "Ioc"}},
                                       reduce_only=reduce_only, cloid=client_id)

    def submit_copy_ioc(self, coin, is_buy, size, limit_price, reduce_only, cloid, dex="", *, expires_ms):
        """Exact-price copy primitive for the canonical gateway; never reprices.

        Uses the installed SDK's order(..., cloid=...) directly, rather than its
        market helper (which calculates another slippage price). Risk approval,
        source reservation, leverage prerequisites and ownership proof belong to
        the gateway. This method does not grant copy authority or retry outcomes.
        """
        from core.settings import validated_network
        validated_network(getattr(self, "network", None))
        coin = self._position_coin(coin, dex)
        if type(is_buy) is not bool or type(reduce_only) is not bool:
            raise ValueError("Explicit copy side and reduce-only flag required")
        size = self._position_number(size, "copy size")
        limit_price = self._position_number(limit_price, "copy limit")
        if not math.isfinite(size * limit_price):
            raise ValueError("Invalid copy notional")
        client_id = self._user_cloid(cloid)
        with self._position_expiry(expires_ms) as require_unexpired:
            assets = [row for row in self.meta(dex)["universe"]
                      if isinstance(row, dict) and row.get("name") == coin]
            if len(assets) != 1:
                raise ValueError("Missing or ambiguous copy market metadata")
            asset = assets[0]
            digits = asset.get("szDecimals")
            if type(digits) is not int or not 0 <= digits <= 6 or asset.get("isDelisted"):
                raise ValueError("Invalid or delisted copy market")
            if normalize_perp_size(size, digits) != size or normalize_perp_price(limit_price, digits) != limit_price:
                raise ValueError("Copy size and limit must already be normalized")
            require_unexpired()
            try:
                return self.exchange.order(coin, is_buy, size, limit_price, {"limit": {"tif": "Ioc"}},
                                           reduce_only=reduce_only, cloid=client_id)
            except Exception:
                raise RuntimeError("Copy IOC outcome unknown; reconcile before any retry") from None

    def positions(self,crypto=True,stocks=True):
        out=[]
        if crypto:
            try: out += self._positions(self.info.user_state(self.address,dex=""),"CRYPTO","")
            except Exception as e: raise RuntimeError("Cannot read crypto positions") from e
        if stocks:
            try: out += self._positions(self.info.user_state(self.address,dex="xyz"),"STOCKS","xyz")
            except Exception as e: raise RuntimeError("Cannot read xyz positions") from e
        return out

    def verify_copy_execution(self, response, coin, dex, before, after, started_ms):
        """Read-only proof for one acknowledged copy order. Never retries.

        Missing/delayed fills, acknowledgement loss and concurrent same-market
        fills are UNKNOWN, even if an aggregate snapshot looks plausible.
        """
        from core.capital_snapshot import finite_amount
        market = self._sdk_coin(coin, dex)
        try:
            statuses = response["response"]["data"]["statuses"]
            if response["status"] != "ok" or not isinstance(statuses, list) or len(statuses) != 1:
                raise ValueError("Invalid acknowledgement")
            fill = statuses[0]["filled"]
            oid = fill["oid"]
            if isinstance(oid, bool) or not isinstance(oid, int) or oid < 0:
                raise ValueError("Missing order identity")
            quantity = finite_amount(fill["totalSz"], "acknowledged size")
            if quantity <= 0: raise ValueError("Empty fill acknowledgement")
            proof = self.info.query_order_by_oid(self.address, oid)
            wrapper, order = proof["order"], proof["order"]["order"]
            if (proof["status"] != "order" or wrapper["status"] not in ("filled", "canceled", "iocCancel")
                    or order["oid"] != oid or self._sdk_coin(order["coin"], dex) != market):
                raise ValueError("Order identity is unconfirmed")
            rows = self.info.user_fills_by_time(self.address, started_ms)
            if not isinstance(rows, list) or len(rows) >= 2000:
                raise ValueError("Incomplete execution history")
            matching = []
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("coin"), str):
                    raise ValueError("Malformed execution history")
                if self._sdk_coin(row["coin"], dex) != market: continue
                if row.get("oid") != oid:
                    raise ValueError("Concurrent external fill requires reconciliation")
                matching.append(row)
            if not matching or len({row.get("tid") for row in matching}) != len(matching) or any(row.get("tid") is None for row in matching):
                raise ValueError("Missing or duplicate fill identity")
            quantities = [finite_amount(row.get("sz"), "fill size") for row in matching]
            if any(value <= 0 for value in quantities): raise ValueError("Invalid fill size")
            signed = lambda p: 0. if p is None else finite_amount(p["size"], "position size") * (1 if p["side"] == "LONG" else -1)
            change = signed(after) - signed(before)
            side = "B" if change > 0 else "A"
            if (any(row.get("side") != side for row in matching) or order.get("side") != side
                    or not math.isclose(math.fsum(quantities), quantity, rel_tol=1e-8, abs_tol=1e-12)
                    or not math.isclose(abs(change), quantity, rel_tol=1e-8, abs_tol=1e-12)):
                raise ValueError("Fill and position delta disagree")
            return {"oid": oid, "trade_ids": [row["tid"] for row in matching],
                    "filled_size": quantity, "side": side, "network": self.base,
                    "verified_ms": int(time.time() * 1000)}
        except Exception as exc:
            raise ValueError("Copy execution proof unavailable; reconciliation required") from exc

    def verify_ai_closed(self, payload, result):
        """Prove flat plus complete entry/exit fill continuity; never trade."""
        from core.capital_snapshot import finite_amount
        coin = payload["coin"]
        rows = fetch_fills(lambda query: self.info.post("/info", query), self.address,
                           int(payload["created_ms"]), int(time.time() * 1000))
        fills = [row for row in rows if row.get("coin") == coin]
        if not fills or any(row.get("tid") is None for row in fills) or len({row["tid"] for row in fills}) != len(fills):
            raise ValueError("Closure fill history unavailable")
        quantity = finite_amount(result["filled_size"], "AI filled size")
        entry = math.fsum(finite_amount(row["sz"], "entry fill size") for row in fills if str(row.get("oid")) == str(result["oid"]))
        if quantity <= 0 or not math.isclose(entry, quantity, rel_tol=1e-8, abs_tol=1e-12):
            raise ValueError("Entry identity not found in closure history")
        signed = []
        for row in fills:
            size = finite_amount(row.get("sz"), "fill size")
            if size <= 0 or row.get("side") not in ("B", "A"):
                raise ValueError("Invalid closure fills")
            signed.append(size * (1 if row["side"] == "B" else -1))
        if not math.isclose(math.fsum(signed), 0., abs_tol=quantity * 1e-8):
            raise ValueError("Entry and closure fills do not reconcile")
        if any(p["coin"] == coin for p in self.positions(True, True)) or any(o.get("coin") == coin for o in self.frontend_open_orders("")):
            raise ValueError("AI market is not conclusively flat")
        return {"status": "CLOSED_RECONCILED", "entry_oid": result["oid"], "trade_ids": [row["tid"] for row in fills]}
    @staticmethod
    def _positions(state,typ,dex):
        from core.hyperliquid import HyperliquidReader
        return HyperliquidReader._positions(state, typ, dex)
    def mid(self, coin, dex=""):
        m = self.info.all_mids(dex=dex)

        coin = str(coin)
        raw = coin.split(":", 1)[-1]

        if dex:
            key = f"{dex}:{raw}"
            if key in m:
                return float(m[key])

        if raw in m:
            return float(m[raw])

        if coin in m:
            return float(m[coin])

        raise ValueError(f"No mid for {coin}/{dex}")

    def spread_bps(self, coin, dex=""):
        sdk_coin = self._sdk_coin(coin, dex)
        book = self.info.post("/info", {"type":"l2Book", "coin":sdk_coin})
        levels = book.get("levels") or []
        if len(levels) < 2 or not levels[0] or not levels[1]:
            raise RuntimeError(f"No usable orderbook for {sdk_coin}")
        bid, ask = float(levels[0][0]["px"]), float(levels[1][0]["px"])
        if bid <= 0 or ask <= bid: raise RuntimeError(f"Invalid spread for {sdk_coin}")
        return (ask - bid) / ((ask + bid) / 2) * 10_000
    def _sdk_coin(self, coin, dex=""):
        coin = str(coin)

        if dex:
            raw = coin.split(":", 1)[-1]
            return f"{dex}:{raw}"

        return coin.split(":", 1)[-1]

    def set_leverage(self,coin,leverage,dex=""):
        if not self.exchange:
            return {"status":"paper"}

        coin = self._sdk_coin(coin, dex)

        leverage = int(max(1, round(leverage)))

        # XYZ assets marked onlyIsolated/noCross cannot use cross margin.
        # Detect the asset mode from the XYZ metadata.
        is_cross = True

        try:
            meta = self.exchange.info.meta(dex=dex)
            for asset_info in meta.get("universe", []):
                if asset_info.get("name") == coin:
                    if asset_info.get("onlyIsolated") or asset_info.get("marginMode") in (
                        "noCross",
                        "strictIsolated",
                    ):
                        is_cross = False
                    break
        except Exception as e:
            print(f"leverage mode detection failed for {coin}: {e}")

        return self.exchange.update_leverage(
            leverage,
            coin,
            is_cross=is_cross
        )

    def _sz_decimals(self, coin):
        """
        Return Hyperliquid size decimals.

        XYZ assets are resolved directly from the XYZ universe because
        the SDK mapping may not contain names such as xyz:INTC.
        """
        coin = str(coin)

        if coin.startswith("xyz:"):
            if coin not in self._xyz_sz_decimals:
                meta = self.exchange.info.post(
                    "/info",
                    {
                        "type": "meta",
                        "dex": "xyz"
                    }
                )

                self._xyz_sz_decimals = {
                    str(asset.get("name")): int(asset["szDecimals"])
                    for asset in meta.get("universe", [])
                    if asset.get("name") and "szDecimals" in asset
                }

            if coin not in self._xyz_sz_decimals:
                raise KeyError(
                    f"XYZ asset not found in universe: {coin}"
                )

            return self._xyz_sz_decimals[coin]

        asset = self.exchange.info.name_to_asset(coin)
        return int(self.exchange.info.asset_to_sz_decimals[asset])

    def round_size(self, coin, size, dex=""):
        """Normalize lots without exceeding the requested allocation."""
        if not self.exchange:
            return float(size)
        return normalize_perp_size(size, self._sz_decimals(self._sdk_coin(coin, dex)))

    def round_price(self, coin, price, dex=""):
        """Normalize both significant digits and the asset-specific perp tick."""
        if not self.exchange:
            return float(price)
        return normalize_perp_price(price, self._sz_decimals(self._sdk_coin(coin, dex)))

    def size_step(self, coin, dex=""):
        if not self.exchange:
            return 0.0
        return 10 ** (-self._sz_decimals(self._sdk_coin(coin, dex)))

    def market_open(self, coin, is_buy, size, dex="", leverage=1, slippage_pct=0.5):
        slippage_pct = self._slippage(slippage_pct)
        if not self.exchange:
            return {"status": "paper"}

        print(
            f"DEBUG market_open INPUT: coin={coin} "
            f"is_buy={is_buy} size={size!r} dex={dex!r} leverage={leverage}"
        )

        coin = self._sdk_coin(coin, dex)

        # Leverage is set by CopyEngine.set_leverage().
        # Do not change leverage again when submitting the order.

        sz_decimals = self._sz_decimals(coin)

        # Hyperliquid requires size to match szDecimals.
        size = round(float(size), sz_decimals)

        if size <= 0:
            raise ValueError(
                f"Order size rounds to zero: {size} "
                f"(szDecimals={sz_decimals})"
            )

        return self.exchange.market_open(
            coin,
            is_buy,
            size,
            slippage=float(slippage_pct) / 100.0
        )

    @staticmethod
    def _slippage(value):
        from core.capital_snapshot import finite_amount
        value = finite_amount(value, "slippage percent")
        if not 0 < value <= 10: raise ValueError("Slippage percent outside (0,10]")
        return value

    def market_close(self,coin,dex="",slippage_pct=None):
        slippage_pct = self._slippage(slippage_pct if slippage_pct is not None else getattr(self, "close_slippage_pct", 0.5))
        if not self.exchange:
            return {"status":"paper"}

        coin = self._sdk_coin(coin, dex)

        return self.exchange.market_close(coin, slippage=slippage_pct / 100.0)

    def place_stop_loss(self, coin, side, size, trigger_price, dex="", *, cloid=None):
        """Place a reduce-only stop-market order which survives this process."""
        if not self.exchange:
            return {"status": "paper", "response": {"data": {"statuses": [{"resting": {"oid": "paper"}}]}}}
        sdk_coin = self._sdk_coin(coin, dex)
        size = self.round_size(coin, size, dex)
        trigger_price = self.round_price(coin, trigger_price, dex)
        if size <= 0 or trigger_price <= 0:
            raise ValueError("Invalid stop-loss size or price")
        if str(side).upper() not in {"LONG", "SHORT"}:
            raise ValueError("Invalid stop-loss side")
        # A long is closed by a sell; a short is closed by a buy.
        is_buy = str(side).upper() == "SHORT"
        return self.exchange.order(
            sdk_coin, is_buy, size, float(trigger_price),
            {"trigger": {"triggerPx": float(trigger_price), "isMarket": True, "tpsl": "sl"}},
            reduce_only=True, **({'cloid': Cloid.from_str(cloid)} if cloid else {}),
        )

    def cancel_order(self, coin, oid, dex=""):
        if not self.exchange or oid in {None, "", "paper"}:
            return {"status": "paper"}
        return self.exchange.cancel(self._sdk_coin(coin, dex), oid)
    def market_reduce(self, coin, is_buy, size, dex="", slippage_pct=0.5):
        slippage_pct = self._slippage(slippage_pct)
        if not self.exchange:
            return {"status": "paper"}

        coin = self._sdk_coin(coin, dex)

        # XYZ orderbook must be queried directly because the SDK
        # l2_snapshot() does not resolve xyz:<COIN> correctly.
        book = self.exchange.info.post("/info", {
            "type": "l2Book",
            "coin": coin
        })

        levels = book.get("levels") or []
        if len(levels) < 2:
            raise RuntimeError(f"No orderbook available for {coin}")

        bids = levels[0]
        asks = levels[1]

        if not bids or not asks:
            raise RuntimeError(f"Empty orderbook for {coin}")

        # For reducing a LONG we SELL -> use bid.
        # For reducing a SHORT we BUY -> use ask.
        if is_buy:
            px = float(asks[0]["px"])
        else:
            px = float(bids[0]["px"])


        from core.capital_snapshot import finite_amount
        px = finite_amount(px, "reduce order price")
        if px <= 0: raise ValueError("Invalid reduce order price")
        px = self.exchange._slippage_price(coin, is_buy, slippage_pct / 100.0, px)
        sz_decimals = self._sz_decimals(coin)
        size = round(float(size), sz_decimals)

        if size <= 0:
            raise ValueError(
                f"Reduce size rounds to zero: {size} "
                f"(szDecimals={sz_decimals})"
            )

        print(
            f"REDUCE {coin}: "
            f"side={'BUY' if is_buy else 'SELL'} "
            f"size={size} px={px}"
        )

        return self.exchange.order(
            coin,
            is_buy,
            size,
            px,
            {"limit": {"tif": "Ioc"}},
            reduce_only=True
        )

    @staticmethod
    def response_error(response):
        if not isinstance(response, dict):
            return "Empty or invalid exchange response"
        if response.get("status") != "ok":
            return str(response.get("response") or response)
        payload = response.get("response")
        if isinstance(payload, dict):
            statuses = (payload.get("data") or {}).get("statuses") or []
            errors = [str(status["error"]) for status in statuses
                      if isinstance(status, dict) and "error" in status]
            if errors:
                return "; ".join(errors)
        return ""

    @staticmethod
    def order_error(response):
        """Return a human-readable exchange rejection, otherwise an empty string.

        Hyperliquid acknowledges an order at the top level even when one of its
        order statuses was rejected. Looking only at ``status == ok`` is unsafe.
        """
        generic_error = HyperliquidAccount.response_error(response)
        if generic_error:
            return generic_error
        statuses = (((response.get("response") or {}).get("data") or {}).get("statuses") or [])
        if not statuses:
            return "Exchange response contains no order status"
        errors = []
        accepted = False
        for status in statuses:
            if not isinstance(status, dict):
                errors.append(str(status)); continue
            if "error" in status:
                errors.append(str(status["error"]))
            elif "filled" in status or "resting" in status:
                accepted = True
            else:
                errors.append(str(status))
        if errors:
            return "; ".join(errors)
        return "" if accepted else "Order was not accepted by exchange"
    def fills_90d(self, dex=None):
        """Single all-market history feed, locally restricted to perps/DEX.

        HistoryIncomplete propagates rather than presenting retained fragments
        as a complete 90-day performance history.
        """
        end = int(time.time()*1000)
        start = max(0, end-90*24*3600*1000)
        fills = fetch_fills(lambda payload: self.info.post("/info", payload), self.address, start, end)
        return filter_perp_fills(fills, dex)

    def realized_pnl_since(self, start_ms):
        """Perp closedPnl once per fill; unchanged pre-fee/funding semantics.

        This is deliberately not relabelled as net profit. Fees and funding
        require a separate explicitly defined account-PnL calculation.
        """
        end = int(time.time()*1000)
        fills = fetch_fills(lambda payload: self.info.post("/info", payload), self.address, start_ms, end)
        return math.fsum(float(fill["closedPnl"]) for fill in filter_perp_fills(fills))

    def cancel_open_orders(self, owned_order_ids=(), *, include_unrelated=False):
        """Default scope is explicit owned IDs, never all manual protection."""
        if not self.exchange:
            return []
        responses = []
        allowed = {str(oid) for oid in owned_order_ids}
        if not allowed and not include_unrelated: return responses
        for dex in ("", "xyz"):
            for order in self.info.open_orders(self.address, dex=dex):
                coin = str(order.get("coin") or "")
                oid = order.get("oid")
                if not coin or oid is None: continue
                if not include_unrelated and str(oid) not in allowed: continue
                responses.append(self.exchange.cancel(self._sdk_coin(coin, dex), oid))
        return responses
