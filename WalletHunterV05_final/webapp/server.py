"""Authenticated Telegram Mini App API for WalletHunter."""
import hashlib
import hmac
import json
import math
import os
import time
import re
import uuid
import sqlite3
from collections import defaultdict, deque
from typing import Literal
from contextlib import ExitStack, contextmanager
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictBool

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import sys
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.hyperliquid import HyperliquidReader
from core.hl_budget import priority_scope
from core.settings import load
from core.storage import Storage, default_profile
from core.state_snapshot import StateConflict
from core.trade_analyzer import TradeAnalyzer
from core.follower_simulator import FollowerSimulator
from core.trading_engine import CopyEngine
from core.ai_assistant import AiAssistant
from core.ai_modes import AiModes
from core.ai_learning_worker import AiLearningWorker
from core.ai_user_orders import AiUserOrders
from core.ai_position_actions import AiPositionActions
from core.ai_review import AiReview, account_guard
from core.manual_positions import ManualPositions, ManualActionError
from core.manual_leader_copy import ManualLeaderCopyService
from core.manual_copy_worker import ManualCopyWorker
from core.confirmed_execution_adapter import build_context
from core.execution_journal import ExecutionJournal
from core.ai_policy import ReviewPolicy
from core.fill_history import HistoryIncomplete
from core.capital_snapshot import UnsupportedCapitalMode
from fastapi.responses import JSONResponse
from integrations.hyperliquid import HyperliquidAccount, verify_account_control

settings = load()
storage = Storage(ROOT, settings.master_key)
reader = HyperliquidReader(settings.hl_mode)
analyzer, simulator = TradeAnalyzer(), FollowerSimulator()
engine = CopyEngine(reader, storage, settings)
ai_assistant = AiAssistant(ROOT)
ai_modes = AiModes(os.path.dirname(os.path.dirname(storage.path)))
ai_learning = AiLearningWorker(os.path.dirname(os.path.dirname(storage.path)))
ai_user_orders = AiUserOrders(os.path.dirname(os.path.dirname(storage.path)))
ai_position_actions = AiPositionActions(os.path.dirname(os.path.dirname(storage.path)))
ai_review = AiReview(ROOT)
execution_journal = ExecutionJournal(os.path.dirname(os.path.dirname(storage.path)))
manual_leader_service: ManualLeaderCopyService | None = None
from core.bounded_cache import BoundedCache
analysis_cache: dict[str, tuple[float, dict]] = BoundedCache(256)
analysis_requests: dict[int, deque[float]] = defaultdict(deque)
chart_cache: dict[tuple[str, str, str, str], tuple[float, list[dict]]] = BoundedCache(256)
CHART_CACHE_TTL = 300.0
CHART_CACHE_PATH = os.path.join(ROOT, "data", "product-market-cache.json")
_chart_cache_loaded = False
price_cache: dict[tuple[str, str], tuple[float, float]] = BoundedCache(256)
markets_cache: tuple[float, list[str]] | None = None
app = FastAPI(docs_url=None, redoc_url=None)

@app.exception_handler(HistoryIncomplete)
async def history_unavailable(request, exc):
    return JSONResponse(status_code=503, content={"detail":{"code":"HISTORY_INCOMPLETE"}})

@app.exception_handler(UnsupportedCapitalMode)
async def capital_mode_unavailable(request, exc):
    return JSONResponse(status_code=503, content={"detail":{"code":"CAPITAL_MODE_UNSUPPORTED"}})
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "webapp", "static")), name="static")


def telegram_user(init_data: str) -> dict:
    """Verify initData according to Telegram Mini Apps validation rules."""
    if not init_data or not settings.telegram_bot_token:
        raise HTTPException(401, "Open this page using the Telegram bot.")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    try:
        auth_date = int(values.get("auth_date", "0") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(401, "Invalid Telegram authentication date") from exc
    now = time.time()
    if not received_hash or not auth_date or auth_date > now + 60 or now - auth_date > 86400:
        raise HTTPException(401, "Telegram session expired. Reopen the application from the bot.")
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", settings.telegram_bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "Invalid Telegram signature.")
    try:
        user = json.loads(values["user"])
        return {"id": int(user["id"]), "name": user.get("first_name") or "Пользователь"}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(401, "Invalid Telegram user.") from exc


def require_user(init_data: str | None) -> dict:
    return telegram_user(init_data or "")


from webapp.intelligence_api import router as intelligence_router
app.include_router(intelligence_router(os.path.join(ROOT, 'data', 'intelligence.sqlite'), settings.hl_mode, require_user))

from webapp.product_api import router as product_router
from core.product_runtime import view_for
from core.product_events import ProductEvents
from integrations.product_confirmation import confirmed_backend

def product_view(uid):
    _,profile=storage.profile(uid)
    return view_for(ROOT,uid,profile,settings.hl_mode)

def product_market(scope,coin,interval,hours,token):
    # Read-only compatibility boundary: reuse existing bounded chart/price caches,
    # never instantiate another client or interpret a candle as a live mark.
    if scope.network!=settings.hl_mode:raise HTTPException(409,'MARKET_NETWORK_MISMATCH')
    # A candle request is the expensive part of this combined product read. If
    # its bounded Hyperliquid budget is temporarily exhausted, keep the
    # lightweight mark path available so an open position still has a current
    # price/PnL instead of falling back to UNKNOWN. The product API will show
    # an empty candle history for that refresh and the next refresh can fill it.
    from core.hl_budget import BudgetUnavailable
    _load_chart_cache()
    cache_key = (scope.network, coin, interval, str(hours))
    try:
        data=chart(coin,interval,hours,token)
    except BudgetUnavailable:
        # Budget deferral must not erase a previously proven chart history.
        # The mark remains independently available through the P1 price path.
        cached = chart_cache.get(cache_key)
        data={'coin':coin,'interval':interval,'hours':hours,
              'candles':list(cached[1]) if cached else []}
    dex,symbol=coin.split(':',1) if ':' in coin else ('',coin)
    try:mark=price(symbol,dex,token,scope.network)
    except Exception:mark=None
    return {**data,'mark':mark}

app.include_router(product_router(require_user,product_view,
    lambda:ProductEvents(os.path.join(ROOT,'data','product.sqlite3')),
    lambda binding:confirmed_backend(ROOT,binding,storage,settings),product_market))


def short_address(value: str) -> str:
    return value if len(value) <= 12 else f"{value[:6]}…{value[-4:]}"


def leader_on(profile: dict, wallet: str) -> bool:
    return bool((profile.get("leader_enabled") or {}).get(wallet, True))


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "webapp", "static", "index.html"), headers={"Cache-Control":"no-store"})


