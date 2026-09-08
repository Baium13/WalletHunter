"""Explainable position reviews. Only an authenticated explicit decision executes.

No LLM, profit prediction or automatic learning is claimed by these rules.
Pending/unknown decisions survive restarts; uncertain orders are never retried.
"""
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from core.execution_journal import ExecutionJournal
from core.ai_policy import source_budget, ReviewPolicy
from core.ai_research import AiResearch
from core.ai_outcomes import OutcomeCosts
from core.ai_candidates import build_candidates
from core.position_history import read_position_episode, HistoryUnavailable
from core.ai_rescue_policy import RESCUE_TRIGGER_ROE_PCT


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite market data")
    return result


def market_key(position):
    dex = position.get("dex") or ""
    coin = str(position["coin"])
    if ":" in coin:
        dex = coin.split(":", 1)[0]
    elif dex:
        coin = f"{dex}:{coin}"
    return f"{coin}|{dex}"


def identity(position):
    return {k: position.get(k) for k in ("coin", "dex", "side", "size", "entry_price", "leverage", "margin_mode")}


@contextmanager
def account_guard(root, address):
    """Nonblocking interprocess exclusion between copying and AI decisions."""
    name = hashlib.sha256(address.lower().encode()).hexdigest()
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    with open(os.path.join(root, "data", f"trade-{name}.lock"), "a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0"); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ema(values, period):
    out = [values[0]]
    alpha = 2 / (period + 1)
    for value in values[1:]: out.append(out[-1] + alpha * (value - out[-1]))
    return out


def analyse(candles, context, now_ms):
    # Closed, ordered candles only. Incomplete/stale data never becomes a signal.
    rows = sorted((r for r in candles if int(r["T"]) < now_ms), key=lambda r: int(r["t"]))
    if len(rows) < 60 or now_ms - int(rows[-1]["T"]) > 35 * 60000:
        raise ValueError("Need at least 60 recent closed 15m candles")
    if any(int(b["t"]) - int(a["t"]) != 900000 for a, b in zip(rows, rows[1:])):
        raise ValueError("Candle history has gaps")
    c, h, l, v = ([number(r[k]) for r in rows] for k in ("c", "h", "l", "v"))
    if min(c + l) <= 0 or min(v) < 0 or any(high < low for high, low in zip(h, l)):
        raise ValueError("Invalid candle values")
    changes = [b - a for a, b in zip(c, c[1:])]
    gain = sum(max(d, 0) for d in changes[:14]) / 14
    loss = sum(max(-d, 0) for d in changes[:14]) / 14
    for d in changes[14:]:
        gain = (gain * 13 + max(d, 0)) / 14
        loss = (loss * 13 + max(-d, 0)) / 14
    rsi = 100 - 100 / (1 + gain / loss) if loss else (100 if gain else 50)
    macd = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    tr = [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])) for i in range(1, len(c))]
    atr = sum(tr[:14]) / 14
    for x in tr[14:]: atr = (atr * 13 + x) / 14
    baseline = sum(v[-21:-1]) / 20
    funding = number(context["funding_bps_hour"])
    oi = number(context["open_interest"])
    if oi < 0: raise ValueError("Invalid open interest")
    return {
        "trend_ema20_50": {"ema20": ema(c, 20)[-1], "ema50": ema(c, 50)[-1]},
        "rsi14": rsi, "macd_hist": macd[-1] - ema(macd, 9)[-1],
        "atr14_pct": atr / c[-1] * 100,
        "volume_ratio20": v[-1] / baseline if baseline else None,
        "levels20": {"support": min(l[-20:]), "resistance": max(h[-20:])},
        "funding_bps_hour": funding, "open_interest": oi,
        "asof_ms": now_ms, "candle_close_ms": int(rows[-1]["T"]),
        "method": "rules-v1; 15m closed candles; OI is a snapshot, not a trend",
    }


