import asyncio
import time
import os
import math
import uuid
from dataclasses import asdict
from core.execution_journal import ExecutionJournal
from core.ai_user_orders import AiUserOrders
from core.ai_position_actions import AiPositionActions
from core.ai_review import account_guard, market_key
from core.state_snapshot import snapshot, plain
from datetime import datetime, time as clock_time
from dataclasses import dataclass


@dataclass
class Result:
    ok: bool
    action: str
    account: str
    coin: str
    side: str = ""
    target_notional: float = 0.0
    size: float = 0.0
    leverage: float = 1.0
    market_type: str = ""
    dex: str | None = None
    price: float = 0.0
    error: str = ""
    paper: bool = False
    capital_pct: float = 0.0
    target_margin: float = 0.0


class CopyEngine:
    """Synchronise one personal follower with up to three leaders."""
    # Hyperliquid rejected the BTC dust order with: "minimum value of $10".
    # Keep this check in the planner/executor so such orders never reach the
    # exchange or generate a misleading ERROR notification for the user.
    MIN_ORDER_NOTIONAL_USD = 10.0
    def __init__(self, reader, storage, settings):
        self.reader = reader
        self.storage = storage
        self.settings = settings
        self.journal = ExecutionJournal(os.path.dirname(os.path.dirname(storage.path))) if storage and hasattr(storage, "path") else None
        self.ai_user_orders = AiUserOrders(os.path.dirname(os.path.dirname(storage.path))) if self.journal else None
        self.ai_position_actions = AiPositionActions(os.path.dirname(os.path.dirname(storage.path))) if self.journal else None
        self.locks, self.waiting, self.paper_positions, self.daily_cache, self.market_cache = {}, set(), {}, {}, {}
        self.ownership_checks = {}

    RISK_PRESETS = {
        "conservative": {"label": "Консервативный", "multiplier": 0.50, "max_positions": 3, "max_asset_pct": 20.0, "daily_loss_pct": 2.0},
        "standard": {"label": "Стандартный", "multiplier": 1.00, "max_positions": 6, "max_asset_pct": 35.0, "daily_loss_pct": 5.0},
        "aggressive": {"label": "Агрессивный", "multiplier": 1.25, "max_positions": 10, "max_asset_pct": 50.0, "daily_loss_pct": 10.0},
    }
    STRATEGY_PRESETS = {
        "conservative": {"label":"Conservative", "max_spread_bps": 8.0, "max_adverse_funding_bps": 1.0, "stop_roe_pct": 18.0, "liq_buffer_pct": 12.0},
        "swing": {"label":"Swing", "max_spread_bps": 15.0, "max_adverse_funding_bps": 3.0, "stop_roe_pct": 30.0, "liq_buffer_pct": 8.0},
        "scalping": {"label":"Scalping", "max_spread_bps": 5.0, "max_adverse_funding_bps": 0.75, "stop_roe_pct": 12.0, "liq_buffer_pct": 15.0},
        "experimental": {"label":"Experimental", "max_spread_bps": 20.0, "max_adverse_funding_bps": 5.0, "stop_roe_pct": 35.0, "liq_buffer_pct": 7.0},
    }

    def risk(self, profile):
        return self.RISK_PRESETS.get(profile.get("risk_mode"), self.RISK_PRESETS["standard"])

    def strategy(self, profile):
        return self.STRATEGY_PRESETS.get(profile.get("strategy_mode"), self.STRATEGY_PRESETS["swing"])

    @staticmethod
    def _key(coin, dex=""):
        canonical = market_key({"coin": coin, "dex": dex})
        return tuple(canonical.split("|", 1))

    @staticmethod
    def _runtime_key(key):
        return f"{key[0]}|{key[1]}"

    @staticmethod
    def _from_runtime(value):
        coin, _, dex = str(value).partition("|")
        return CopyEngine._key(coin, dex)

    @staticmethod
    def _enabled(position, profile):
        return profile.get("crypto_enabled", True) if position.get("market_type") == "CRYPTO" else profile.get("stocks_enabled", True)

    def desired(self, leader, leader_balance, capital_slice, profile):
        """Preserve the leader's margin percentage on its equal capital slice."""
        if leader_balance <= 0 or capital_slice <= 0:
            return 0.0, 1.0, 0.0, 0.0
        chosen_limit = float(profile.get("max_leverage") or self.settings.max_leverage)
        market_limit = float(leader.get("market_max_leverage") or chosen_limit)
        leverage = min(max(1.0, float(leader.get("leverage") or 1.0)), float(self.settings.max_leverage), chosen_limit, market_limit)
        leader_notional = abs(float(leader.get("position_value") or 0.0))
        leader_leverage = max(1.0, float(leader.get("leverage") or 1.0))
        if not all(math.isfinite(v) and v >= 0 for v in (leader_balance, capital_slice, leader_notional, leverage, leader_leverage)):
            raise ValueError("Invalid balance, leverage or notional")
        margin_pct = min(float(self.settings.max_position_pct), leader_notional / leader_leverage / leader_balance * 100.0)
        target_margin = capital_slice * margin_pct / 100.0
        return target_margin * leverage, leverage, margin_pct, target_margin

    def _plan(self, snapshots, target_balance, profile):
        if not snapshots or target_balance <= 0:
            return {}
        # Equal-slice policy: each configured wallet owns an equal part of the
        # deposit. A paused or empty wallet never donates its slice to another
        # wallet, so enabling/disabling one cannot unexpectedly enlarge risk.
        models = profile.get("leader_models") or {}
        enabled_snapshots = [s for s in snapshots if s.get("enabled", True)]
        analysed = [models.get(str(s.get("wallet", "")).lower()) for s in enabled_snapshots]
        require_approved = any(model is not None for model in analysed)
        active = [s for s in enabled_snapshots if s.get("balance", 0) > 0 and (not require_approved or (models.get(str(s.get("wallet", "")).lower()) or {}).get("eligible"))]
        # Selecting AI in source slot three reserves its third of the deposit.
        # It cannot silently be reallocated to the two real wallets while AI
        # is still collecting shadow evidence.
        configured_count = 3  # User policy: fixed thirds, including empty slots.
        slice_balance = target_balance / configured_count
        plan = {}
        for snapshot in active:
            for p in snapshot.get("positions", []):
                if not self._enabled(p, profile):
                    continue
                notional, leverage, _, margin = self.desired(p, float(snapshot["balance"]), slice_balance, profile)
                if notional <= 0:
                    continue
                key = self._key(p["coin"], p.get("dex"))
                sign = 1 if p.get("side") == "LONG" else -1
                row = plan.setdefault(key, {"signed_notional": 0.0, "entry_sum": 0.0, "entry_weight": 0.0,
                                            "leverage": 1.0, "market_type": p.get("market_type", ""),
                                            "capital_pct": 0.0, "target_margin": 0.0, "sources": []})
                row["sources"].append({"wallet": snapshot["wallet"], "signed_notional": sign*notional,
                                       "margin": margin, "slot_budget": slice_balance})
                row["signed_notional"] += sign * notional
                row["leverage"] = max(row["leverage"], leverage)
                row["capital_pct"] += margin / target_balance * 100.0
                row["target_margin"] += margin
                entry = float(p.get("entry_price") or 0.0)
                if entry > 0:
                    row["entry_sum"] += entry * notional
                    row["entry_weight"] += notional

        risk = self.risk(profile)
        asset_cap = target_balance * risk["max_asset_pct"] / 100.0
        for row in plan.values():
            row["signed_notional"] *= risk["multiplier"]
            row["target_margin"] *= risk["multiplier"]
            row["capital_pct"] *= risk["multiplier"]
            for source in row["sources"]:
                source["signed_notional"] *= risk["multiplier"]
                source["margin"] *= risk["multiplier"]
            if abs(row["signed_notional"]) > asset_cap:
                factor = asset_cap / abs(row["signed_notional"])
                row["signed_notional"] *= factor
                row["target_margin"] *= factor
                row["capital_pct"] *= factor
                for source in row["sources"]:
                    source["signed_notional"] *= factor
                    source["margin"] *= factor
        # Several positions of one source cannot cumulatively exceed its slice.
        per_source = {}
        for row in plan.values():
            for s in row["sources"]: per_source[s["wallet"]] = per_source.get(s["wallet"], 0)+s["margin"]
        for row in plan.values():
            for s in row["sources"]:
                factor = min(1., slice_balance / per_source[s["wallet"]]) if per_source[s["wallet"]] else 1.
                s["signed_notional"] *= factor; s["margin"] *= factor
            row["signed_notional"] = sum(s["signed_notional"] for s in row["sources"])
            row["target_margin"] = abs(row["signed_notional"]) / row["leverage"]
            row["capital_pct"] = row["target_margin"] / target_balance * 100
        gross = sum(abs(row["signed_notional"]) for row in plan.values())
        cap = float(self.settings.max_total_exposure_usd)
        scale = min(1.0, cap / gross) if cap > 0 and gross > cap else 1.0
        for row in plan.values():
            row["signed_notional"] *= scale
            row["target_margin"] *= scale
            row["capital_pct"] *= scale
            for s in row["sources"]:
                s["signed_notional"] *= scale; s["margin"] *= scale
            row["target_notional"] = abs(row["signed_notional"])
            row["side"] = "LONG" if row["signed_notional"] >= 0 else "SHORT"
            row["entry_price"] = row["entry_sum"] / row["entry_weight"] if row["entry_weight"] else 0.0
        return {key: row for key, row in plan.items() if row["target_notional"] > 0}

    async def sync_profile(self, user_id, profile, client, snapshots, notify):
        if not profile.get("copy_enabled") or not profile.get("account"):
            return []
        lock = self.locks.setdefault(str(user_id), asyncio.Lock())
        async with lock:
            root = os.path.dirname(os.path.dirname(self.storage.path))
            try:
                with account_guard(root, profile["account"]["address"]):
                    # A decision may have installed a HOLD after this cycle's
                    # initial snapshot. Reload before any copying action.
                    _, fresh = self.storage.profile(user_id)
                    if not fresh.get("copy_enabled") or fresh.get("account") != profile.get("account"):
                        return []
                    if set(fresh.get("leaders", [])) != {s["wallet"] for s in snapshots}:
                        return []  # Sources changed during the API read; retry fresh.
                    profile.clear(); profile.update(fresh)
                    if hasattr(profile, "base"): profile.base = fresh.base
                    for s in snapshots:
                        s["enabled"] = bool(profile.get("leader_enabled", {}).get(s["wallet"], True))
                    return await self._sync_locked(user_id, profile, client, snapshots, notify)
            except (BlockingIOError, PermissionError):
                return []

    async def _sync_locked(self, user_id, profile, client, snapshots, notify):
        account = profile["account"]
        live = self.settings.auto_trading and client.exchange is not None
        target_balance = await asyncio.to_thread(client.balance)
        if not math.isfinite(float(target_balance)) or target_balance < 0:
            raise ValueError("Invalid follower equity; copying was not attempted")
        runtime = self._mode_runtime(profile, account, live)
        def persist():
            self._persist_mode_runtime(user_id, profile, runtime, live)

        async def safe_notify(result):
            # Telegram delivery is not part of the exchange transaction. A
            # transport failure cannot prevent exits or lose a risk cooldown.
            try:
                await notify(result, account)
            except Exception as exc:
                pending = list(runtime.get("pending_notifications") or [])
                pending.append({"result": asdict(result), "error": str(exc), "attempts": 1,
                                "id": uuid.uuid4().hex,
                                "created_ms": int(time.time() * 1000),
                                "next_retry": time.time() + 60})
                runtime["pending_notifications"] = pending[-100:]
                persist()

        queued = list(runtime.get("pending_notifications") or [])
        retained = []
        delivered = 0
        for item in queued:
            if delivered >= 3 or item.get("next_retry", 0) > time.time():
                retained.append(item); continue
            delivered += 1
            try:
                await notify(Result(**item["result"]), account)
            except Exception as exc:
                item = dict(item, attempts=int(item.get("attempts", 0)) + 1, error=str(exc))
                item["next_retry"] = time.time() + min(3600, 60 * 2 ** min(item["attempts"], 6))
                retained.append(item)
        if queued:
            runtime["pending_notifications"] = retained
            persist()
        # Persisted per market, so a sub-$10 leader adjustment does not spam
        # Telegram on every watcher cycle or after a process restart.
        min_notional_notified = set(runtime.get("min_notional_notified") or [])
        # A single entry can bounce between price, spread and minimum-size
        # checks.  Treat those as one blocked attempt, not a stream of chat
        # alerts, until the market is actually executable or disappears.
        entry_block_notified = set(runtime.get("entry_block_notified") or []) | min_notional_notified
        today = datetime.now().astimezone().date().isoformat()
        if runtime.get("risk_day") != today:
            runtime.pop("blocked_reason", None)
            runtime["risk_day"] = today
            if not live:
                runtime["paper_daily_pnl"] = 0.0
        risk = self.risk(profile)
        now = time.time()
        cache_key = (str(user_id), account["address"].lower(), today)
        cached = self.daily_cache.get(cache_key)
        if not live:
            pnl = float(runtime.get("paper_daily_pnl", 0))
        elif not cached or now - cached[0] >= 60:
            midnight = datetime.combine(datetime.now().astimezone().date(), clock_time.min).astimezone()
            try:
                pnl = await asyncio.to_thread(client.realized_pnl_since, int(midnight.timestamp() * 1000))
                if not math.isfinite(float(pnl)): raise ValueError("Invalid daily PnL")
                self.daily_cache[cache_key] = (now, pnl)
            except Exception:
                pnl = None  # Missing history blocks new risk, never source exits.
        else:
            pnl = cached[1]
        daily_limit = target_balance * risk["daily_loss_pct"] / 100.0
        runtime["daily_pnl"] = pnl
        runtime["daily_limit"] = daily_limit
        daily_blocked = pnl is None or (daily_limit > 0 and pnl <= -daily_limit)
        if daily_blocked:
            reason = "Daily PnL unavailable: new risk paused; leader exits remain enabled." if pnl is None else f"Daily loss limit reached: ${pnl:,.2f} / -${daily_limit:,.2f}"
            first = not runtime.get("blocked_reason")
            runtime["blocked_reason"] = reason
            runtime["last_error"] = reason
            runtime["last_sync_ms"] = int(now * 1000)
            persist()
            if first:
                result = Result(False, "RISK_STOP", account["name"], "", error=reason, paper=not live)
                self._append_journal(runtime, [result])
                persist()
                await safe_notify(result)
                # The daily limit prevents increasing risk, not leader exits.
        else:
            runtime.pop("blocked_reason", None)
        desired = self._plan(snapshots, target_balance, profile)
        # Pausing a leader is deliberately a HOLD policy: it stops new copying
        # and changes from that wallet without silently closing live exposure.
        # It also keeps a shared position intact when another active leader is
        # no longer trading that market.
        paused_keys = {
            self._key(position["coin"], position.get("dex"))
            for snapshot in snapshots if not snapshot.get("enabled", True)
            for position in snapshot.get("positions", []) if self._enabled(position, profile)
        }
        try:
            actual_rows = (await asyncio.to_thread(client.positions, True, True)) if live else list((runtime.get("positions") or {}).values())
        except Exception as exc:
            return [Result(False, "ERROR", account["name"], "", error=f"Cannot read follower positions: {exc}")]
        actual = {self._key(p["coin"], p.get("dex")): p for p in actual_rows}
        if not live:
            # The in-memory direct-reconcile helper must agree with the durable
            # paper book, including when a Hyperliquid account is replaced.
            for memory_key in list(self.paper_positions):
                if memory_key[0] == account["id"]:
                    self.paper_positions.pop(memory_key, None)
            for key, position in actual.items():
                self.paper_positions[(account["id"], key)] = float(position["size"]) * (1 if position["side"] == "LONG" else -1)
        managed = {self._from_runtime(item) for item in runtime.get("managed", [])}
        # An individually confirmed AI order belongs to its own durable ledger,
        # never to a copied wallet. Read account-wide before journal recovery:
        # even stale state.json or a re-bound Telegram profile cannot adopt it.
        # A failed ledger read deliberately stops this cycle before any trade.
        ai_user_holds = set()
        if live:
            ai_user_holds.update(runtime.get("ai_user_order_holds", {}))
            ai_user_holds.update(runtime.get("ai_position_action_holds", {}))
            if self.ai_user_orders:
                ai_user_holds.update(self.ai_user_orders.reserved_markets(None, account["address"]))
            if self.ai_position_actions:
                ai_user_holds.update(self.ai_position_actions.reserved_markets(None, account["address"]))
        owned = self.journal.owned(account["address"]) if live and self.journal else {}
        pending_before_recovery = self.journal.pending(account["address"]) if live and self.journal else set()
        recovery_holds = set()
        # A confirmed SQLite execution may survive a crash before state.json.
        # Recover only its EXACT verified position with no later exchange fills.
        # Matching a symbol alone must never adopt an unrelated manual order.
        for encoded, record in owned.items():
            if encoded in ai_user_holds:
                continue
            key = self._from_runtime(encoded)
            current = actual.get(key)
            if record.get("managed") and not current and key in managed:
                # An exchange SL, liquidation or external manual close must
                # never be immediately undone by opening the old copy target.
                if encoded in pending_before_recovery:
                    recovery_holds.add(encoded)
                    continue
                holds = runtime.setdefault("manual_hold_keys", [])
                if encoded not in holds: holds.append(encoded)
                runtime.setdefault("manual_actions", {})[encoded] = {
                    "action":"observed_flat", "status":"complete", "flat":True,
                    "reason":"Exchange position disappeared outside a confirmed copy close", "created":time.time()}
                persist()  # Durable HOLD before modifying the ownership record.
                operation = self.journal.prepare(account["address"], encoded, {"action":"OBSERVED_FLAT", "before":record})
                self.journal.finish(operation, {"ok":True,"action":"OBSERVED_FLAT","orders_sent":0},
                    dict(record, managed=False, size=0, position=None, attribution="exchange_flat_observed_not_bot_execution"))
                managed.discard(key)
                runtime["managed"] = sorted(self._runtime_key(k) for k in managed)
                persist()
                continue
            if not record.get("managed") or not current:
                if key in managed and current:
                    recovery_holds.add(encoded)
                continue
            saved = record.get("position") or {}
            matches = (current.get("side") == record.get("side") and
                       math.isclose(float(current.get("size", 0)), float(record.get("size", 0)), rel_tol=1e-8, abs_tol=1e-12) and
                       math.isclose(float(current.get("entry_price", 0)), float(saved.get("entry_price", -1)), rel_tol=1e-8, abs_tol=1e-8))
            if not matches:
                recovery_holds.add(encoded)
                continue
            try:
                since = int(record["verified_at_ms"])
                proof_key = (account["address"].lower(), encoded, since)
                cached_proof = self.ownership_checks.get(proof_key, 0)
                history = [] if time.time()-cached_proof < 60 else await asyncio.to_thread(self.reader._info, {
                    "type": "userFillsByTime", "user": account["address"],
                    "startTime": since, "aggregateByTime": False})
                if not isinstance(history, list) or len(history) >= 2000:
                    raise ValueError("Incomplete recovery history")
                if any(self._key(f["coin"], f.get("dex")) == key for f in history):
                    raise ValueError("Later fills require explicit ownership reconciliation")
                self.ownership_checks[proof_key] = time.time()
                if key not in managed:
                    managed.add(key)
                    runtime["managed"] = sorted(self._runtime_key(k) for k in managed)
                    persist()
            except Exception:
                recovery_holds.add(encoded)
        configured = set(profile.get("leaders", []))
        paused_wallets = {s["wallet"] for s in snapshots if not s.get("enabled", True)}
        source_holds = set()
        for encoded, record in owned.items():
            sources = {s.get("wallet") for s in record.get("source_targets", [])}
            if sources & paused_wallets or sources - configured:
                source_holds.add(encoded)
        # Persist pause exposure independently of later leader snapshots.
        pause_memory = runtime.setdefault("paused_source_markets", {})
        for snapshot in snapshots:
            wallet = snapshot["wallet"]
            if snapshot.get("enabled", True):
                pause_memory.pop(wallet, None)
            else:
                keys = {self._runtime_key(self._key(p["coin"], p.get("dex"))) for p in snapshot.get("positions", [])}
                pause_memory[wallet] = sorted(set(pause_memory.get(wallet, [])) | keys)
        for keys in pause_memory.values(): source_holds.update(keys)
        pending = self.journal.pending(account["address"]) if live and self.journal else set()
        held = set(runtime.get("ai_hold_keys", {})) | set(runtime.get("manual_hold_keys", {})) | pending | source_holds | recovery_holds | ai_user_holds
        runtime["recovery_required"] = sorted(pending | recovery_holds)
        # Positions are detached if their source wallet was removed.  They
        # remain visible on the follower and are never silently changed or
        # closed merely because the source is no longer configured.
        detached = {self._from_runtime(item) for item in runtime.get("detached_keys", [])}
        results = []
        cooldowns = dict(runtime.get("cooldowns") or {})
        strategy = self.strategy(profile)

        def commit_positions():
            runtime["managed"] = sorted(self._runtime_key(k) for k in managed)
            runtime["cooldowns"] = dict(cooldowns)
            if not live:
                runtime["positions"] = {self._runtime_key(k): plain(p) for k, p in actual.items()}
            persist()

        # Leader-exit policy: a copied position stays open while its leader
        # keeps it.  ROE/liquidation circuit breakers are available only when
        # explicitly opted into; they must never silently front-run a leader.
        for key in ([] if profile.get("leader_exit_only", True) else list(managed)):
            if self._runtime_key(key) in held: continue
            if key in detached: continue
            position = actual.get(key)
            if not position: continue
            if not self._enabled(position, profile): continue
            close_reason = ""
            if float(position.get("roe", 0) or 0) <= -strategy["stop_roe_pct"]:
                close_reason = f"ROE stop: {float(position.get('roe', 0)):.1f}%"
            liq = float(position.get("liquidation_price", 0) or 0)
            if not close_reason and liq > 0:
                try:
                    mid = await asyncio.to_thread(client.mid, key[0], key[1])
                    distance = abs(mid - liq) / mid * 100 if mid else 100.0
                    if distance <= strategy["liq_buffer_pct"]:
                        close_reason = f"Liquidation buffer: {distance:.1f}%"
                except Exception: pass
            if close_reason:
                result = await self._close(account, client, position, runtime=runtime, persist=persist)
                result.action = "RISK_CLOSE" if result.ok else result.action
                result.error = close_reason if result.ok else result.error
                if result.ok:
                    managed.discard(key); actual.pop(key, None)
                    cooldowns[self._runtime_key(key)] = int((now + 4 * 3600) * 1000)
                    commit_positions()
                results.append(result)
                await safe_notify(result)

        for key, spec in desired.items():
            if self._runtime_key(key) in held: continue
            # Removal is a persistent HOLD, not permission for another source
            # trading the same market to adopt this position automatically.
            if key in detached:
                continue
            cooldown_until = int(cooldowns.get(self._runtime_key(key), 0) or 0)
            if cooldown_until > int(now * 1000):
                continue
            cooldowns.pop(self._runtime_key(key), None)
            existing = actual.get(key)
            if key not in managed and existing:
                results.append(Result(False, "MANUAL_POSITION", account["name"], key[0], existing.get("side", ""),
                                      market_type=spec["market_type"], dex=key[1] or None,
                                      error="Manual position exists: bot will not alter it."))
                continue
            if daily_blocked:
                if existing and existing.get("side") != spec["side"]:
                    # Close the abandoned old side, but do not open the new
                    # side while the daily risk limit prevents fresh exposure.
                    result = await self._close(account, client, existing, runtime=runtime, persist=persist)
                    if result.ok:
                        managed.discard(key); actual.pop(key, None)
                        commit_positions()
                    results.append(result)
                    await safe_notify(result)
                    continue
                if not existing or spec["target_notional"] > float(existing.get("position_value", 0)):
                    continue
                # A daily stop must not raise leverage on retained exposure.
                spec = dict(spec, leverage=min(spec["leverage"], float(existing.get("leverage", 1))))
            if not existing and len(actual) >= risk["max_positions"]:
                notice_key = self._runtime_key(key)
                if notice_key in entry_block_notified: continue
                entry_block_notified.add(notice_key)
                result = Result(False, "MAX_POSITIONS", account["name"], key[0], spec["side"],
                                market_type=spec["market_type"], dex=key[1] or None,
                                error=f"Risk profile permits at most {risk['max_positions']} open positions.")
                results.append(result)
                runtime["entry_block_notified"] = sorted(entry_block_notified)
                persist()
                await safe_notify(result)
                continue
            result = await self._reconcile(account, client, key, spec, existing, key in managed, profile)
            notification_key = self._runtime_key(key)
            if result.action in {"MIN_NOTIONAL_SKIP", "WAIT_PRICE", "WAIT_SPREAD", "WAIT_FUNDING", "EXECUTION_BLOCK", "FUNDING_BLOCK"}:
                if notification_key in entry_block_notified:
                    result.action = "NO_CHANGE"
                else:
                    entry_block_notified.add(notification_key)
            elif result.action in {"OPEN", "ADD", "PARTIAL_CLOSE", "PARTIAL_FILL", "REVERSE", "LEVERAGE_UPDATE"}:
                # A completed executable action makes a later new block worth
                # reporting again.
                entry_block_notified.discard(notification_key)
                min_notional_notified.discard(notification_key)
            if result.ok and result.action in {"OPEN", "ADD", "PARTIAL_CLOSE", "PARTIAL_FILL", "REVERSE", "LEVERAGE_UPDATE", "FULL_CLOSE"}:
                if result.size > 0:
                    managed.add(key)
                    entry = float((existing or {}).get("entry_price") or result.price)
                    if not existing or existing.get("side") != result.side:
                        entry = result.price
                    elif result.size > float(existing.get("size", 0)):
                        old_size = float(existing["size"])
                        entry = (old_size * entry + (result.size-old_size) * result.price) / result.size
                    actual[key] = {"coin": key[0], "dex": key[1], "side": result.side, "size": result.size,
                                   "leverage": result.leverage, "position_value": result.size*result.price,
                                   "entry_price": entry,
                                   "market_type": result.market_type}
                else:
                    managed.discard(key); actual.pop(key, None)
                # Commit ownership before waiting for the next watcher cycle.
                commit_positions()
            if result.action != "NO_CHANGE":
                results.append(result)
                # Save de-duplication markers before potentially slow delivery.
                runtime["entry_block_notified"] = sorted(entry_block_notified)
                persist()
                await safe_notify(result)

        # A disabled market is a hold/pause policy, never a hidden close-all.
        for key in list(managed):
            if self._runtime_key(key) in held: continue
            if key in desired:
                continue
            existing = actual.get(key)
            if not existing:
                managed.discard(key)
                detached.discard(key)
                continue
            if not self._enabled(existing, profile):
                continue
            if key in paused_keys:
                continue
            if key in detached:
                continue
            # An eligibility filter must never masquerade as a leader exit.
            if any(self._key(p["coin"], p.get("dex")) == key for s in snapshots for p in s.get("positions", [])):
                continue
            result = await self._close(account, client, existing, runtime=runtime, persist=persist)
            results.append(result)
            if result.ok:
                managed.discard(key)
                actual.pop(key, None)
                commit_positions()
            await safe_notify(result)

        runtime["managed"] = sorted(self._runtime_key(key) for key in managed)
        runtime["detached_keys"] = sorted(self._runtime_key(key) for key in detached if key in managed)
        runtime["min_notional_notified"] = sorted(
            min_notional_notified & {self._runtime_key(key) for key in desired}
        )
        runtime["entry_block_notified"] = sorted(
            entry_block_notified & {self._runtime_key(key) for key in desired}
        )
        runtime["cooldowns"] = cooldowns
        runtime["last_sync_ms"] = int(now * 1000)
        runtime["last_error"] = next((result.error for result in reversed(results) if result.error), "")
        self._append_journal(runtime, results)
        if not live:
            runtime["positions"] = {self._runtime_key(k): plain(p) for k, p in actual.items()}
        persist()
        return results

    @staticmethod
    def _mode_runtime(profile, account, live):
        shared = profile.setdefault("runtime", snapshot({}))
        if live:
            return shared
        saved = shared.get("paper_runtime") or {}
        if saved.get("account") != account["address"].lower():
            saved = {"account": account["address"].lower(), "managed": [], "positions": {}, "journal": []}
        return snapshot(plain(saved))

    def _persist_mode_runtime(self, user_id, profile, runtime, live):
        if live:
            self.storage.update_runtime(user_id, runtime)
            return
        # Commit the root Snapshot so independent web/live changes are merged.
        # Never substitute a plain dictionary into Storage.update_runtime.
        shared = profile["runtime"]
        shared["paper_runtime"] = plain(runtime)
        self.storage.update_runtime(user_id, shared)
        restored = plain(shared["paper_runtime"])
        runtime.clear(); runtime.update(snapshot(restored)); runtime.base = restored

    @staticmethod
    def _append_journal(runtime, results):
        journal = list(runtime.get("journal") or [])
        stamp = int(time.time() * 1000)
        for result in results:
            if result.action in {"NO_CHANGE", "LEVERAGE_UPDATE"}: continue
            if result.action in {"MANUAL_POSITION", "MAX_POSITIONS", "RISK_STOP", "ERROR"}:
                notices = runtime.setdefault("status_notice_fingerprints", {})
                key = f"{result.action}|{result.coin}|{result.dex or ''}"
                signature = f"{result.side}|{result.size:g}|{result.error}"
                if notices.get(key) == signature: continue
                notices[key] = signature
            journal.append({"time": stamp, "action": result.action, "coin": result.coin,
                            "side": result.side, "notional": round(result.target_notional, 4),
                            "size": round(result.size, 8), "paper": result.paper, "error": result.error})
        runtime["journal"] = journal[-200:]

    def _finish_operation(self, operation, result, position=None, sources=None):
        if operation and self.journal:
            ownership = None
            if result.ok and result.action in {"OPEN", "ADD", "PARTIAL_FILL", "PARTIAL_CLOSE", "REVERSE", "FULL_CLOSE", "LEVERAGE_UPDATE"}:
                ownership = {"managed": result.action != "FULL_CLOSE" and result.size > 0, "size": result.size,
                             "side": result.side, "position": position,
                             "source_targets": sources or [],
                             "attribution": "strategy_targets_not_individual_exchange_fills"}
            self.journal.finish(operation, asdict(result), ownership)
        return result

    async def _margin_preflight(self, client, dex, current_size, current_leverage, target_size, target_leverage, price):
        """Fresh spendable collateral, never portfolio equity or cached balance.

        This only reads the exchange. Rejection precedes PREPARED, so an API
        failure here must not become an ambiguous execution requiring recovery.
        """
        try:
            values = (current_size, current_leverage, target_size, target_leverage, price)
            if not all(math.isfinite(float(v)) for v in values) or min(current_size, target_size) < 0 or min(current_leverage, target_leverage, price) <= 0:
                raise ValueError("Invalid collateral calculation inputs")
            available_method = getattr(client, "available_margin", None)
            if not callable(available_method): raise ValueError("Fresh available collateral API missing")
            available = await asyncio.to_thread(available_method, dex)
            if isinstance(available, bool) or not isinstance(available, (int, float)) or not math.isfinite(available) or available < 0:
                raise ValueError("Invalid available collateral")
            incremental = max(0., target_size * price / target_leverage - current_size * price / current_leverage)
            # Reserve 10bps fees plus the configured worst permitted slippage
            # on newly added notional. This is a buffer, not a fee prediction.
            slippage = float(self.settings.max_slippage_pct)
            if not math.isfinite(slippage) or slippage < 0: raise ValueError("Invalid slippage buffer")
            added_notional = max(0., target_size-current_size) * price
            required = incremental + added_notional * (.001 + slippage / 100.)
            if not math.isfinite(required): raise ValueError("Invalid required collateral")
            if available + 1e-9 < required:
                return f"Insufficient available collateral: ${available:.4f}; required with buffer ${required:.4f}."
            return ""
        except Exception as exc:
            return f"Cannot verify available collateral: {exc}"

    async def _reconcile(self, account, client, key, spec, existing, managed, profile):
        coin, dex = key
        price = await asyncio.to_thread(client.mid, coin, dex)
        if not math.isfinite(price) or price <= 0:
            return Result(False, "ERROR", account["name"], coin, spec["side"], error="Invalid market price")
        live = self.settings.auto_trading and client.exchange is not None
        strategy = self.strategy(profile)
        current_signed = 0.0
        if existing:
            current_signed = float(existing.get("size", 0)) * (1 if existing.get("side") == "LONG" else -1)
        elif not live:
            current_signed = self.paper_positions.get((account["id"], key), 0.0)
        desired_signed = (1 if spec["side"] == "LONG" else -1) * spec["target_notional"] / price
        size_step = 0.0
        if live:
            # Compare and trade only exchange-executable sizes. This prevents a
            # recurring reduce-only order whose delta rounds to zero.
            size_step = await asyncio.to_thread(client.size_step, coin, dex)
            executable = await asyncio.to_thread(client.round_size, coin, abs(desired_signed), dex)
            desired_signed = (1 if desired_signed >= 0 else -1) * executable

        # New entries must be cheap enough to execute and must not immediately
        # pay abnormal funding. Existing positions are never silently changed by
        # these entry filters.
        if not managed and not existing and live:
            try:
                spread = await asyncio.to_thread(client.spread_bps, coin, dex)
                if spread > strategy["max_spread_bps"]:
                    return Result(False, "EXECUTION_BLOCK", account["name"], coin, spec["side"], error=f"Spread {spread:.1f} bps exceeds strategy limit.")
                cache_key = (coin, dex); cached = self.market_cache.get(cache_key)
                if not cached or time.time() - cached[0] > 60:
                    context = await asyncio.to_thread(self.reader.market_context, coin, dex)
                    self.market_cache[cache_key] = (time.time(), context)
                else: context = cached[1]
                funding = float(context.get("funding_bps_hour", 0) or 0)
                adverse = funding if spec["side"] == "LONG" else -funding
                if adverse > strategy["max_adverse_funding_bps"]:
                    return Result(False, "FUNDING_BLOCK", account["name"], coin, spec["side"], error=f"Adverse funding {adverse:.2f} bps/hour exceeds strategy limit.")
            except Exception as exc:
                return Result(False, "EXECUTION_BLOCK", account["name"], coin, spec["side"], error=f"Cannot verify execution conditions: {exc}")

        wait_key = (account["id"], key)
        if not managed and not existing and live and spec["entry_price"] > 0:
            deviation = abs(price - spec["entry_price"]) / spec["entry_price"] * 100.0
            if deviation > self.settings.entry_price_tolerance_pct:
                action = "NO_CHANGE" if wait_key in self.waiting else "WAIT_PRICE"
                self.waiting.add(wait_key)
                return Result(True, action, account["name"], coin, spec["side"], spec["target_notional"], 0,
                              spec["leverage"], spec["market_type"], dex or None, price,
                              f"Waiting for price: deviation {deviation:.2f}% exceeds {self.settings.entry_price_tolerance_pct:.2f}%.", not live,
                              spec["capital_pct"], spec["target_margin"])
        self.waiting.discard(wait_key)
        # A difference of one exchange size step cannot be improved reliably:
        # the next order can round in the opposite direction or be partially
        # filled, which caused repeated BTC ``reduce-only`` dust orders.
        # Treat a full executable step as already reconciled.
        tolerance = max(abs(desired_signed) * 0.002, size_step, 1e-9)
        reverse = (abs(current_signed) > tolerance and abs(desired_signed) > tolerance
                   and current_signed * desired_signed < 0)
        delta = desired_signed - current_signed
        if live and abs(delta) > tolerance and abs(delta) * price < self.MIN_ORDER_NOTIONAL_USD:
            # Dust adjustments of an already open follower position are normal
            # when the leader rebalance is small. Do not trade or notify.
            if abs(current_signed) > tolerance:
                return Result(True, "NO_CHANGE", account["name"], coin, spec["side"], spec["target_notional"], abs(current_signed),
                              spec["leverage"], spec["market_type"], dex or None, price,
                              paper=False, capital_pct=spec["capital_pct"], target_margin=spec["target_margin"])
            # For this event target_notional means the required adjustment,
            # not the already-held total position.  That keeps the user-facing
            # explanation aligned with the exchange's $10 rule.
            return Result(True, "MIN_NOTIONAL_SKIP", account["name"], coin, spec["side"], abs(delta) * price, abs(current_signed),
                          spec["leverage"], spec["market_type"], dex or None, price,
                          f"Сделка не совершена: объём нашей позиции меньше ${self.MIN_ORDER_NOTIONAL_USD:.0f}.", False,
                          spec["capital_pct"], spec["target_margin"])
        operation = None
        try:
            current_leverage = float(existing.get("leverage", 1)) if existing else float(spec["leverage"])
            reducing = (not reverse and current_signed * desired_signed >= 0
                        and abs(desired_signed) < abs(current_signed)-tolerance)
            if live and reducing and spec["leverage"] < current_leverage:
                # Reduce exposure first at its existing leverage. A simultaneous
                # leverage decrease could require collateral BEFORE the reduce
                # fills; defer that independent adjustment to the next cycle.
                spec = dict(spec, leverage=current_leverage,
                            target_margin=abs(desired_signed)*price/current_leverage)
            increases_exposure = abs(desired_signed) > abs(current_signed)+tolerance
            decreases_leverage = bool(existing) and spec["leverage"] < current_leverage
            if live and not reverse and (increases_exposure or decreases_leverage):
                rejection = await self._margin_preflight(client, dex, abs(current_signed), current_leverage,
                                                         abs(desired_signed), spec["leverage"], price)
                if rejection:
                    return Result(False, "EXECUTION_BLOCK", account["name"], coin,
                                  existing.get("side", spec["side"]) if existing else spec["side"],
                                  spec["target_notional"], abs(current_signed), current_leverage,
                                  spec["market_type"], dex or None, price, rejection, False,
                                  spec["capital_pct"], spec["target_margin"])
            lev_changed = bool(existing) and abs(float(existing.get("leverage", 1)) - spec["leverage"]) > 1e-9
            if live and self.journal and (reverse or abs(delta) > tolerance or lev_changed):
                operation = self.journal.prepare(account["address"], self._runtime_key(key), {
                    "before": existing, "target": spec, "action": "RECONCILE"})
            if reverse:
                reversal_runtime = profile.get("runtime") if live and profile.get("user_id") else None
                reversal_persist = (lambda: self.storage.update_runtime(profile["user_id"], reversal_runtime)) if reversal_runtime is not None else None
                close = await self._close(account, client, existing or {"coin": coin, "dex": dex, "side": "LONG" if current_signed > 0 else "SHORT", "leverage": spec["leverage"]}, journalled=False,
                                          runtime=reversal_runtime, persist=reversal_persist)
                if not close.ok:
                    return self._finish_operation(operation, close)
                current_signed = 0.0
                if live and abs(desired_signed) * price < self.MIN_ORDER_NOTIONAL_USD:
                    result = Result(True, "FULL_CLOSE", account["name"], coin, existing.get("side", "") if existing else "",
                                    price=price, dex=dex or None, market_type=spec["market_type"],
                                    error="Old side closed; new reverse position is below the $10 exchange minimum.")
                    return self._finish_operation(operation, result)
                if live:
                    # Closing may release margin, so refetch only after the
                    # exchange confirmed flat and stop cleanup completed.
                    rejection = await self._margin_preflight(client, dex, 0., spec["leverage"],
                                                             abs(desired_signed), spec["leverage"], price)
                    if rejection:
                        close.error = f"Old side closed; reverse entry not placed. {rejection}"
                        return self._finish_operation(operation, close)
            lev_changed = bool(existing) and abs(float(existing.get("leverage", 1)) - spec["leverage"]) > 1e-9
            if live and (not existing or lev_changed):
                error = client.response_error(await asyncio.to_thread(client.set_leverage, coin, spec["leverage"], dex))
                if error:
                    raise RuntimeError(f"Leverage rejected: {error}")
            delta = desired_signed - current_signed
            if delta > tolerance:
                if current_signed < -tolerance:
                    response = await asyncio.to_thread(client.market_reduce, coin, True, delta, dex, self.settings.max_slippage_pct) if live else {"status": "paper"}
                    action = "PARTIAL_CLOSE"
                else:
                    response = await asyncio.to_thread(client.market_open, coin, True, delta, dex, spec["leverage"], self.settings.max_slippage_pct) if live else {"status": "paper"}
                    action = "REVERSE" if reverse else ("ADD" if abs(current_signed) > tolerance else "OPEN")
            elif delta < -tolerance:
                if current_signed > tolerance:
                    response = await asyncio.to_thread(client.market_reduce, coin, False, abs(delta), dex, self.settings.max_slippage_pct) if live else {"status": "paper"}
                    action = "PARTIAL_CLOSE"
                else:
                    response = await asyncio.to_thread(client.market_open, coin, False, abs(delta), dex, spec["leverage"], self.settings.max_slippage_pct) if live else {"status": "paper"}
                    action = "REVERSE" if reverse else ("ADD" if abs(current_signed) > tolerance else "OPEN")
            else:
                result = Result(True, "LEVERAGE_UPDATE" if lev_changed else "NO_CHANGE", account["name"], coin, spec["side"], spec["target_notional"], abs(current_signed),
                              spec["leverage"], spec["market_type"], dex or None, price, paper=not live,
                              capital_pct=spec["capital_pct"], target_margin=spec["target_margin"])
                return self._finish_operation(operation, result, existing, spec.get("sources"))
            if live:
                error = client.order_error(response)
                if error:
                    raise RuntimeError(error)
                snapshot_started_ms = int(time.time() * 1000)
                rows = await asyncio.to_thread(client.positions, True, True)
                actual = next((p for p in rows if self._key(p["coin"], p.get("dex")) == key), None)
                if actual: actual = dict(actual, snapshot_started_ms=snapshot_started_ms)
                size = float(actual.get("size", 0)) if actual else 0.0
                actual_signed = size * (1 if actual and actual.get("side") == "LONG" else -1)
                warning = ""
                if abs(actual_signed - desired_signed) > tolerance:
                    action = "PARTIAL_FILL"
                    warning = f"Exchange reports {actual_signed:g}; target is {desired_signed:g}. Bot will reconcile again."
            else:
                self.paper_positions[(account["id"], key)] = desired_signed
                size = abs(desired_signed)
                warning = ""
            result = Result(True, action, account["name"], coin, actual["side"] if live and actual else spec["side"], spec["target_notional"], size,
                          float(actual.get("leverage", spec["leverage"])) if live and actual else spec["leverage"], spec["market_type"], dex or None, price, warning, not live,
                          capital_pct=spec["capital_pct"], target_margin=spec["target_margin"])
            return self._finish_operation(operation, result, actual if live else None, spec.get("sources"))
        except Exception as exc:
            result = Result(False, "ERROR", account["name"], coin, spec["side"], spec["target_notional"], abs(current_signed),
                          spec["leverage"], spec["market_type"], dex or None, price, str(exc), not live,
                          spec["capital_pct"], spec["target_margin"])
            return self._finish_operation(operation, result)

    async def _close(self, account, client, position, journalled=True, runtime=None, persist=None):
        live = self.settings.auto_trading and client.exchange is not None
        operation = None
        try:
            if live and journalled and self.journal:
                operation = self.journal.prepare(account["address"], market_key(position), {"action": "CLOSE", "before": position})
            response = await asyncio.to_thread(client.market_close, position["coin"], position.get("dex") or "") if live else {"status": "paper"}
            if live:
                error = client.order_error(response)
                if error:
                    raise RuntimeError(error)
                rows = await asyncio.to_thread(client.positions, True, True)
                key = self._key(position["coin"], position.get("dex"))
                remaining = next((p for p in rows if self._key(p["coin"], p.get("dex")) == key
                                  and abs(float(p.get("size", 0))) > 0), None)
                if remaining:
                    raise RuntimeError("Close is not complete: exchange still reports an open position; ownership retained")
                if runtime is not None and persist:
                    from core.manual_positions import ManualPositions
                    await asyncio.to_thread(ManualPositions(client, runtime, persist).delete_stop_loss,
                                            position["coin"], position.get("dex") or "")
            self.paper_positions.pop((account["id"], self._key(position["coin"], position.get("dex"))), None)
            result = Result(True, "FULL_CLOSE", account["name"], position["coin"], position.get("side", ""),
                          leverage=float(position.get("leverage", 1)), market_type=position.get("market_type", ""),
                          dex=position.get("dex"), paper=not live)
            return self._finish_operation(operation, result)
        except Exception as exc:
            result = Result(False, "ERROR", account["name"], position.get("coin", ""), error=str(exc), paper=not live)
            return self._finish_operation(operation, result)

    async def emergency_stop(self, user_id, profile, client, close_managed=False):
        """Pause execution, cancel resting orders, optionally close bot-owned positions."""
        account = profile["account"]
        results = []
        live = self.settings.auto_trading and client.exchange is not None
        runtime = self._mode_runtime(profile, account, live)
        try:
            if live:
                responses = await asyncio.to_thread(client.cancel_open_orders)
                for response in responses:
                    error = client.response_error(response)
                    if error: raise RuntimeError(error)
            results.append(Result(True, "ORDERS_CANCELLED", account["name"], "", paper=not live))
            if close_managed:
                managed = {self._from_runtime(item) for item in runtime.get("managed", [])}
                positions = (await asyncio.to_thread(client.positions, True, True)) if live else list((runtime.get("positions") or {}).values())
                for position in positions:
                    if self._key(position["coin"], position.get("dex")) in managed:
                        closed = await self._close(account, client, position, runtime=runtime,
                            persist=lambda: self._persist_mode_runtime(user_id, profile, runtime, live))
                        results.append(closed)
                        if closed.ok:
                            managed.discard(self._key(position["coin"], position.get("dex")))
                            if not live:
                                runtime.setdefault("positions", {}).pop(market_key(position), None)
                runtime["managed"] = sorted(self._runtime_key(k) for k in managed)
            runtime["blocked_reason"] = "Emergency stop activated"
            runtime["last_sync_ms"] = int(time.time() * 1000)
            self._append_journal(runtime, results)
            self._persist_mode_runtime(user_id, profile, runtime, live)
            return results
        except Exception as exc:
            result = Result(False, "ERROR", account["name"], "", error=f"Emergency stop: {exc}")
            return [result]