@app.get("/health")
def health():
    from core.product_read import ProductReadModel,observed
    from core.foundation.contracts import Scope
    now=int(time.time()*1000)
    scope=Scope(tenant='public-health',account='0x'+'0'*40,network=settings.hl_mode)
    research=ProductReadModel(ROOT,scope).discovery()
    return {'ok':True,'status':'DEGRADED','network':settings.hl_mode,'checked_ms':now,
        'private_health':'AUTHENTICATED_API_REQUIRED','components':{
            'Web/API':observed(now,now),'Discovery':{k:research.get(k) for k in ('status','last_success_ms','heartbeat_ms')},
            'Risk':observed(None,now,critical=True),'Execution':observed(None,now,critical=True)}}


@app.get("/api/dashboard")
def dashboard(x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    _, profile = storage.profile(user["id"])
    account = profile.get("account")
    balance, positions, balance_error = 0.0, [], None
    if account:
        try:
            balance = reader.balance(account["address"])
        except UnsupportedCapitalMode:
            balance, balance_error = None, "CAPITAL_MODE_UNSUPPORTED"
        except Exception:
            balance, balance_error = None, "BALANCE_UNAVAILABLE"
        positions = reader.positions(account["address"], True, True)
    leaders = profile.get("leaders") or []
    runtime = profile.get("runtime") or {}
    manual_stops = runtime.get("manual_stops") or {}
    ownership = execution_journal.owned(account["address"]) if account else {}
    ai_reservations = ai_user_orders.reserved_markets(None, account["address"]) if account else {}
    intervention_holds = ai_position_actions.reserved_markets(None, account["address"]) if account else {}
    action_summary = ai_position_actions.summary(user["id"], profile, int(time.time()*1000))
    try:
        leverage_choices = [item for item in reader.leverage_choices() if item <= settings.max_leverage]
    except Exception:
        leverage_choices = [1, 2, 3, 5, 10, 20]
    return {
        "user": user["name"],
        "live": settings.auto_trading,
        "execution_scope": {"network": settings.hl_mode,
            "copy_mode": "LIVE" if settings.auto_trading else "PAPER",
            "manual_and_confirmed_orders": "EXPLICIT_CONFIRMATION_CAN_EXECUTE_ON_SELECTED_NETWORK",
            "autonomous_ai_mainnet": False},
        "copy_enabled": bool(profile.get("copy_enabled")),
        "balance": balance,
        "balance_error": balance_error,
        "account": short_address(account["address"]) if account else None,
        "max_leverage": profile.get("max_leverage") or settings.max_leverage,
        "platform_leverage_choices": sorted(set([1, *leverage_choices])),
        "server_max_leverage": settings.max_leverage,
        "crypto_enabled": bool(profile.get("crypto_enabled", True)),
        "stocks_enabled": bool(profile.get("stocks_enabled", True)),
        "notifications": bool(profile.get("notifications", True)),
        "risk_mode": profile.get("risk_mode", "standard"),
        "strategy_mode": profile.get("strategy_mode", "swing"),
        "language": profile.get("language", "ru"),
        "position_actions_pending": len(action_summary.get("pending") or []),
        "ai_entries_pending": len(ai_user_orders.summary(user["id"], profile, int(time.time()*1000)).get("pending") or []),
        # Slot selection alone never grants permission to send an order.
        "ai_slot_selected": bool(profile.get("ai_slot_selected", False)),
        "wallets": [{"slot": index + 1, "enabled": leader_on(profile, wallet), "configured": True}
                    for index, wallet in enumerate(leaders)] +
                   [{"slot": index + 1, "enabled": False, "configured": False}
                    for index in range(len(leaders), 3)],
        "positions": [dict(row, stop_loss=manual_stops.get(engine._runtime_key(engine._key(row["coin"], row.get("dex")))),
                           origin=position_origin(row, profile, ownership, ai_reservations, intervention_holds))
                      for row in positions[:20]],
        "events": list(runtime.get("journal") or [])[-12:][::-1],
    }


class Action(BaseModel):
    value: bool
    confirm_open_positions: bool = False

class SettingsPatch(BaseModel):
    crypto_enabled: bool | None = None
    stocks_enabled: bool | None = None
    notifications: bool | None = None
    risk_mode: str | None = None
    strategy_mode: str | None = None
    max_leverage: int | None = None
    language: str | None = None

class WalletInput(BaseModel):
    address: str

class AccountInput(BaseModel):
    name: str
    address: str
    private_key: str
    confirm_open_positions: bool = False

class EmergencyInput(BaseModel):
    close_managed: bool = False


class AiSlotInput(BaseModel):
    value: bool


class DeleteWalletInput(BaseModel):
    confirm_open_positions: bool = False

class ManualCopyInput(BaseModel):
    leader: str | None = None
    allocation_pct: float | None = None
    action: Literal["start", "stop"] | None = None


class PositionInput(BaseModel):
    coin: str
    dex: str = ""


class StopLossInput(PositionInput):
    price: float

ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def position_summary(rows: list[dict]) -> list[dict]:
    """Return only the information needed for a destructive-action warning."""
    return [{"coin": str(row.get("coin") or "?"), "side": str(row.get("side") or "")}
            for row in rows if row.get("coin")]


def position_origin(row, profile, ownership, ai_reservations=None, intervention_holds=None):
    key = engine._runtime_key(engine._key(row["coin"], row.get("dex")))
    if key in (intervention_holds or {}):
        return {"managed": True, "source_slots": [], "basis": "user_confirmed_position_action",
                "held": True, "controller": "ai_position_action",
                "execution_status": intervention_holds[key]["status"]}
    if key in (ai_reservations or {}):
        return {"managed": False, "source_slots": [], "basis": "user_confirmed_ai_form_hold",
                "held": True, "controller": "ai_user_confirmed",
                "execution_status": ai_reservations[key]["status"]}
    record = ownership.get(key) or {}
    sources = record.get("source_targets") or []
    wallets = profile.get("leaders") or []
    slots = sorted({wallets.index(s["wallet"])+1 for s in sources if s.get("wallet") in wallets})
    return {"managed": key in profile.get("runtime", {}).get("managed", []),
            "source_slots": slots, "basis": record.get("attribution", "unknown"),
            "held": key in profile.get("runtime", {}).get("ai_hold_keys", {}) or
                    key in profile.get("runtime", {}).get("manual_hold_keys", [])}


def safe_positions(address: str) -> list[dict]:
    try:
        return reader.positions(address, True, True)
    except Exception as exc:
        # A temporary public API fault must not make it possible to bypass a
        # confirmation dialog for an action that removes trading control.
        raise HTTPException(503, "Не удалось проверить открытые позиции. Повторите попытку.") from exc


def affected_source_positions(profile, wallet):
    account=profile.get("account")
    if not account: return []
    records=execution_journal.owned(account["address"])
    positions=safe_positions(account["address"])
    out=[]
    for p in positions:
        key=engine._runtime_key(engine._key(p["coin"],p.get("dex")))
        record=records.get(key)
        if record is None or any(s.get("wallet")==wallet for s in record.get("source_targets",[])):
            out.append(p)
    return out


@contextmanager
def mutation_profile(user_id, extra_addresses=()):
    """Serialize profile changes with in-flight orders; never wait on trading.

    The profile lock also protects a currently unbound user. Account locks use
    the same interprocess guard as copying/manual actions, including an optional
    new account during binding. Always reload after acquiring those locks.
    """
    with ExitStack() as locks:
        try:
            locks.enter_context(account_guard(ROOT, f"telegram-profile:{user_id}"))
            _, initial = storage.profile(user_id)
            initial_account = initial.get("account")
            current_address = (initial_account or {}).get("address", "")
            addresses = {str(v).strip().lower() for v in (current_address, *extra_addresses) if v}
            for address in sorted(addresses):
                locks.enter_context(account_guard(ROOT, address))
        except OSError as exc:
            raise HTTPException(409, "Account action is in progress; retry after refresh") from exc
        data, profile = storage.profile(user_id)
        if profile.get("account") != initial_account:
            raise HTTPException(409, "Account changed; reload before changing settings")
        try:
            yield data, profile
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from exc


@app.post("/api/copy")
def set_copy(payload: Action, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (data, profile):
        if payload.value and (not profile.get("account") or not any(leader_on(profile, x) for x in profile.get("leaders") or [])):
            raise HTTPException(400, "Подключите Hyperliquid и включите хотя бы один кошелёк.")
        if payload.value:
            # Verify capital semantics before enabling live copying. Never
            # guess a portfolio/legacy mode or turn an API failure into zero.
            addresses=[profile["account"]["address"]]+[w for w in profile.get("leaders",[]) if leader_on(profile,w)]
            for address in addresses:
                try:
                    capital=float(reader.balance(address))
                    if not math.isfinite(capital) or capital<=0:raise ValueError("Capital unavailable")
                except UnsupportedCapitalMode:
                    raise
                except Exception as exc:
                    raise HTTPException(503,{"code":"BALANCE_UNAVAILABLE"}) from exc
        if not payload.value and profile.get("account"):
            positions = safe_positions(profile["account"]["address"])
            if positions and not payload.confirm_open_positions:
                raise HTTPException(409, {"message": "Есть открытые позиции. Подтвердите остановку копирования.",
                                          "positions": position_summary(positions)})
        profile["copy_enabled"] = payload.value
        data["profiles"][str(user["id"])] = profile
        storage.save(data)
    return {"ok": True, "copy_enabled": payload.value}


@app.post("/api/wallet/{slot}")
def set_wallet(slot: int, payload: Action, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (data, profile):
        wallets = profile.get("leaders") or []
        if not 1 <= slot <= len(wallets):
            raise HTTPException(404, "Кошелёк не настроен.")
        if not payload.value:
            positions = affected_source_positions(profile, wallets[slot - 1])
            if positions and not payload.confirm_open_positions:
                raise HTTPException(409, {"message": "У этого кошелька есть открытые позиции. Подтвердите остановку.",
                                          "positions": position_summary(positions)})
        profile.setdefault("leader_enabled", {})[wallets[slot - 1]] = payload.value
        data["profiles"][str(user["id"])] = profile
        storage.save(data)
    return {"ok": True, "enabled": payload.value}


@app.post("/api/ai/slot")
def set_ai_slot(payload: AiSlotInput, x_telegram_init_data: str | None = Header(default=None)):
    """Reserve slot three; this preference never sends or authorizes an order."""
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (data, profile):
        leaders = profile.get("leaders") or []
        if payload.value and len(leaders) != 2:
            raise HTTPException(400, "AI можно выбрать только при двух добавленных кошельках.")
        profile["ai_slot_selected"] = payload.value
        data["profiles"][str(user["id"])] = profile
        storage.save(data)
    return {"ok": True, "selected": payload.value, "active": False}

@app.put("/api/settings")
def update_settings(payload: SettingsPatch, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (data, profile):
        allowed = {"crypto_enabled", "stocks_enabled", "notifications", "risk_mode", "strategy_mode", "max_leverage", "language"}
        values = payload.model_dump(exclude_none=True)
        for key, value in values.items():
            if key not in allowed:
                continue
            if key == "risk_mode" and value not in {"conservative", "standard", "aggressive"}:
                raise HTTPException(400, "Unknown risk profile.")
            if key == "strategy_mode" and value not in {"conservative", "swing", "scalping", "experimental"}:
                raise HTTPException(400, "Unknown strategy.")
            if key == "max_leverage" and not 1 <= int(value) <= settings.max_leverage:
                raise HTTPException(400, "Invalid leverage.")
            if key == "language" and value not in {"ru", "en"}:
                raise HTTPException(400, "Unknown language.")
            profile[key] = value
        data["profiles"][str(user["id"])] = profile
        storage.save(data)
    return {"ok": True}

@app.post("/api/wallet")
def add_wallet(payload: WalletInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    wallet = payload.address.strip().lower()
    if not ADDRESS.fullmatch(wallet):
        raise HTTPException(400, "Неверный адрес кошелька.")
    with mutation_profile(user["id"]) as (data, profile):
        wallets = profile.setdefault("leaders", [])
        if wallet in wallets:
            raise HTTPException(400, "Кошелёк уже добавлен.")
        if len(wallets) >= 3:
            raise HTTPException(400, "Можно добавить максимум три кошелька.")
        if profile.get("ai_slot_selected") and len(wallets) == 2:
            raise HTTPException(400, "Третий слот занят AI. Сначала отключите AI-слот.")
        wallets.append(wallet)
        profile.setdefault("leader_enabled", {})[wallet] = True
        data["profiles"][str(user["id"])] = profile
        storage.save(data)
    return {"ok": True}

@app.get("/api/wallet/{slot}/open-positions")
def wallet_open_positions(slot: int, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    _, profile = storage.profile(user["id"])
    wallets = profile.get("leaders") or []
    if not 1 <= slot <= len(wallets):
        raise HTTPException(404, "Кошелёк не настроен.")
    return {"positions": position_summary(safe_positions(wallets[slot - 1]))}


@app.delete("/api/wallet/{slot}")
def delete_wallet(slot: int, payload: DeleteWalletInput | None = None,
                  x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (data, profile):
        wallets = profile.get("leaders") or []
        if not 1 <= slot <= len(wallets):
            raise HTTPException(404, "Кошелёк не настроен.")
        wallet = wallets[slot - 1]
        positions = affected_source_positions(profile, wallet)
        if positions and not (payload and payload.confirm_open_positions):
            raise HTTPException(409, {"message": "У этого кошелька есть открытые позиции. Подтвердите удаление.",
                                      "positions": position_summary(positions)})
        # Detach only bot-managed markets that are still open on this source.
        # The engine will keep them untouched after the source is removed.
        runtime = profile.setdefault("runtime", {})
        managed = set(runtime.get("managed") or [])
        detached = set(runtime.get("detached_keys") or [])
        for row in positions:
            key = engine._runtime_key(engine._key(row["coin"], row.get("dex")))
            if key in managed:
                detached.add(key)
        runtime["detached_keys"] = sorted(detached)
        wallets.pop(slot - 1)
        profile.setdefault("leader_enabled", {}).pop(wallet, None)
        data["profiles"][str(user["id"])] = profile
        storage.save(data)
    return {"ok": True}

@app.post("/api/emergency")
async def emergency_stop(payload: EmergencyInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (data, profile):
        if not profile.get("account"):
            raise HTTPException(400, "Hyperliquid-аккаунт не подключён.")
        # The pause must survive even if exchange cancellation/closure fails.
        profile["copy_enabled"] = False
        storage.update_profile(user["id"], profile)
        try:
            client = account_client_for(profile)
            results = await engine.emergency_stop(user["id"], profile, client, payload.close_managed)
        except Exception as exc:
            raise HTTPException(500, f"Emergency stop: {exc}") from exc
    errors = [result.error for result in results if not result.ok]
    return {"ok": not errors, "errors": errors, "closed": bool(payload.close_managed and not errors),
            "closed_requested": payload.close_managed}

@app.put("/api/account")
def set_account(payload: AccountInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    if not ADDRESS.fullmatch(payload.address.strip()) or not payload.private_key.strip():
        raise HTTPException(400, "Проверьте адрес и API private key.")
    new_address = payload.address.strip().lower()
    with mutation_profile(user["id"], (new_address,)) as (data, profile):
        existing_account = profile.get("account") or {}
        same_account = str(existing_account.get("address") or "").lower() == new_address
        if any(uid != str(user["id"]) and (p.get("account") or {}).get("address", "").lower() == new_address
               for uid,p in data["profiles"].items()):
            raise HTTPException(409, "This Hyperliquid account is already linked to another profile")
        try:
            control = verify_account_control(new_address, payload.private_key.strip(), lambda query: reader._info(query))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if existing_account and not same_account:
            if safe_positions(existing_account["address"]) and not payload.confirm_open_positions:
                raise HTTPException(409, "Existing account has open positions; confirm before replacing it")
            old_runtime = profile.get("runtime") or {}
            keep = {k:old_runtime[k] for k in ("chat_message_ids","notification_message_ids","controller_message_id",
                                               "journal_cleared_at","message_tombstones") if k in old_runtime}
            profile["runtime"] = {**default_profile(user["id"])["runtime"], **keep}
        profile["account"] = {
            "id": existing_account.get("id") if same_account else uuid.uuid4().hex,
            "name": payload.name.strip()[:40] or "My Hyperliquid",
            "address": new_address,
            "private_key": storage.encrypt(payload.private_key.strip()),
            "control": dict(control, verified_network=settings.hl_mode, verified_ms=int(time.time()*1000)),
        }
        profile["copy_enabled"] = False
        data["profiles"][str(user["id"])] = profile
        storage.save(data)
    return {"ok": True}

@app.get("/api/account/open-positions")
def account_open_positions(x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    _, profile = storage.profile(user["id"])
    account = profile.get("account")
    return {"positions": position_summary(safe_positions(account["address"])) if account else []}


def account_client_for(profile: dict) -> HyperliquidAccount:
    account = profile.get("account")
    if not account:
        raise HTTPException(400, "Hyperliquid-аккаунт не подключён.")
    return HyperliquidAccount(account["address"], storage.decrypt(account["private_key"]), settings.hl_mode, slippage_pct=settings.max_slippage_pct)


def manual_leader_controller() -> ManualLeaderCopyService:
    global manual_leader_service
    if manual_leader_service is None:
        manual_leader_service = ManualLeaderCopyService(engine)
    return manual_leader_service


def manual_leader_account(user_id: int, profile: dict, *, read_exchange=True):
    account = profile.get("account")
    if not account:
        raise HTTPException(400, "Hyperliquid account is not connected")
    from core.settings import validated_network
    from types import SimpleNamespace
    network = validated_network(settings.hl_mode)
    scoped = {"address": account["address"], "_tenant": str(user_id)}
    if not read_exchange:
        # Saving a public wallet, reading configuration and pausing need no
        # exchange metadata/history request (and no signing credentials).
        return scoped, SimpleNamespace(network=network, address=account["address"])
    try:
        # Configuration/start/stop are public strategy mutations; no signer is
        # required and the private key must not be loaded for these endpoints.
        client = HyperliquidAccount(account["address"], None, settings.hl_mode)
    except Exception as exc:
        from core.hl_budget import BudgetUnavailable
        if isinstance(exc, BudgetUnavailable):
            raise
        raise HTTPException(400, "Hyperliquid account is unavailable") from exc
    # ManualLeaderCopyService uses the tenant in Scope; do not persist secrets
    # or return this enriched dictionary to the client.
    return scoped, client


@app.get("/api/manual-copy")
def manual_copy_config(x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    _, profile = storage.profile(user["id"])
    account = profile.get("account")
    if not account:
        return {"configured": False, "enabled": False, "allocation_pct": None, "leader": None}
    try:
        scoped, client = manual_leader_account(user["id"], profile, read_exchange=False)
        config = manual_leader_controller().config(scoped, client)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, {"code": "MANUAL_STATE_UNAVAILABLE"}) from None
    if not config:
        return {"configured": False, "enabled": False, "allocation_pct": None, "leader": None}
    return {"configured": True, "enabled": bool(config.enabled), "allocation_pct": config.allocation_pct,
            "leader": config.leader, "alias": config.alias, "updated_ms": config.updated_ms,
            "network": config.scope.network, "generation_id": config.generation_id,
            "runtime": ManualCopyWorker(engine,reader).diagnostics(config.scope)}


@app.put("/api/manual-copy")
def configure_manual_copy(payload: ManualCopyInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    _, profile = storage.profile(user["id"])
    scoped, client = manual_leader_account(user["id"], profile, read_exchange=False)
    controller = manual_leader_controller()
    try:
        with account_guard(ROOT, scoped['address']):
            config = controller.config(scoped, client)
            if payload.leader is not None:
                leader = payload.leader.strip().lower()
                if not re.fullmatch(r"0x[0-9a-fA-F]{40}", leader):
                    raise ValueError("Invalid Hyperliquid leader address")
            elif config:
                leader = config.leader
            else:
                raise ValueError("Manual leader is required")
            allocation = payload.allocation_pct if payload.allocation_pct is not None else (config.allocation_pct if config else 80.0)
            if payload.action == "start":
                from core.hl_budget import priority_scope
                from core.interactive_budget import manual_review_budget
                from webapp.manual_preview import current_evidence
                # The same bounded interactive budget as preview, including
                # client construction. START still re-reads all safety evidence.
                with manual_review_budget(), priority_scope('manual_start.current_evidence', 2):
                    scoped, client = manual_leader_account(user['id'], profile)
                    baseline = current_evidence(ManualCopyWorker(engine,reader),scoped,client,leader)
                    config = controller.start(scoped, client, baseline=baseline,
                                              leader=leader, allocation_pct=allocation)
            elif payload.action == "stop": config, _ = controller.stop(scoped, client)
            else: config = controller.configure(scoped, client, leader, allocation, alias=leader)
    except OSError:
        raise HTTPException(409, "Account action in progress; refresh before changing Manual Copy") from None
    except (ValueError, TypeError) as exc:
        safe_codes = {'UNRESOLVED_EXECUTION', 'OUTSTANDING_LIVE_GRANT',
                      'MANUAL_COPY_ALREADY_ACTIVE', 'FRESH_START_BASELINE_REQUIRED',
                      'MANUAL_COPY_MAINNET_BLOCKED', 'LEADER_NETWORK_MISMATCH'}
        raise HTTPException(400, {'code': str(exc) if str(exc) in safe_codes else 'MANUAL_VALIDATION_FAILED'}) from None
    except HTTPException:
        raise
    except Exception as exc:
        from webapp.manual_preview import failure
        raise HTTPException(503, failure(exc, getattr(exc, 'preview_stage', 'START_EVIDENCE')),
                            headers={'Retry-After': '15'}) from None
    return {"configured": True, "enabled": bool(config.enabled), "allocation_pct": config.allocation_pct,
            "leader": config.leader, "alias": config.alias, "updated_ms": config.updated_ms,
            "network": config.scope.network, "generation_id": config.generation_id}


@app.post("/api/manual-copy/preview")
def preview_manual_copy(payload: ManualCopyInput, x_telegram_init_data: str | None = Header(default=None)):
    """Read-only analysis. No configuration, generation, grant or order created."""
    user=require_user(x_telegram_init_data)
    _,profile=storage.profile(user['id'])
    leader=(payload.leader or '').strip().lower()
    if not re.fullmatch(r'0x[0-9a-f]{40}',leader): raise HTTPException(400,'Invalid Hyperliquid leader address')
    from core.hl_budget import priority_scope
    from core.interactive_budget import manual_review_budget
    from webapp.manual_preview import current_evidence,failure
    stage='FOLLOWER_ACCOUNT'
    try:
        # Interactive current evidence uses P2, not discovery history priority,
        # and never borrows the P0/P1 reconciliation reserve. History analysis
        # is a separate read request; it is not the START safety contract.
        with manual_review_budget(),priority_scope('manual_preview.current_evidence',2):
            scoped,client=manual_leader_account(user['id'],profile)
            stage='LEADER_ACCOUNT'
            baseline=current_evidence(ManualCopyWorker(engine,reader),scoped,client,leader)
        cached=analysis_cache.get(leader)
        analysis=cached[1] if cached and 0<=time.time()-cached[0]<300 else None
        p=baseline['account']
        if p['completeness']!='COMPLETE': raise ValueError('ACCOUNT_EVIDENCE_UNAVAILABLE')
        pct=float(payload.allocation_pct or 0)
        if not math.isfinite(pct) or not 0<pct<=100: raise ValueError('INVALID_ALLOCATION')
        slots=(len(profile.get('leaders',[])[:3]) if profile.get('copy_enabled') else 0)+int(bool(profile.get('ai_slot_selected')))
        capital=p['sizing_capital']*max(0.,1-min(3,slots)/3)
        limit=capital*pct/100
        pending=bool(engine.journal.pending(scoped['address']))
        # No position attribution is invented during preview. Current execution
        # still rebuilds ownership and capacity through the canonical ledger.
        clear=not p['positions'] and not p['orders'] and not pending
        return dict(leader=leader,analysis=analysis,analysis_status='AVAILABLE' if analysis is not None else 'PENDING',
                    capital=baseline['leader']['capital'],leader_positions=baseline['leader']['positions'],
                    account_balance=p['equity'],allocatable_capital=capital,allocation_limit=limit,
                    committed=0. if clear else None,reserved=0. if clear else None,
                    available=min(limit,p['available_collateral']) if clear else None,
                    account_capacity=p['available_collateral'],network=scoped.get('network',client.network),
                    timestamp=baseline['leader']['exchange_ms'],account_timestamp=p['exchange_ms'],
                    received_ms=p['received_ms'],execution_authorized=False)
    except Exception as exc:
        if isinstance(exc,HTTPException) and exc.__cause__ is None:raise
        detail=failure(exc,getattr(exc,'preview_stage',stage))
        raise HTTPException(503,detail,headers={'Retry-After':'15'}) from None


@app.post('/api/manual-copy/analysis')
def manual_copy_analysis(payload: ManualCopyInput,x_telegram_init_data: str | None = Header(default=None)):
    """Separate optional quality read; cannot configure, start or sign."""
    from core.interactive_budget import manual_review_budget
    from core.hl_budget import priority_scope
    user=require_user(x_telegram_init_data)
    leader=(payload.leader or '').strip().lower()
    if not re.fullmatch(r'0x[0-9a-f]{40}',leader):raise HTTPException(400,'Invalid Hyperliquid leader address')
    with manual_review_budget(),priority_scope('manual_preview.history',2):
        return analyse_for_user(user['id'],leader)


def own_position(profile: dict, coin: str, dex: str) -> dict:
    account = profile.get("account")
    rows = safe_positions(account["address"])
    for row in rows:
        if engine._key(row.get("coin", ""), row.get("dex")) == engine._key(coin, dex):
            return row
    raise HTTPException(404, "Открытая позиция не найдена или уже закрыта.")


def order_id(response: dict):
    statuses = (((response.get("response") or {}).get("data") or {}).get("statuses") or [])
    for status in statuses:
        resting = status.get("resting") if isinstance(status, dict) else None
        if isinstance(resting, dict) and resting.get("oid") is not None:
            return resting["oid"]
    return "paper" if response.get("status") == "paper" else None


def manual_action(user_id, payload, action):
    _, profile = storage.profile(user_id)
    account = profile.get("account")
    if not account:
        raise HTTPException(400, "Hyperliquid account is not connected")
    try:
        with account_guard(ROOT, account["address"]):
            _, profile = storage.profile(user_id)
            if (profile.get("account") or {}).get("address") != account["address"]:
                raise HTTPException(409, "Account changed; reload")
            runtime = profile["runtime"]
            account_client = account_client_for(profile)
            canonical = lambda request: build_context(account_client,tenant=user_id,
                coin=request['coin'],dex=request['dex'],action=request['action'],source='manual',
                settings=settings,profile=profile,journal=engine.journal,request=request)
            service = ManualPositions(account_client, runtime,
                                      lambda: storage.update_runtime(user_id, runtime),
                                      canonical_context=canonical)
            if action == "stop":
                return service.set_stop_loss(payload.coin.strip(), payload.dex.strip().lower(), payload.price)
            if action == "delete":
                return service.delete_stop_loss(payload.coin.strip(), payload.dex.strip().lower())
            return service.close_position(payload.coin.strip(), payload.dex.strip().lower())
    except ManualActionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(409, "Another position action is in progress; retry after refresh") from exc
    except RuntimeError as exc:
        raise HTTPException(503, "Exchange state unavailable; no action confirmed") from exc


@app.post("/api/position/close")
def close_position(payload: PositionInput, x_telegram_init_data: str | None = Header(default=None)):
    return manual_action(require_user(x_telegram_init_data)["id"], payload, "close")


@app.post("/api/position/stop-loss")
def set_stop_loss(payload: StopLossInput, x_telegram_init_data: str | None = Header(default=None)):
    return manual_action(require_user(x_telegram_init_data)["id"], payload, "stop")


@app.delete("/api/position/stop-loss")
def delete_stop_loss(payload: PositionInput, x_telegram_init_data: str | None = Header(default=None)):
    return manual_action(require_user(x_telegram_init_data)["id"], payload, "delete")


@app.delete("/api/account")
def delete_account(payload: DeleteWalletInput | None = None,
                   x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (data, profile):
        account = profile.get("account")
        positions = safe_positions(account["address"]) if account else []
        if positions and not (payload and payload.confirm_open_positions):
            raise HTTPException(409, {"message": "На Hyperliquid-аккаунте есть открытые позиции. Подтвердите удаление ключа.",
                                      "positions": position_summary(positions)})
        profile["copy_enabled"] = False
        profile["account"] = None
        profile.setdefault("runtime", {})["managed"] = []
        data["profiles"][str(user["id"])] = profile
        storage.save(data)
    return {"ok": True}

def analyse_wallet(wallet: str) -> dict:
    fills = reader.fills_90d(wallet)
    balance = reader.balance(wallet)
    positions = reader.positions(wallet, True, True)
    report = analyzer.report(fills, balance); sim = simulator.simulate(fills)
    return {"wallet": short_address(wallet), "balance": balance, "rating": report.rating, "recommendation": report.recommendation,
            "trades": report.trades, "win_rate": report.win_rate, "net_pnl": report.net_pnl, "profit_factor": "∞" if report.profit_factor==float("inf") else report.profit_factor,
            "drawdown": report.max_drawdown, "score": sim.score, "eligible": sim.eligible,
            "methodology":"Closing-fill statistics; fees included, funding and intratrade equity drawdown excluded. Cost stress test is not a follower backtest or trading admission.",
            "positions": positions[:30]}

def analyse_for_user(user_id: int, wallet: str) -> dict:
    now = time.time()
    requests = analysis_requests[user_id]
    while requests and now - requests[0] > 600:
        requests.popleft()
    if len(requests) >= 15:
        raise HTTPException(429, "Лимит анализа: 15 запросов за 10 минут. Попробуйте позже.")
    cached = analysis_cache.get(wallet)
    if cached and now - cached[0] < 300:
        return cached[1]
    requests.append(now)
    report = analyse_wallet(wallet)
    analysis_cache[wallet] = (now, report)
    return report


@app.get("/api/analyse/{slot}")
def analyse(slot: int, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    _, profile = storage.profile(user["id"])
    wallets = profile.get("leaders") or []
    if not 1 <= slot <= len(wallets):
        raise HTTPException(404, "Кошелёк не настроен.")
    return analyse_for_user(user["id"], wallets[slot - 1])


@app.post("/api/analyse")
def analyse_any(payload: WalletInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    wallet = payload.address.strip().lower()
    if not ADDRESS.fullmatch(wallet):
        raise HTTPException(400, "Неверный адрес кошелька.")
    return analyse_for_user(user["id"], wallet)


@app.get("/api/account/report")
def account_report(days: int = 7, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    _, profile = storage.profile(user["id"])
    account = profile.get("account")
    if not account:
        raise HTTPException(400, "Hyperliquid-аккаунт не подключён.")
    days = min(90, max(1, int(days)))
    wallet = account["address"]
    balance = reader.balance(wallet); positions = reader.positions(wallet, True, True)
    fills = reader.fills_90d(wallet)
    cutoff = int((time.time() - days * 86400) * 1000)
    report = analyzer.report([x for x in fills if int(x.get("time", 0) or 0) >= cutoff], balance)
    pf = "∞" if report.profit_factor == float("inf") else round(report.profit_factor, 2)
    return {"days": days, "balance": balance, "exposure": sum(float(x.get("position_value", 0) or 0) for x in positions),
            "positions": positions, "pnl": report.net_pnl, "trades": report.trades, "win_rate": report.win_rate,
            "profit_factor": pf, "drawdown": report.max_drawdown,
            "methodology":"Realised fills less exchange fees; funding and unrealised equity path excluded."}

@app.get("/api/chart")
def chart(coin: str, interval: str = "15m", hours: int = 24, x_telegram_init_data: str | None = Header(default=None)):
    """Authenticated candle feed for the Mini App chart."""
    require_user(x_telegram_init_data)
    interval = interval if interval in {"1m", "5m", "15m", "1h", "4h", "1d"} else "15m"
    hours = min(24 * 30, max(1, int(hours)))
    coin = coin.strip()
    if not re.fullmatch(r"(?:[a-z0-9]+:)?[A-Za-z0-9._/-]{1,32}", coin):
        raise HTTPException(400, "Неверный инструмент.")
    _load_chart_cache()
    cache_key = (settings.hl_mode, coin, interval, str(hours))
    now = time.time()
    cached = chart_cache.get(cache_key)
    if cached and now - cached[0] < CHART_CACHE_TTL:
        candles = cached[1]
    else:
        end = int(now * 1000)
        start = end - hours * 3600 * 1000
        # Once a bounded history exists, request only the missing tail and
        # merge it with last-good rows.  This keeps Home cheap while retaining
        # the requested historical window.
        if cached and cached[1]:
            latest = max(int(row.get("t", 0)) for row in cached[1])
            if latest > start:
                start = latest
        from core.hl_budget import priority_scope
        with priority_scope("product.chart.initial", 2):
            raw = reader._info({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                               "startTime": start, "endTime": end}})
        fresh = [{"t": int(x["t"]), "o": float(x["o"]), "h": float(x["h"]), "l": float(x["l"]), "c": float(x["c"])} for x in raw]
        merged = {int(row["t"]): row for row in (cached[1] if cached else [])}
        merged.update({int(row["t"]): row for row in fresh})
        candles = [merged[key] for key in sorted(merged)][-500:]
        chart_cache[cache_key] = (now, candles)
        _persist_chart_cache()
    return {"coin": coin, "interval": interval, "hours": hours, "candles": candles}


def _load_chart_cache():
    """Load only bounded, finite public candle history from disk once per process."""
    global _chart_cache_loaded
    if _chart_cache_loaded:
        return
    _chart_cache_loaded = True
    try:
        with open(CHART_CACHE_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        entries = payload.get("entries", []) if isinstance(payload, dict) else []
        for item in entries[-256:]:
            key = item.get("key")
            rows = item.get("candles")
            received = float(item.get("received", 0))
            if not isinstance(key, list) or len(key) != 4 or not isinstance(rows, list) or not math.isfinite(received):
                continue
            clean = []
            for row in rows[-500:]:
                try:
                    parsed = {"t": int(row["t"]), "o": float(row["o"]), "h": float(row["h"]),
                              "l": float(row["l"]), "c": float(row["c"])}
                    if parsed["t"] > 0 and all(math.isfinite(parsed[k]) and parsed[k] > 0 for k in ("o", "h", "l", "c")):
                        clean.append(parsed)
                except (KeyError, TypeError, ValueError):
                    continue
            if clean:
                chart_cache[tuple(str(part) for part in key)] = (received, clean)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return


def _persist_chart_cache():
    entries = []
    for key, (received, rows) in list(chart_cache.items())[-256:]:
        entries.append({"key": list(key), "received": received, "candles": rows[-500:]})
    payload = {"version": 1, "entries": entries}
    try:
        os.makedirs(os.path.dirname(CHART_CACHE_PATH), exist_ok=True)
        tmp = CHART_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), allow_nan=False)
        os.replace(tmp, CHART_CACHE_PATH)
    except OSError:
        # Cache persistence is best-effort; the live read remains authoritative.
        return


@app.get("/api/price")
def price(coin: str, dex: str = "", x_telegram_init_data: str | None = Header(default=None),
          network: Literal["MAINNET", "TESTNET"] | None = None):
    """Small authenticated live-price feed for the chart marker."""
    require_user(x_telegram_init_data)
    if network is not None and network != settings.hl_mode:
        raise HTTPException(409, "MARKET_NETWORK_MISMATCH")
    coin, dex = coin.strip(), dex.strip().lower()
    if dex not in {"", "xyz"} or not re.fullmatch(r"[A-Za-z0-9._/-]{1,32}", coin):
        raise HTTPException(400, "Неверный инструмент.")
    key, now = (coin, dex), time.time()
    cached = price_cache.get(key)
    if cached and now - cached[0] < 1.5:
        value = cached[1]
    else:
        # A current mark is a lightweight, read-only product datum. Give it
        # the interactive evidence priority so a queued candle/history scan
        # cannot make every open position render without a price. This does
        # not bypass the shared host budget or alter any trading admission.
        with priority_scope("product.price", 1):
            value = float(reader.mid(coin, dex))
        price_cache[key] = (now, value)
    return {"coin": coin, "dex": dex or None, "price": value, "time": int(now * 1000)}

@app.get("/api/markets")
def markets(x_telegram_init_data: str | None = Header(default=None)):
    """Searchable list of currently listed Hyperliquid perpetual markets."""
    global markets_cache
    require_user(x_telegram_init_data)
    now = time.time()
    if markets_cache and now - markets_cache[0] < 300:
        return {"markets": markets_cache[1]}
    names = set()
    for dex in ("", "xyz"):
        try:
            meta = reader._info({"type": "meta", **({"dex": dex} if dex else {})})
            for item in meta.get("universe", []):
                name = str(item.get("name") or "")
                if name:
                    names.add(name if ":" in name or not dex else f"{dex}:{name}")
        except Exception:
            continue
    markets_cache = (now, sorted(names, key=str.lower))
    return {"markets": markets_cache[1]}

@app.get("/api/ai")
def ai_summary(x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    _, profile = storage.profile(user["id"])
    result = ai_assistant.summary(user["id"])
    result["global_training"] = ai_assistant.global_training_stats()
    result["readiness"] = ai_assistant.readiness()
    result["reviews"] = ai_review.list(user["id"])
    result["research"] = ai_review.research.summary(user["id"])
    runtime=profile["runtime"]
    result["holds"] = dict(runtime.get("ai_hold_keys", {}))
    for key in runtime.get("manual_hold_keys", []):
        result["holds"][key] = {"manual":True}
    result["policy"] = profile.get("ai_review_policy") or ReviewPolicy().as_dict()
    result["modes"] = ai_modes.summary(user["id"], profile)
    result["learning"] = ai_learning.summary()
    result["user_orders"] = ai_user_orders.summary(user["id"], profile, int(time.time() * 1000))
    result["position_actions"] = ai_position_actions.summary(user["id"], profile, int(time.time() * 1000))
    return result


class AiModeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["trader", "rescue"]
    enabled: StrictBool


@app.post("/api/ai/modes")
def set_ai_mode(payload: AiModeInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (data, profile):
        try:
            ai_modes.set_enabled(profile, payload.mode, payload.enabled)
        except ValueError as exc:
            raise HTTPException(409,{"code":"AI_MODE_UNAVAILABLE"}) from exc
        data["profiles"][str(user["id"])]=profile
        storage.save(data)
    return {"ok":True,"modes":ai_modes.summary(user["id"],profile)}


class AiDecisionInput(BaseModel):
    confirm: bool


class AiOrderPrepareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AiOrderDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool


def public_account_for(profile):
    """No signing key is ever read for a preview or rejection."""
    account = profile.get("account")
    return HyperliquidAccount(account["address"], None, settings.hl_mode) if account else None


@app.post("/api/ai/orders/prepare")
def ai_order_prepare(payload: AiOrderPrepareInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (_, profile):
        try:
            return {"user_orders": ai_user_orders.prepare(user["id"], profile,
                public_account_for(profile), reader, ai_learning.summary(), int(time.time() * 1000))}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, {"code": "AI_ORDER_PREVIEW_UNAVAILABLE"}) from exc


@app.post("/api/ai/orders/{proposal_id}/decision")
def ai_order_decision(proposal_id: str, payload: AiOrderDecisionInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (data, profile):
        def persist_runtime():
            data["profiles"][str(user["id"])] = profile
            storage.save(data)
        try:
            # Only the service's fresh checks and durable one-shot claim may
            # invoke this signing factory. Decline has no exchange dependency.
            result = ai_user_orders.decide(user["id"], proposal_id, payload.confirm,
                profile, public_account_for(profile) if payload.confirm else None,
                lambda: account_client_for(profile), persist_runtime, int(time.time() * 1000),
                canonical_context=(lambda p: build_context(account_client_for(profile), tenant=user['id'], coin=p['coin'],
                    dex=p.get('dex',''), action='OPEN', source='ai',settings=settings,profile=profile,
                    journal=engine.journal,request=p)) if payload.confirm else None)
            return {"result": result,
                "user_orders": ai_user_orders.summary(user["id"], profile, int(time.time() * 1000))}
        except HTTPException:
            raise
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, {"code": "AI_ORDER_UNAVAILABLE"}) from exc
        except Exception as exc:
            # A network/commit failure is NOT permission to repeat the order.
            # The persisted SUBMITTING/UNKNOWN state must be refreshed first.
            raise HTTPException(503, {"code": "AI_ORDER_STATUS_CHECK_REQUIRED"}) from exc


@app.post("/api/ai/positions/prepare")
def ai_position_prepare(payload: AiOrderPrepareInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (_, profile):
        try:
            return {"position_actions": ai_position_actions.prepare(user["id"], profile,
                public_account_for(profile), reader, int(time.time() * 1000), force_refresh=True)}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, {"code": "AI_POSITION_PREVIEW_UNAVAILABLE"}) from exc


@app.post("/api/ai/positions/{proposal_id}/decision")
def ai_position_decision(proposal_id: str, payload: AiOrderDecisionInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (data, profile):
        def persist_runtime():
            data["profiles"][str(user["id"])] = profile
            storage.save(data)
        try:
            result = ai_position_actions.decide(user["id"], proposal_id, payload.confirm, profile,
                public_account_for(profile) if payload.confirm else None,
                lambda: account_client_for(profile), persist_runtime, int(time.time() * 1000),
                canonical_context=(lambda p: build_context(account_client_for(profile), tenant=user['id'], coin=p['coin'],
                    dex=p.get('dex',''), action=p['action'], source=p.get('source_wallet','ai'),settings=settings,
                    profile=profile,journal=engine.journal,request=p)) if payload.confirm else None)
            return {"result": result, "position_actions": ai_position_actions.summary(user["id"], profile, int(time.time() * 1000))}
        except HTTPException:
            raise
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, {"code": "AI_POSITION_ACTION_UNAVAILABLE"}) from exc
        except Exception as exc:
            raise HTTPException(503, {"code": "AI_POSITION_STATUS_CHECK_REQUIRED"}) from exc


class AiPositionResumeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: str


@app.post("/api/ai/positions/resume")
def ai_position_resume(payload: AiPositionResumeInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    with mutation_profile(user["id"]) as (data, profile):
        def persist_runtime():
            data["profiles"][str(user["id"])] = profile
            storage.save(data)
        try:
            result = ai_position_actions.release(user["id"], payload.market, profile,
                public_account_for(profile), persist_runtime, int(time.time() * 1000))
            return {"result": result, "position_actions": ai_position_actions.summary(user["id"], profile, int(time.time() * 1000))}
        except (ValueError, OSError, KeyError) as exc:
            raise HTTPException(409, {"code": "AI_POSITION_RECONCILIATION_REQUIRED"}) from exc


class AiResumeInput(BaseModel):
    market: str


@app.post("/api/ai/reviews/{proposal_id}/decision")
def ai_decision(proposal_id: str, payload: AiDecisionInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    try:
        return ai_review.decide(user["id"], proposal_id, payload.confirm, storage, account_client_for)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/ai/resume-copy")
def ai_resume(payload: AiResumeInput, x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    try:
        ai_review.resume(user["id"], payload.market, storage)
        return {"ok": True}
    except (ValueError, OSError, StateConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