class AiReview:
    def __init__(self, root):
        self.root = root
        self.execution_journal = ExecutionJournal(root)
        self.research = AiResearch(root)
        self.path = os.path.join(root, "data", "ai_reviews.sqlite3")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS proposals(
                  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, account TEXT NOT NULL,
                  market TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                  status TEXT NOT NULL, payload TEXT NOT NULL, result TEXT,
                  notified INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS review_user ON proposals(user_id,created);
            """)
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def intervention_gate(self, profile, position):
        address = (profile.get("account") or {}).get("address", "")
        budget = source_budget(profile, position, profile.get("_review_equity", 0), self.execution_journal.owned(address))
        # Ownership can be recovered independently; calibrated probability
        # remains unavailable until validated outcome data and a model exist.
        return {"allowed": False, "minimum_probability": .60,
                "probability": None, "source_budget": budget,
                "policy": profile.get("ai_review_policy") or ReviewPolicy().as_dict(),
                "reason": "calibrated_model_unavailable" if budget["available"] else "source_budget_and_calibrated_model_unavailable"}

    def list(self, uid):
        with closing(self.connect()) as db:
            db.execute("UPDATE proposals SET status='EXPIRED' WHERE status='PENDING' AND expires<?", (time.time(),))
            db.commit()
            rows = db.execute("SELECT * FROM proposals WHERE user_id=? ORDER BY created DESC LIMIT 40", (str(uid),)).fetchall()
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]

    def generate(self, uid, profile, client, reader):
        account = profile.get("account")
        if not account or not profile.get("ai_review_enabled", True): return []
        created = []
        profile = dict(profile)
        profile["_review_equity"] = number(client.balance())
        for p in client.positions(True, True):
            if number(p.get("roe", 0)) > RESCUE_TRIGGER_ROE_PCT: continue
            key, now = market_key(p), time.time()
            with closing(self.connect()) as db:
                recent = db.execute("SELECT 1 FROM proposals WHERE user_id=? AND account=? AND market=? AND "
                                    "(created>? OR status IN ('EXECUTING','UNKNOWN','PARTIAL')) LIMIT 1",
                                    (str(uid), account["address"].lower(), key, now-1800)).fetchone()
            if recent: continue
            try:
                context = reader.market_context(p["coin"], p.get("dex") or "")
                end = int(now * 1000)
                candles = reader._info({"type": "candleSnapshot", "req": {
                    "coin": key.split("|")[0], "interval": "15m", "startTime": end-120*900000, "endTime": end}})
                factors = analyse(candles, context, end)
                price = number(client.mid(p["coin"], p.get("dex") or ""))
                if price <= 0: continue
                size = number(p["size"])
                amount = number(client.round_size(p["coin"], size * .25, p.get("dex") or ""))
                direction = 1 if p["side"] == "LONG" else -1
                trend = factors["trend_ema20_50"]
                adverse = (trend["ema20"] - trend["ema50"]) * direction < 0 and factors["macd_hist"] * direction < 0
                # No unbounded averaging; funding/OI aren't profit predictors.
                gate = self.intervention_gate(profile, p)
                action = "REDUCE" if gate["allowed"] and adverse and 0 < amount < size and amount * price >= 10 else "HOLD"
                payload = {"position": identity(p), "roe": p["roe"], "price": price,
                           "action": action, "size": amount if action == "REDUCE" else 0,
                           "notional_usdc": amount * price if action == "REDUCE" else 0,
                           "extra_margin_usdc": 0, "leverage": p.get("leverage"),
                           "remaining_size": size-amount if action == "REDUCE" else size,
                           "factors": factors, "gate": gate, "copy_hold_after_confirm": True,
                           "reason": "adverse_trend_and_macd" if action == "REDUCE" else "no_supported_action",
                           "unavailable": ["AVERAGE: additional budget and source allocation not validated",
                                           "ADD_MARGIN/LOWER_LEVERAGE: collateral budget not configured"],
                           "warning": "No guaranteed recovery. Reduction realises part of the loss; fees apply."}
                proposal_id = uuid.uuid4().hex
                with closing(self.connect()) as db:
                    db.execute("BEGIN IMMEDIATE")
                    # Recheck inside the transaction to prevent duplicate workers.
                    if db.execute("SELECT 1 FROM proposals WHERE user_id=? AND account=? AND market=? AND created>?",
                                  (str(uid), account["address"].lower(), key, now-1800)).fetchone(): continue
                    db.execute("INSERT INTO proposals(id,user_id,account,market,created,expires,status,payload) VALUES(?,?,?,?,?,?,?,?)",
                               (proposal_id, str(uid), account["address"].lower(), key, now, now+300,
                                "PENDING" if action != "HOLD" else "INFORMATION", json.dumps(payload)))
                    db.commit()
                created.append(proposal_id)
                # Register research independently of notification/confirmation.
                # Missing lifecycle evidence is recorded as unavailable; it is
                # never substituted with a fabricated zero PnL or zero fee.
                try:
                    self.register_research(uid, profile, client, reader, p, proposal_id, payload, end)
                except Exception:
                    pass  # A research failure cannot duplicate a live proposal.
            except (ValueError, KeyError, TypeError):
                # Missing/invalid facts must not create executable proposals.
                continue
        return created

    def register_research(self, uid, profile, client, reader, position, pid, payload, now_ms):
        account = profile["account"]["address"]
        basis = costs = cost_label = None
        candidates = []
        reason = None
        try:
            with account_guard(self.root, account):
                current = next((p for p in client.positions(True, True) if market_key(p) == market_key(position)), None)
                if not current or identity(current) != identity(position):
                    raise HistoryUnavailable("position_changed_during_review")
                observed = int(time.time() * 1000)
                # Review features are frozen earlier. The study begins in the
                # future; keep timestamp ordering explicit, not backdated.
                if observed//900000 != now_ms//900000:
                    raise HistoryUnavailable("review_crossed_forward_candle_boundary")
                with closing(self.connect()) as db:
                    prior = db.execute("SELECT id FROM proposals WHERE account=? AND market=? AND status IN ('EXECUTING','EXECUTED','UNKNOWN','PARTIAL')",
                        (account.lower(), market_key(current))).fetchall()
                usage = (profile.get("runtime", {}).get("ai_budget_usage") or {}).get(market_key(current))
                if usage or prior: raise HistoryUnavailable("prior_intervention_requires_frozen_lifecycle_ledger")
                history = read_position_episode(reader, account, current, now_ms=observed, position_asof_ms=observed,
                    frozen_pre_intervention_margin={"basis":"pre_intervention_exchange_margin",
                        "margin_usdc":current.get("margin_used"), "asof_ms":observed,
                        "evidence":"Fresh exchange margin frozen before any intervention in this research study"},
                    intervention_history=[])
                owned = self.execution_journal.owned(account).get(market_key(current)) or {}
                if history["episode"]["last_fill_ms"] > int(owned.get("verified_at_ms", 0)):
                    raise HistoryUnavailable("fill_after_verified_ownership")
                basis = history["risk_basis"]
                if basis is None: raise HistoryUnavailable(history["risk_basis_unavailable_reason"])
                if current.get("dex") or ":" in current["coin"]:
                    raise HistoryUnavailable("builder_perp_cost_model_not_verified")
                fees = reader._info({"type":"userFees", "user":account})
                fee_bps = number(fees["userCrossRate"]) * 10000
                if not 0 <= fee_bps < 100: raise HistoryUnavailable("fee_rate_invalid")
                funding = payload["factors"]["funding_bps_hour"]
                direction = 1 if current["side"] == "LONG" else -1
                # Constant current funding + 5bps exit slippage are explicit
                # forward assumptions, not future actual costs or fills.
                costs = OutcomeCosts(entry_cost_usdc=0, exit_fee_bps=fee_bps,
                                     exit_slippage_bps=5,
                                     funding_usdc_hour=number(current["size"])*payload["price"]*funding*direction/10000)
                cost_label = "Current userCrossRate; assumed 5bps exit slippage; constant current funding (not actual future costs)"
                budget = payload["gate"]["source_budget"]
                if budget.get("available"):
                    basis["source_wallet"] = budget["source"]
                    # Research clients deliberately have no signing Exchange.
                    # Read lot precision from public core-perp metadata instead
                    # of the SDK helper, whose read-only fallback is size_step=0.
                    metadata = reader._info({"type": "meta"})
                    universe = metadata.get("universe") if isinstance(metadata, dict) else None
                    if not isinstance(universe, list) or any(not isinstance(asset, dict) for asset in universe):
                        raise HistoryUnavailable("research_precision_metadata_invalid")
                    matches = [asset for asset in universe if asset.get("name") == current["coin"]]
                    if len(matches) != 1:
                        raise HistoryUnavailable("research_precision_market_missing_or_ambiguous")
                    digits = matches[0].get("szDecimals")
                    if isinstance(digits, bool) or not isinstance(digits, int) or not 0 <= digits <= 6:
                        raise HistoryUnavailable("research_size_precision_invalid")
                    candidates = build_candidates(dict(current, mark_price=payload["price"]), basis,
                        {"verified":True,"owner_count":1,"wallet":budget["source"],"slot_budget_usdc":budget["slot_usdc"],
                         "free_budget_usdc":max(0,budget["slot_usdc"]-budget["reserved_usdc"]),
                         "additional_spent_usdc":budget["extra_spent_usdc"],"injections_count":budget["additions_count"]},
                        {"label":cost_label,"entry_fee_bps":fee_bps,"exit_fee_bps":fee_bps,"slippage_bps":5},sz_decimals=digits)
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            # Incomplete evidence cannot become a successful zero-cost study.
            basis = costs = cost_label = None
        payload["candidates"] = candidates
        payload["research_input_status"] = "VERIFIED" if basis else "UNAVAILABLE"
        payload["research_unavailable_reason"] = reason
        with closing(self.connect()) as db:
            db.execute("UPDATE proposals SET payload=? WHERE id=? AND status='INFORMATION'", (json.dumps(payload), pid))
            db.commit()
        return self.research.register(uid, account, pid, position, payload,
                                      self.execution_journal, risk_basis=basis, costs=costs,
                                      costs_source=cost_label, now_ms=now_ms)

    def finish(self, pid, status, result):
        with closing(self.connect()) as db:
            db.execute("UPDATE proposals SET status=?,result=? WHERE id=?", (status, json.dumps(result), pid))
            db.commit()

    def decide(self, uid, pid, confirm, storage, client_factory):
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM proposals WHERE id=? AND user_id=?", (pid, str(uid))).fetchone()
            if not row: raise ValueError("Proposal not found")
            if row["status"] != "PENDING": raise ValueError("Proposal already handled or unavailable")
            if row["expires"] < time.time():
                db.execute("UPDATE proposals SET status='EXPIRED' WHERE id=?", (pid,)); db.commit()
                raise ValueError("Proposal expired; wait for a fresh analysis")
            db.execute("UPDATE proposals SET status=? WHERE id=?", ("EXECUTING" if confirm else "DECLINED", pid))
            db.commit()
        if not confirm: return {"status": "DECLINED"}
        submitted = False
        try:
            with account_guard(self.root, row["account"]):
                _, profile = storage.profile(uid)
                if (profile.get("account") or {}).get("address", "").lower() != row["account"]:
                    raise ValueError("Account changed")
                if not profile.get("ai_review_enabled", True):
                    raise ValueError("AI review is disabled; previous confirmation is unavailable")
                client = client_factory(profile)
                if client.exchange is None: raise ValueError("Live API account not available")
                profile["_review_equity"] = number(client.balance())
                payload = json.loads(row["payload"])
                p = next((p for p in client.positions(True, True) if market_key(p) == row["market"]), None)
                if not p or identity(p) != payload["position"]: raise ValueError("Position changed; fresh confirmation required")
                if not self.intervention_gate(profile, p)["allowed"]:
                    raise ValueError("Verified source budget and calibrated probability >=60% required")
                if number(p.get("roe", 0)) > RESCUE_TRIGGER_ROE_PCT: raise ValueError("ROE recovered above trigger; fresh analysis required")
                price = number(client.mid(p["coin"], p.get("dex") or ""))
                if price <= 0 or abs(price/payload["price"]-1) > .005: raise ValueError("Price moved more than 0.5%; proposal is stale")
                if payload["action"] != "REDUCE": raise ValueError("Unsupported action")
                size = number(payload["size"])
                if not 0 < size < number(p["size"]) or size * price < 10: raise ValueError("Size no longer executable")
                # Persist HOLD before sending, preventing copying from undoing
                # the intervention. Unknown outcomes retain this hold.
                runtime = profile["runtime"]
                runtime.setdefault("ai_hold_keys", {})[row["market"]] = {"proposal": pid, "created": time.time()}
                storage.update_runtime(uid, runtime)
                from core.confirmed_execution_adapter import build_context, execute_confirmed_ai
                from core.settings import load
                operation=self.execution_journal.prepare(row['account'],row['market'],
                    {'action':'AI_REVIEW_REDUCE','proposal_id':pid,'network':client.network,'before':p})
                context=build_context(client,tenant=uid,coin=p['coin'],dex=p.get('dex') or '',action='REDUCE',
                    source='ai_review',settings=load(),profile=profile,journal=self.execution_journal,
                    request={'slippage_pct':.5})
                side='BUY' if p['side']=='SHORT' else 'SELL'
                slip=context['policy'].max_slippage_pct
                raw=price*(1+slip/100 if side=='BUY' else 1-slip/100)
                bound=context['round_price'](raw)
                if (bound>raw if side=='BUY' else bound<raw): bound=context['round_price'](price)
                submitted = True
                response=execute_confirmed_ai(context,coin=p['coin'],dex=p.get('dex') or '',side=side,size=size,
                    price=bound,action='REDUCE',source='ai_review',identity=pid,operation_id=operation,
                    expected_position=p,leverage=int(p['leverage']),expires_ms=int(row['expires']*1000))
                status={'FILLED':'EXECUTED','PARTIAL':'PARTIAL','REJECTED':'REJECTED'}.get(response.status,'UNKNOWN')
                after=next((v for v in context['store'].portfolio(response.scope).positions if v.instrument.market_key==row['market']),None)
                remaining=after.size if after else 0.
                self.execution_journal.finish(operation,{'ok':response.status=='FILLED','status':response.status,
                    'execution_evidence':{'intent_id':response.intent_id,'network':response.scope.network}})
                if response.status=='UNKNOWN': raise RuntimeError('Execution outcome unresolved; query recovery required')
                result = {"status": status, "remaining_size": remaining, "copy_on_hold": True}
                self.finish(pid, status, result)
                return result
        except Exception as exc:
            status = "UNKNOWN" if submitted else "INVALIDATED"
            self.finish(pid, status, {"error": str(exc), "retry_allowed": False})
            raise ValueError(f"{status}: {exc}") from exc

    def mark_notified(self, pid):
        with closing(self.connect()) as db:
            db.execute("UPDATE proposals SET notified=1 WHERE id=?", (pid,)); db.commit()

    def resume(self, uid, key, storage):
        _, profile = storage.profile(uid)
        address = (profile.get("account") or {}).get("address")
        if not address: raise ValueError("No account")
        with account_guard(self.root, address):
            _, profile = storage.profile(uid)
            if str((profile.get("account") or {}).get("address") or "").lower() != address.lower():
                raise ValueError("Account changed; reload before resuming")
            from core.ai_position_actions import AiPositionActions
            if key in AiPositionActions(self.root).reserved_markets(None, address):
                raise ValueError("Use the position-assistant resume action after execution reconciliation")
            with closing(self.connect()) as db:
                if db.execute("SELECT 1 FROM proposals WHERE user_id=? AND account=? AND market=? AND status IN ('EXECUTING','UNKNOWN','PARTIAL')",
                              (str(uid), address.lower(), key)).fetchone():
                    raise ValueError("Unresolved execution requires exchange reconciliation first")
            runtime = profile["runtime"]
            manual = runtime.get("manual_actions", {}).get(key, {})
            if manual.get("status") in {"unknown", "submitting", "cleanup_required", "cleanup"}:
                raise ValueError("Manual execution must be reconciled before resuming")
            if key in self.execution_journal.pending(address):
                raise ValueError("Copy execution must be reconciled before resuming")
            # Validate every blocker first, then clear BOTH holds in one saved
            # snapshot; failed manual validation must not partially resume AI.
            runtime.setdefault("ai_hold_keys", {}).pop(key, None)
            runtime["manual_hold_keys"] = [v for v in runtime.get("manual_hold_keys", []) if v != key]
            storage.update_runtime(uid, runtime)
