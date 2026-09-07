import asyncio
import html
import os
import re
import sys
import time
import uuid
import hashlib
import requests
from datetime import datetime

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.types import KeyboardButtonWebView

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from core.settings import load
from core.storage import Storage
from core.hyperliquid import HyperliquidReader
from core.trade_analyzer import TradeAnalyzer
from core.follower_simulator import FollowerSimulator
from core.analysis_metrics import persisted_profit_factor
from core.trading_engine import CopyEngine
from core.ai_assistant import AiAssistant
from core.ai_modes import AiModes
from core.ai_learning_worker import AiLearningWorker
from core.ai_review import AiReview, account_guard
from core.ai_review_text import review_text
from core.ai_position_actions import AiPositionActions
from core.ai_position_observer import AiPositionObserver, position_notice, position_app_url
from core.ai_user_orders import AiUserOrders
from core.ai_entry_observer import AiEntryObserver, entry_notice
from core.profile_mutation import save_profile_guarded, legacy_trading_callback
from integrations.hyperliquid import HyperliquidAccount, verify_account_control

S = load()
# Versioned URL makes Telegram reopen the current Mini App build after deployments.
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://wallethunter-hl.duckdns.org/?v=8")
if not S.telegram_api_id or not S.telegram_api_hash or not S.telegram_bot_token:
    raise RuntimeError("Telegram credentials missing")
if not S.master_key: raise RuntimeError("MASTER_KEY is required")
store, reader, analyzer, simulator = Storage(ROOT, S.master_key), HyperliquidReader(S.hl_mode), TradeAnalyzer(), FollowerSimulator()
engine = CopyEngine(reader, store, S)
ai_assistant = AiAssistant(ROOT)
ai_modes = AiModes(ROOT)
ai_learning = AiLearningWorker(ROOT)
ai_review = AiReview(ROOT)
ai_position_actions = AiPositionActions(ROOT)
ai_user_orders = AiUserOrders(ROOT)
client = TelegramClient(os.path.join(os.path.dirname(__file__), "bot.session"), S.telegram_api_id, S.telegram_api_hash)
waiting, clients, public_clients = {}, {}, {}
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")

def esc(value): return html.escape(str(value))
def profile(uid): return store.profile(uid)
def short_wallet(value):
    """Recognisable address without consuming a phone screen."""
    value = str(value or "")
    return value if len(value) <= 12 else f"{value[:6]}…{value[-4:]}"
def leader_is_enabled(p, wallet): return bool((p.get("leader_enabled") or {}).get(wallet, True))
def active_leaders(p): return [wallet for wallet in p.get("leaders", []) if leader_is_enabled(p, wallet)]
def menu_button():
    return [[Button.text("🚀 ОТКРЫТЬ ПРИЛОЖЕНИЕ", resize=True, persistent=True, single_use=False),
             Button.text("🧹 ОЧИСТИТЬ УВЕДОМЛЕНИЯ", resize=True, persistent=True, single_use=False)]]
def account_client(uid, p):
    account = p.get("account")
    if not account: return None
    cached = clients.get(str(uid))
    key_version = hashlib.sha256(account["private_key"].encode()).hexdigest()
    if not cached or cached.address.lower() != account["address"].lower() or getattr(cached, "key_version", None) != key_version:
        cached = HyperliquidAccount(account["address"], store.decrypt(account["private_key"]), S.hl_mode)
        cached.key_version = key_version
        clients[str(uid)] = cached
    return cached

def public_account_reader(p):
    """AI observers have no signing client or decrypted trading key."""
    address=p["account"]["address"].strip().lower()
    if address not in public_clients:
        public_clients[address]=HyperliquidAccount(address,None,S.hl_mode)
    return public_clients[address]

def tick_paper_trader(uid):
    # The shared profile lock prevents one final virtual action racing an
    # account/slot change or pause. This never acquires a signing client.
    with account_guard(ROOT,f"telegram-profile:{int(uid)}"):
        _,p=store.profile(uid)
        return ai_modes.tick(uid,p,reader)


async def ai_position_watcher():
    # This worker can only prepare forms and notify. There is deliberately no
    # signing factory or financial decision callback in this background path.
    observer = AiPositionObserver(ROOT, store, ai_position_actions, public_account_reader, reader)
    await asyncio.sleep(7)
    while True:
        next_check = 60
        try:
            for uid_text, saved in store.load().get("profiles", {}).items():
                if not saved.get("account") or not saved.get("ai_review_enabled", True): continue
                uid = int(uid_text)
                try:
                    check = await asyncio.to_thread(observer.prepare, uid)
                    print("[AI POSITION CHECK]", (check or {}).get("status"), (check or {}).get("reason"))
                    batches, current = await asyncio.to_thread(observer.notices, uid)
                    for rows in batches[:3]:
                        en = current.get("language") == "en"
                        message = await client.send_message(uid, position_notice(rows, en), parse_mode=None,
                            buttons=[[KeyboardButtonWebView("Review in APP" if en else "Рассмотреть в APP", position_app_url(WEBAPP_URL))]])
                        for row in rows: ai_position_actions.mark_notified(uid, row["id"])
                        _, fresh = profile(uid)
                        remember_notification(uid, fresh, message)
                        _, fresh = profile(uid)
                        journal = list(fresh["runtime"].get("journal") or [])
                        journal.append({"time":int(time.time()*1000),"action":"AI_POSITION_REVIEW",
                                        "coin":rows[0]["payload"]["coin"]})
                        fresh["runtime"]["journal"] = journal[-200:]
                        store.update_runtime(uid, fresh["runtime"])
                except BlockingIOError:
                    next_check = 7  # Avoid phase-locking with 60s paper checks.
                except OSError as exc:
                    next_check = 15
                    print("[AI POSITION IO]", type(exc).__name__)
                except Exception as exc:
                    print("[AI POSITION REVIEW]", type(exc).__name__)
        except Exception as exc:
            print("[AI POSITION LOOP]", type(exc).__name__)
        await asyncio.sleep(next_check)

async def ai_entry_watcher():
    # Public signal -> immutable form -> notification only. Signing happens
    # solely in the authenticated APP decision endpoint after user confirmation.
    observer = AiEntryObserver(ROOT, store, ai_user_orders, public_account_reader, reader, ai_learning)
    await asyncio.sleep(13)
    while True:
        next_check = 60
        try:
            for uid_text, saved in store.load().get("profiles", {}).items():
                if not saved.get("account") or not saved.get("ai_trader_enabled", False):continue
                uid = int(uid_text)
                try:
                    check = await asyncio.to_thread(observer.prepare, uid)
                    print("[AI ENTRY CHECK]", (check or {}).get("status"), (check or {}).get("reason"))
                    rows, current = await asyncio.to_thread(observer.notices, uid)
                    for row in rows[:1]:
                        if not await asyncio.to_thread(observer.claim_notice, uid, row["id"]):continue
                        en = current.get("language") == "en"
                        message = await client.send_message(uid, entry_notice(row, en), parse_mode=None,
                            buttons=[[KeyboardButtonWebView("Review in APP" if en else "Рассмотреть в APP", position_app_url(WEBAPP_URL))]])
                        _, fresh = profile(uid)
                        remember_notification(uid, fresh, message)
                except BlockingIOError:
                    next_check = 11
                except OSError as exc:
                    next_check = 15
                    print("[AI ENTRY IO]", type(exc).__name__)
                except Exception as exc:
                    print("[AI ENTRY REVIEW]", type(exc).__name__)
        except Exception as exc:
            print("[AI ENTRY LOOP]", type(exc).__name__)
        await asyncio.sleep(next_check)

async def ai_trader_watcher():
    while True:
        try:
            for uid_text,p in store.load().get("profiles",{}).items():
                if not p.get("account") or not p.get("ai_trader_enabled",False):continue
                try:
                    await asyncio.to_thread(tick_paper_trader,int(uid_text))
                except OSError:
                    pass  # A user action holds the profile; retry next cycle.
                except Exception as exc:
                    print("[AI PAPER TRADER]",uid_text,type(exc).__name__)
        except Exception as exc:
            print("[AI PAPER LOOP]",type(exc).__name__)
        await asyncio.sleep(60)

async def balance(uid, p):
    try:
        c = account_client(uid, p)
        return await asyncio.to_thread(c.balance) if c else 0.0
    except Exception as exc:
        raise RuntimeError("Account balance unavailable") from exc


async def ai_learning_watcher():
    # One PUBLIC model for the application, not one duplicate per user.
    # No signing client, account address, balance or trading settings are used.
    public_learning_reader = HyperliquidReader(S.hl_mode)
    while True:
        try:
            await asyncio.to_thread(ai_learning.cycle, public_learning_reader)
        except Exception as exc:
            print('[AI LEARNING]', type(exc).__name__)
        await asyncio.sleep(60)

def dashboard(uid, p, bal):
    account = p.get("account")
    leaders = p.get("leaders") or []
    # Every slot keeps one third, including empty, paused and research AI slots.
    runtime = p.get("runtime") or {}
    risk = engine.risk(p)
    last_sync = runtime.get("last_sync_ms", 0)
    sync_text = datetime.fromtimestamp(last_sync / 1000).strftime("%H:%M:%S") if last_sync else "ещё не было"
    daily = float(runtime.get("daily_pnl", 0) or 0)
    limit = float(runtime.get("daily_limit", 0) or 0)
    block = runtime.get("blocked_reason")
    last_error = runtime.get("last_error")
    block_text = f"\n⚠️ <b>{esc(block)}</b>" if block else ""
    if not block and last_error: block_text = f"\n⚠️ {esc(last_error)}"
    target = (f"<b>⚡ Hyperliquid</b> · <code>{esc(short_wallet(account['address']))}</code>\n"
              f"Баланс <b>${bal:,.2f}</b>" if account else "<i>Не подключён</i>")
    wallet_indicators = []
    for index in range(3):
        if index == 2 and p.get("ai_slot_selected"):
            wallet_indicators.append("🧠 3 · ИИ исследование")
        elif index >= len(leaders): wallet_indicators.append(f"⚪ {index + 1}")
        else: wallet_indicators.append(f"{'🟢' if leader_is_enabled(p, leaders[index]) else '⏸'} {index + 1}")
    live_status = "🟢 LIVE" if S.auto_trading else "🟡 PAPER"
    copy_status = "🟢 ON" if p.get("copy_enabled") else "⚪ OFF"
    return (f"<b>⚡ WALLET HUNTER</b> · <b>{live_status}</b>\n"
            f"<i>Синхронизация {sync_text}</i>{block_text}\n\n"
            f"<b>🎯 TARGET</b>\n{target}\n\n"
            f"<b>👛 КОШЕЛЬКИ</b>\n{'  ·  '.join(wallet_indicators)}\n"
            f"Бюджет: <b>1/3</b> на каждый слот · Макс: <b>{float(p.get('max_leverage') or S.max_leverage):g}x</b>\n\n"
            f"{copy_status} Copy · 🪙 {'ON' if p.get('crypto_enabled') else 'HOLD'} · 📈 {'ON' if p.get('stocks_enabled') else 'HOLD'}\n"
            f"🛡 {risk['label']} · PnL <b>${daily:+,.0f}</b> / -${limit:,.0f} · 🎛 {engine.strategy(p)['label']}")

def dashboard_buttons(p):
    account_label = "⚡ Hyperliquid" if p.get("account") else "➕ Hyperliquid"
    copy_label = "⏸ Пауза copy" if p.get("copy_enabled") else "▶️ Запустить copy"
    return [
        [Button.inline(account_label, b"account"), Button.inline("👛 Кошельки", b"leaders")],
        [Button.inline(copy_label, b"copy"), Button.inline("⚙️ Плечо", b"leverage")],
        [Button.inline(f"🪙 Futures {'🟢' if p.get('crypto_enabled') else '⚪'}", b"crypto"), Button.inline(f"📈 XYZ {'🟢' if p.get('stocks_enabled') else '⚪'}", b"stocks")],
        [Button.inline("🧪 Анализ", b"analyse"), Button.inline("📊 Аккаунт", b"stats"), Button.inline("🛡 Риск", b"risk")],
        [Button.inline("🎛 Стратегия", b"strategy"), Button.inline("📜 Журнал", b"journal"), Button.inline("🧾 Отчёт", b"reports")],
        [Button.inline("🚨 Стоп", b"emergency"), Button.inline(f"🔔 {'ON' if p.get('notifications') else 'OFF'}", b"notify"), Button.inline("ℹ️ Бот", b"about")],
    ]

async def show_leaders(event, p):
    rows = []
    for i, wallet in enumerate(p["leaders"]):
        enabled = leader_is_enabled(p, wallet)
        rows.append([Button.inline(("⏸ Выключить" if enabled else "▶️ Включить") + f" #{i+1}", f"toggleleader:{i}".encode()),
                     Button.inline(f"🗑 Удалить #{i+1}", f"delleader:{i}".encode())])
    if len(p["leaders"]) < 3: rows.append([Button.inline("➕ Добавить кошелёк", b"addleader")])
    rows.append([Button.inline("◀️ Назад", b"menu")])
    leaders = "\n".join(f"<b>#{i+1}</b> {'🟢 Активен' if leader_is_enabled(p, w) else '⏸ Пауза'} · <code>{esc(short_wallet(w))}</code>" for i, w in enumerate(p["leaders"])) or "<i>Пока пусто.</i>"
    await event.edit(f"<b>👛 МОИ КОШЕЛЬКИ</b>\n\n{leaders}\n\nПауза прекращает новые копирования и изменения от выбранного кошелька. Уже открытые позиции сохраняются до вашего отдельного решения.", buttons=rows, parse_mode="html")

async def show_menu(event, edit=False):
    text = ("<b>⚡ WALLET HUNTER</b>\n"
            "<i>CRYPTO COPY TERMINAL · HYPERLIQUID</i>\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "В этом чате приходят только важные уведомления: исполнение, риск и ошибки.")
    buttons = [[Button.inline("🧹 ОЧИСТИТЬ УВЕДОМЛЕНИЯ", b"clear_journal")]]
    if edit:
        try: await event.edit(text, buttons=buttons, parse_mode="html")
        except MessageNotModifiedError: pass
    else:
        message = await event.respond(text, buttons=buttons, parse_mode="html")
        try:
            _, owner_profile = profile(event.sender_id)
            runtime = owner_profile.setdefault("runtime", {})
            runtime["controller_message_id"] = int(message.id)
            store.update_runtime(event.sender_id, runtime)
            remember_chat_message(event.sender_id, owner_profile, message)
        except Exception as exc: print("[SAVE MENU]", event.sender_id, type(exc).__name__)
        try: await client.pin_message(event.sender_id, message, notify=False)
        except Exception as exc: print("[PIN MENU]", event.sender_id, type(exc).__name__)

async def save_profile(uid, p):
    save_profile_guarded(store, uid, p)


def remember_notification(uid, p, message):
    """Keep only bot-authored notification IDs; never touch user messages."""
    remember_chat_message(uid, p, message)
    if not message:
        return
    runtime = p.setdefault("runtime", {})
    ids = [int(item) for item in (runtime.get("notification_message_ids") or []) if str(item).isdigit()]
    ids.append(int(message.id))
    runtime["notification_message_ids"] = ids[-300:]
    store.update_runtime(uid, runtime)


def remember_chat_message(uid, p, message):
    """Store IDs as they arrive; Telegram bots cannot read old private history."""
    if not message:
        return
    runtime = p.setdefault("runtime", {})
    ids = [int(item) for item in (runtime.get("chat_message_ids") or []) if str(item).isdigit()]
    message_id = int(message.id)
    if message_id not in ids:
        ids.append(message_id)
    runtime["chat_message_ids"] = ids[-1000:]
    store.update_runtime(uid, runtime)


async def clear_private_chat(uid, p, keep_message_id=None):
    """Delete known private-chat messages, keeping the pinned controller."""
    runtime = p.setdefault("runtime", {})
    keep = int(runtime.get("controller_message_id") or keep_message_id or 0)
    known = list(runtime.get("chat_message_ids") or []) + list(runtime.get("notification_message_ids") or [])
    candidates = sorted({int(item) for item in known if str(item).isdigit() and int(item) != keep})
    deleted = 0
    failed = []
    for start in range(0, len(candidates), 100):
        batch = candidates[start:start + 100]
        try:
            await client.delete_messages(uid, batch)
            deleted += len(batch)
        except Exception as exc:
            failed.extend(batch)
            print("[CLEAR CHAT]", uid, type(exc).__name__)
    runtime["journal"], runtime["last_error"] = [], ""
    runtime["pending_notifications"] = []
    if runtime.get("paper_runtime"):
        runtime["paper_runtime"]["journal"] = []
        runtime["paper_runtime"]["pending_notifications"] = []
    runtime["notification_message_ids"] = [i for i in runtime.get("notification_message_ids", []) if int(i) in failed]
    runtime["journal_cleared_at"] = int(time.time() * 1000)
    runtime["chat_message_ids"] = sorted(set(failed + ([keep] if keep else [])))
    await save_profile(uid, p)
    return len(candidates), deleted

def notification_labels(result, english: bool):
    """Keep exchange symbols intact, but never expose engine codes to the user."""
    action_ru = {
        "WAIT_PRICE": "ОЖИДАНИЕ ЦЕНЫ", "WAIT_SPREAD": "ОЖИДАНИЕ СПРЕДА",
        "WAIT_FUNDING": "ОЖИДАНИЕ FUNDING", "OPEN": "ОТКРЫТИЕ", "ADD": "ДОБОР",
        "REDUCE": "СОКРАЩЕНИЕ", "CLOSE": "ЗАКРЫТИЕ", "REVERSE": "РАЗВОРОТ",
        "ERROR": "ОШИБКА",
        "FULL_CLOSE":"ЗАКРЫТИЕ", "PARTIAL_CLOSE":"СОКРАЩЕНИЕ", "PARTIAL_FILL":"ЧАСТИЧНОЕ ИСПОЛНЕНИЕ",
        "RISK_STOP":"НОВЫЕ ВХОДЫ НА ПАУЗЕ", "RISK_CLOSE":"ЗАКРЫТИЕ ПО ЛИМИТУ",
        "MAX_POSITIONS":"ЛИМИТ ПОЗИЦИЙ", "EXECUTION_BLOCK":"ВХОД НЕДОСТУПЕН",
        "FUNDING_BLOCK":"НЕБЛАГОПРИЯТНЫЙ ФАНДИНГ", "ORDERS_CANCELLED":"ЗАЯВКИ ОТМЕНЕНЫ",
        "MANUAL_POSITION":"ПОЗИЦИЯ ВНЕ УПРАВЛЕНИЯ БОТА",
    }
    action_en = {
        "WAIT_PRICE": "WAIT FOR PRICE", "WAIT_SPREAD": "WAIT FOR SPREAD",
        "WAIT_FUNDING": "WAIT FOR FUNDING", "OPEN": "OPEN", "ADD": "ADD",
        "REDUCE": "REDUCE", "CLOSE": "CLOSE", "REVERSE": "REVERSE", "ERROR": "ERROR",
        "FULL_CLOSE":"CLOSED", "PARTIAL_CLOSE":"REDUCED", "PARTIAL_FILL":"PARTIAL FILL",
        "RISK_STOP":"NEW ENTRIES PAUSED", "RISK_CLOSE":"RISK CLOSE",
        "MAX_POSITIONS":"POSITION LIMIT", "EXECUTION_BLOCK":"ENTRY UNAVAILABLE",
        "FUNDING_BLOCK":"ADVERSE FUNDING", "ORDERS_CANCELLED":"ORDERS CANCELLED",
        "MANUAL_POSITION":"UNMANAGED POSITION",
    }
    side = {"LONG": "LONG" if english else "ЛОНГ", "SHORT": "SHORT" if english else "ШОРТ"}.get(result.side, result.side)
    market = {"CRYPTO": "CRYPTO" if english else "КРИПТО", "STOCKS": "STOCKS" if english else "АКЦИИ", "XYZ": "XYZ"}.get(result.market_type, result.market_type)
    action = (action_en if english else action_ru).get(result.action, result.action.replace("_", " "))
    status = ("PAPER" if result.paper else "LIVE") if english else ("ТЕСТ" if result.paper else "РЕАЛЬНЫЙ")
    error = str(result.error or "")
    if not english:
        if error.startswith("Waiting for price: deviation "):
            error = error.replace("Waiting for price: deviation ", "Ожидание цены: отклонение ", 1).replace(" exceeds ", " превышает ", 1)
        elif error.startswith("Waiting for spread: "):
            error = error.replace("Waiting for spread: ", "Ожидание спреда: ", 1).replace(" exceeds ", " превышает ", 1)
        elif error.startswith("Daily loss limit reached: "):
            error = error.replace("Daily loss limit reached: ", "Достигнут дневной лимит убытка: ", 1)
        elif error.startswith("Daily PnL unavailable:"):
            error = "История дневного PnL недоступна. Новый риск на паузе; выходы вслед за кошельком разрешены."
        elif error.startswith("Exchange reports "):
            error = "Частичное исполнение: показан фактический размер позиции на бирже."
        elif error and not re.search(r"[А-Яа-яЁё]", error):
            error = "Условия операции не подтверждены. Подробности сохранены в техническом журнале."
    return action, side, market, status, error


async def notify(uid, p, result, account):
    if not p.get("notifications") or result.action in {"NO_CHANGE", "LEVERAGE_UPDATE"}: return
    english = p.get("language") == "en"
    if result.action == "MIN_NOTIONAL_SKIP":
        body = (f"⚪ <b>{'Trade not placed' if english else 'Сделка не совершена'}</b> · <b>{esc(result.coin)}</b>\n"
                f"{'Required position change' if english else 'Требуемое изменение позиции'}: <b>${result.target_notional:,.2f}</b> · {'Hyperliquid minimum' if english else 'минимум Hyperliquid'}: <b>$10.00</b>\n"
                + ("No order was sent. Repeated entry checks remain silent until execution or a new position episode." if english else "Бот не отправлял ордер. Повторные проверки не вызывают уведомлений до исполнения или нового входа кошелька."))
        remember_notification(uid, p, await client.send_message(uid, body, parse_mode="html"))
        return
    icon = "🟢" if result.side == "LONG" else "🔴" if result.side == "SHORT" else "⚪"
    action, side, market, status, error = notification_labels(result, english)
    body = (f"{icon} <b>{esc(action)}</b> · <b>{esc(result.coin)}</b>\n"
            f"{esc(side)} · {esc(market)} · {result.leverage:g}x\n"
            f"{'Target' if english else 'Цель'}: <b>${result.target_notional:,.2f}</b> | {'Filled size' if english else 'Факт. размер'}: <b>{result.size:g}</b>\n"
            f"{'Mode' if english else 'Режим'}: <b>{status}</b>")
    if error: body += f"\n⚠️ {esc(error)}"
    remember_notification(uid, p, await client.send_message(uid, body, parse_mode="html"))

@client.on(events.NewMessage(pattern=r"^/start$"))
async def start(event): await show_menu(event)

@client.on(events.NewMessage)
async def message(event):
    text = (event.raw_text or "").strip()
    _, incoming_profile = profile(event.sender_id)
    remember_chat_message(event.sender_id, incoming_profile, event.message)
    if text in {"☰ МОЙ COPY TRADER", "🚀 ОТКРЫТЬ ПРИЛОЖЕНИЕ"}: await show_menu(event); return
    if text == "🧹 ОЧИСТИТЬ УВЕДОМЛЕНИЯ":
        _, p = profile(event.sender_id)
        _, deleted = await clear_private_chat(event.sender_id, p)
        confirmation = await event.respond(f"🧹 Чат очищен. Удалено сообщений: {deleted}.", buttons=menu_button())
        remember_chat_message(event.sender_id, p, confirmation)
        return
    if not text or text.startswith("/"): return
    task = waiting.pop(event.sender_id, None)
    if not task: return
    _, p = profile(event.sender_id)
    try:
        if task == "account":
            parts = [x.strip() for x in text.split("|")]
            if len(parts) != 3 or not ADDRESS.fullmatch(parts[1]): raise ValueError("Формат: Название | 0xACCOUNT_ADDRESS | API_PRIVATE_KEY")
            control = await asyncio.to_thread(verify_account_control, parts[1], parts[2], reader._info)
            p["account"] = {"id": f"a{uuid.uuid4().hex[:12]}", "name": parts[0][:40] or "My Hyperliquid", "address": parts[1].lower(), "private_key": store.encrypt(parts[2]),
                            "control": dict(control, verified_network=S.hl_mode, verified_ms=int(time.time()*1000))}
            p["copy_enabled"] = False; clients.pop(str(event.sender_id), None)
            await save_profile(event.sender_id, p)
            await event.respond("✅ Hyperliquid-аккаунт сохранён. Добавьте кошельки и включите копирование.", buttons=menu_button())
        elif task == "leader":
            if not ADDRESS.fullmatch(text): raise ValueError("Нужен адрес вида 0x + 40 hex-символов")
            wallet = text.lower()
            if wallet in p["leaders"]: raise ValueError("Этот кошелёк уже добавлен")
            if len(p["leaders"]) >= 3: raise ValueError("Можно добавить максимум 3 кошелька")
            p["leaders"].append(wallet); p.setdefault("leader_enabled", {})[wallet] = True; await save_profile(event.sender_id, p)
            await event.respond(f"✅ Кошелёк #{len(p['leaders'])} добавлен.", buttons=menu_button())
        elif task == "analyse_wallet":
            if not ADDRESS.fullmatch(text): raise ValueError("Нужен адрес вида 0x + 40 hex-символов")
            await analyse(event, text.lower(), event.sender_id, p, edit=False)
    except Exception as exc:
        await event.respond(f"❌ {esc(exc)}", buttons=menu_button(), parse_mode="html")

@client.on(events.CallbackQuery)
async def callback(event):
    uid, key = event.sender_id, event.data.decode()
    _, p = profile(uid)
    if key.startswith("air:"):
        _, answer, proposal_id = key.split(":", 2)
        if answer not in {"yes", "no"}: return
        await event.answer("Проверяю…" if p.get("language") != "en" else "Checking…")
        try:
            result = await asyncio.to_thread(ai_review.decide, uid, proposal_id, answer == "yes", store,
                                             lambda current: account_client(uid, current))
            await event.edit(("AI · " + result["status"] + "\n" +
                              ("Анализ продолжается. Статус и пауза копирования доступны во вкладке AI." if p.get("language") != "en" else
                               "Analysis continues. View status and copy hold in the AI tab.")), buttons=None)
        except Exception as exc:
            await event.respond("AI: " + str(exc))
        return
    if legacy_trading_callback(key):
        await event.answer("Trading controls moved to the app: open APP and confirm the action there."
                           if p.get("language") == "en" else
                           "Управление перенесено в приложение: откройте APP и подтвердите действие там.", alert=True)
        return
    if key == "menu": await show_menu(event, True); return
    if key == "clear_journal":
        _, deleted = await clear_private_chat(uid, p)
        await event.answer(f"Чат очищен: {deleted}")
        # Keep the pinned controller and its inline button in place.
        return
    if key == "about":
        await event.edit("<b>ℹ️ WALLET HUNTER — ЧТО ДЕЛАЕТ БОТ</b>\n\n"
                         "<b>Назначение.</b> Бот отслеживает до трёх публичных кошельков Hyperliquid и копирует их открытые позиции на ваш личный аккаунт через введённый вами API wallet.\n\n"
                         "<b>Размер позиции.</b> Для каждого кошелька выделяется часть вашего баланса. Бот повторяет процент маржи и плечо кошелька, затем ограничивает размер вашими риск-лимитами. Совпадающие позиции неттируются.\n\n"
                         "<b>Бюджет.</b> Депозит делится на фиксированные три доли. Свободный или выключенный слот не отдаёт свою долю другим.\n\n"
                         "<b>Защита.</b> Лимиты размера и количества позиций, фильтры цены, спреда и фандинга. Дневной лимит запрещает новый риск, но разрешает выход вслед за кошельком. Обычная пауза не закрывает позиции. Неизвестное исполнение блокирует повторный ордер до сверки.\n\n"
                         "<b>Аналитика.</b> PnL закрытых исполнений с комиссиями, без фандинга. История проверяется на дубли и ограничения API. Оценка издержек не является бэктестом нашего депозита. Полная просадка счёта, ROI и Sharpe не выдумываются по одним исполнениям.\n\n"
                         "<b>AI.</b> При ROE ≤−40% изучает восемь факторов и готовит варианты сокращения или небольшого добора. Вероятность успеха неизвестна, результат не гарантирован. Действие выполняется только после вашего подтверждения в APP; копирование этой монеты затем приостанавливается до сверки. Доборы: до 50% доли своего кошелька, максимум четыре. Отдельный AI-трейдер: 10% своей доли 1/3, плечо до 40× в пределах настроек и лимита монеты. Виртуальные сделки не тратят деньги, реальные формы требуют подтверждения.\n\n"
                         "<b>Управление.</b> Настройки, предупреждения и ручные действия находятся в APP. Это инструмент исполнения и анализа, не гарантия прибыли.",
                         buttons=[[Button.inline("◀️ Назад", b"menu")]], parse_mode="html"); return
    if key == "account":
        waiting[uid] = "account"
        await event.edit("<b>🎯 Target Hyperliquid account</b>\n\nОтправьте одной строкой:\n<code>Название | 0xACCOUNT_ADDRESS | API_PRIVATE_KEY</code>\n\nЭтот ключ виден только в вашем профиле.", buttons=[[Button.inline("◀️ Назад", b"menu")]], parse_mode="html"); return
    if key == "leaders":
        await show_leaders(event, p); return
    if key == "addleader":
        waiting[uid] = "leader"; await event.edit("Отправьте отслеживаемый кошелёк Hyperliquid:\n<code>0x...</code>", buttons=[[Button.inline("◀️ Назад", b"leaders")]], parse_mode="html"); return
    if key.startswith("delleader:"):
        index = int(key.split(":", 1)[1])
        if 0 <= index < len(p["leaders"]):
            wallet = p["leaders"].pop(index); p.setdefault("leader_enabled", {}).pop(wallet, None); await save_profile(uid, p)
        await show_leaders(event, p); return
    if key.startswith("toggleleader:"):
        index = int(key.split(":", 1)[1])
        if 0 <= index < len(p["leaders"]):
            wallet = p["leaders"][index]
            p.setdefault("leader_enabled", {})[wallet] = not leader_is_enabled(p, wallet)
            await save_profile(uid, p)
        await show_leaders(event, p); return
    if key == "copy":
        if not p.get("account") or not active_leaders(p):
            await event.answer("Сначала подключите Hyperliquid-аккаунт и включите хотя бы один кошелёк.", alert=True); return
        p["copy_enabled"] = not p.get("copy_enabled")
        if p["copy_enabled"] and str((p.get("runtime") or {}).get("blocked_reason", "")).startswith("Emergency"):
            p.setdefault("runtime", {}).pop("blocked_reason", None)
        await save_profile(uid, p); await show_menu(event, True); return
    if key in {"crypto", "stocks", "notify"}:
        field = {"crypto":"crypto_enabled", "stocks":"stocks_enabled", "notify":"notifications"}[key]
        p[field] = not p.get(field); await save_profile(uid, p); await show_menu(event, True); return
    if key == "risk":
        current = p.get("risk_mode", "standard")
        rows = [[Button.inline(("✅ " if name == current else "") + spec["label"], f"risk:{name}".encode())] for name, spec in engine.RISK_PRESETS.items()]
        rows.append([Button.inline("◀️ Назад", b"menu")])
        await event.edit("<b>🛡 РИСК-ПРОФИЛЬ</b>\n\n"
                         "Консервативный: 50% размера, 3 позиции, 2% дневной лимит.\n"
                         "Стандартный: 100% размера, 6 позиций, 5% дневной лимит.\n"
                         "Агрессивный: 125% размера, 10 позиций, 10% дневной лимит.\n\n"
                         "Лимит на один актив применяется автоматически.", buttons=rows, parse_mode="html"); return
    if key.startswith("risk:"):
        mode = key.split(":", 1)[1]
        if mode in engine.RISK_PRESETS:
            p["risk_mode"] = mode
            (p.get("runtime") or {}).pop("blocked_reason", None)
            await save_profile(uid, p)
        await show_menu(event, True); return
    if key == "strategy":
        current = p.get("strategy_mode", "swing")
        rows = [[Button.inline(("✅ " if name == current else "") + spec["label"], f"strategy:{name}".encode())] for name, spec in engine.STRATEGY_PRESETS.items()]
        rows.append([Button.inline("◀️ Назад", b"menu")])
        await event.edit("<b>🎛 СТРАТЕГИЯ ИСПОЛНЕНИЯ</b>\n\n"
                         "Conservative — только узкий spread и маленький adverse funding.\n"
                         "Swing — базовый режим для более долгих позиций.\n"
                         "Scalping — самый строгий spread и короткий стоп.\n"
                         "Experimental — менее строгие фильтры; используйте только с малым бюджетом.", buttons=rows, parse_mode="html"); return
    if key.startswith("strategy:"):
        mode = key.split(":", 1)[1]
        if mode in engine.STRATEGY_PRESETS:
            p["strategy_mode"] = mode; await save_profile(uid, p)
        await show_menu(event, True); return
    if key == "leverage":
        try:
            exchange_limits = await asyncio.to_thread(reader.leverage_choices)
            available = [x for x in exchange_limits if x <= S.max_leverage]
        except Exception as exc:
            await event.answer(f"Не удалось получить лимиты биржи: {exc}", alert=True); return
        available = sorted(set([1, *available]))
        current = int(float(p.get("max_leverage") or S.max_leverage))
        rows = [[Button.inline(("✅ " if value == current else "") + f"{value}x", f"leverage:{value}".encode()) for value in available[i:i+3]] for i in range(0, len(available), 3)]
        rows.append([Button.inline("◀️ Назад", b"menu")])
        exchange_text = ', '.join(str(x) + 'x' for x in exchange_limits)
        await event.edit(f"<b>⚙️ МАКСИМАЛЬНОЕ ПЛЕЧО</b>\n\nВаш предел: <b>{current}x</b>. Ни одна копируемая позиция не превысит его.\n\nHyperliquid сейчас публикует максимумы: <b>{exchange_text}</b>. Для каждого рынка бот дополнительно применяет его собственный максимум.\n\nВ этом боте можно выбрать до <b>{S.max_leverage}x</b> — это серверный защитный предел.", buttons=rows, parse_mode="html"); return
    if key.startswith("leverage:"):
        value = int(key.split(":", 1)[1])
        if 1 <= value <= S.max_leverage:
            p["max_leverage"] = value; await save_profile(uid, p)
        await show_menu(event, True); return
    if key == "emergency":
        await event.edit("<b>🚨 EMERGENCY STOP</b>\n\n"
                         "Остановка всегда выключает новое копирование и отменяет resting-ордера.\n"
                         "Открытые позиции сохраняются, пока вы отдельно не подтвердите их закрытие.",
                         buttons=[[Button.inline("⏸ Пауза + отменить ордера", b"estop:hold")],
                                  [Button.inline("🛑 Пауза + закрыть позиции бота", b"estop:close")],
                                  [Button.inline("◀️ Назад", b"menu")]], parse_mode="html"); return
    if key.startswith("estop:"):
        if not p.get("account"):
            await event.answer("Target account не подключен.", alert=True); return
        close = key.endswith("close")
        p["copy_enabled"] = False
        c = account_client(uid, p)
        results = await engine.emergency_stop(uid, p, c, close_managed=close)
        await save_profile(uid, p)
        for result in results: await notify(uid, p, result, p["account"])
        errors = [x.error for x in results if not x.ok]
        await event.edit("✅ <b>Emergency stop выполнен.</b>" if not errors else f"⚠️ <b>Emergency stop с ошибкой:</b>\n<code>{esc('; '.join(errors))}</code>", buttons=[[Button.inline("◀️ В панель", b"menu")]], parse_mode="html"); return
    if key == "journal":
        entries = list((p.get("runtime") or {}).get("journal") or [])[-15:][::-1]
        if not entries: text = "<i>Операций пока нет.</i>"
        else:
            text = "\n".join(f"<b>{datetime.fromtimestamp(x['time']/1000).strftime('%d.%m %H:%M')}</b> · {esc(x['action'])} {esc(x.get('coin') or '')} {esc(x.get('side') or '')} · ${float(x.get('notional',0)):+,.2f}{' ⚠️' if x.get('error') else ''}" for x in entries)
        await event.edit(f"<b>📜 ЖУРНАЛ ПОСЛЕДНИХ ОПЕРАЦИЙ</b>\n\n{text}", buttons=[[Button.inline("◀️ Назад", b"menu")]], parse_mode="html"); return
    if key == "reports":
        await event.edit("<b>🧾 ОТЧЁТЫ</b>\n\nВыберите период.", buttons=[[Button.inline("Сегодня", b"report:1"), Button.inline("7 дней", b"report:7")], [Button.inline("◀️ Назад", b"menu")]], parse_mode="html"); return
    if key.startswith("report:"):
        if p.get("account"): await account_report(event, uid, p, int(key.split(":", 1)[1]))
        else: await event.answer("Target account не подключен.", alert=True)
        return
    if key == "analyse":
        rows = [[Button.inline(f"Кошелёк #{i+1} {'🟢' if leader_is_enabled(p, wallet) else '⏸'}", f"analyse:{i}".encode())] for i, wallet in enumerate(p["leaders"])]
        rows.append([Button.inline("✍️ Ввести другой wallet", b"analyse:custom")])
        rows.append([Button.inline("◀️ Назад", b"menu")])
        await event.edit("<b>🧪 АНАЛИЗ КОШЕЛЬКА · 90 ДНЕЙ</b>\n\nВыберите сохранённый кошелёк либо укажите любой публичный адрес Hyperliquid. Анализ не добавляет адрес к копированию.", buttons=rows, parse_mode="html"); return
    if key == "analyse:custom":
        waiting[uid] = "analyse_wallet"
        await event.edit("Отправьте wallet для разового анализа:\n<code>0x...</code>\n\nОн не будет добавлен в копирование.", buttons=[[Button.inline("◀️ Назад", b"analyse")]], parse_mode="html"); return
    if key.startswith("analyse:"):
        index = int(key.split(":", 1)[1])
        if 0 <= index < len(p["leaders"]): await analyse(event, p["leaders"][index], uid, p)
        return
    if key == "stats": await stats(event, uid, p); return

async def analyse(event, wallet, uid, p, edit=True):
    if edit: await event.edit("⏳ <b>Анализирую 90 дней и открытые позиции…</b>", parse_mode="html")
    else: await event.respond("⏳ <b>Анализирую 90 дней и открытые позиции…</b>", parse_mode="html")
    try:
        fills = await asyncio.to_thread(reader.fills_90d, wallet)
        bal, positions = await asyncio.gather(asyncio.to_thread(reader.balance, wallet), asyncio.to_thread(reader.positions, wallet, True, True))
        r = analyzer.report(fills, bal); sim = simulator.simulate(fills); pf = "∞" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
        # Research must never write the legacy admission/configuration snapshot.
        # Existing leader_models remain unchanged; only explicit configuration
        # may replace that policy. Web analysis is also authority-read-only.
        models = p.setdefault("wallet_research", {})
        models[wallet] = {"score": sim.score, "eligible": sim.eligible, "net_pnl": sim.net_pnl,
                          "cost_usd": sim.cost_usd, "train_pf": persisted_profit_factor(sim.train_pf),
                          "test_pf": persisted_profit_factor(sim.test_pf),
                          "daily_pnl": sim.daily_pnl, "updated_ms": int(time.time() * 1000)}
        correlations = [abs(simulator.correlation(sim.daily_pnl, model.get("daily_pnl") or {}))
                        for other, model in models.items() if other != wallet and model.get("daily_pnl")]
        models[wallet]["max_correlation"] = max(correlations, default=0.0)
        for other, model in models.items():
            if other != wallet and model.get("daily_pnl"):
                model["max_correlation"] = max(float(model.get("max_correlation", 0) or 0),
                                                abs(simulator.correlation(model["daily_pnl"], sim.daily_pnl)))
        await save_profile(uid, p)
        open_rows = [f"{'🟢' if x['side']=='LONG' else '🔴'} <b>{esc(x['coin'])}</b> {x['side']} · ${x['position_value']:,.2f} · {x['leverage']:g}x · PnL ${x['unrealized_pnl']:+,.2f}" for x in positions]
        open_text = "\n".join(open_rows[:15]) if open_rows else "<i>Нет открытых сделок.</i>"
        if len(open_rows) > 15: open_text += f"\n<i>… ещё {len(open_rows)-15}</i>"
        text = (f"<b>🧪 ПРОВЕРКА КОШЕЛЬКА · 90 ДНЕЙ</b>\n<code>{esc(wallet)}</code>\n\n"
                f"Баланс wallet: <b>${bal:,.2f} USDC</b>\n"
                f"<b>Рейтинг {r.rating}/100</b> · {r.recommendation}\n"
                f"Закрывающих исполнений: <b>{r.trades}</b> | Win rate: <b>{r.win_rate:.1f}%</b> | PF: <b>{pf}</b>\n"
                f"Net PnL: <b>${r.net_pnl:+,.2f}</b> | Expectancy: <b>${r.expectancy:+,.2f}</b>\n"
                f"Просадка закрытых исполнений: <b>${r.max_drawdown:,.2f}</b>\n"
                f"Полная просадка капитала / Sharpe / ROI: <b>не определены по одним исполнениям</b>\n"
                f"Положительных дней: <b>{r.positive_days}/{r.active_days}</b>\n"
                f"Концентрация по инструменту: <b>{r.top_coin_share:.1f}%</b>\n\n"
                f"<b>🧪 СТРЕСС-ТЕСТ ИЗДЕРЖЕК</b>\nОценка сценария: <b>{sim.score}/100</b>\n"
                f"Результат после издержек: <b>${sim.net_pnl:+,.2f}</b> · Издержки: <b>${sim.cost_usd:,.2f}</b>\n"
                f"PF раннего / позднего периода: <b>{'∞' if sim.train_pf == float('inf') else f'{sim.train_pf:.2f}'} / {'∞' if sim.test_pf == float('inf') else f'{sim.test_pf:.2f}'}</b> · Просадка: <b>${sim.max_drawdown:,.2f}</b>\n"
                f"<i>Не бэктест нашего депозита, не прогноз прибыли и не допуск к торговле.</i>\n\n"
                f"<b>📌 ОТКРЫТЫЕ СДЕЛКИ ({len(positions)})</b>\n{open_text}")
        if edit: await event.edit(text, buttons=[[Button.inline("◀️ Назад", b"menu")]], parse_mode="html")
        else: await event.respond(text, buttons=[[Button.inline("◀️ В панель", b"menu")]], parse_mode="html")
    except Exception as exc:
        text = f"❌ Ошибка анализа: <code>{esc(exc)}</code>"
        if edit: await event.edit(text, buttons=[[Button.inline("◀️ Назад", b"menu")]], parse_mode="html")
        else: await event.respond(text, buttons=menu_button(), parse_mode="html")

async def stats(event, uid, p):
    if not p.get("account"): await event.answer("Сначала подключите Target account.", alert=True); return
    try:
        c = account_client(uid, p); bal, positions = await asyncio.gather(asyncio.to_thread(c.balance), asyncio.to_thread(c.positions, True, True))
        rows = "\n".join(f"{'🟢' if x['side']=='LONG' else '🔴'} {esc(x['coin'])} {x['side']} · ${x['position_value']:,.2f} · PnL ${x['unrealized_pnl']:+,.2f}" for x in positions[:15]) or "<i>Нет позиций.</i>"
        await event.edit(f"<b>📊 {esc(p['account']['name'])}</b>\n\nBalance: <b>${bal:,.2f}</b>\nOpen positions: <b>{len(positions)}</b>\n\n{rows}", buttons=[[Button.inline("◀️ Назад", b"menu")]], parse_mode="html")
    except Exception as exc: await event.edit(f"❌ Ошибка: <code>{esc(exc)}</code>", buttons=[[Button.inline("◀️ Назад", b"menu")]], parse_mode="html")

async def account_report(event, uid, p, days):
    await event.edit("⏳ Формирую отчёт…", parse_mode="html")
    try:
        c = account_client(uid, p)
        balance_value, positions, fills = await asyncio.gather(asyncio.to_thread(c.balance), asyncio.to_thread(c.positions, True, True), asyncio.to_thread(c.fills_90d))
        threshold = int((time.time() - days * 86400) * 1000)
        recent = [fill for fill in fills if int(fill.get("time", 0) or 0) >= threshold]
        report = analyzer.report(recent, balance_value)
        pf = "∞" if report.profit_factor == float("inf") else f"{report.profit_factor:.2f}"
        exposure = sum(float(x.get("position_value", 0) or 0) for x in positions)
        await event.edit(f"<b>🧾 МОЙ ОТЧЁТ · {days} {'ДЕНЬ' if days == 1 else 'ДНЕЙ'}</b>\n\n"
                         f"Balance: <b>${balance_value:,.2f}</b>\nOpen exposure: <b>${exposure:,.2f}</b> · Positions: <b>{len(positions)}</b>\n\n"
                         f"Realized PnL: <b>${report.net_pnl:+,.2f}</b>\n"
                         f"Closing fills: <b>{report.trades}</b> · Win rate: <b>{report.win_rate:.1f}%</b> · PF: <b>{pf}</b>\n"
                         f"Max DD: <b>${report.max_drawdown:,.2f}</b> · Expectancy: <b>${report.expectancy:+,.2f}</b>",
                         buttons=[[Button.inline("◀️ Назад", b"reports")]], parse_mode="html")
    except Exception as exc:
        await event.edit(f"❌ Ошибка отчёта: <code>{esc(exc)}</code>", buttons=[[Button.inline("◀️ Назад", b"menu")]], parse_mode="html")

async def scheduled_report(uid, p, c, days, title):
    try:
        balance_value, positions, fills = await asyncio.gather(asyncio.to_thread(c.balance), asyncio.to_thread(c.positions, True, True), asyncio.to_thread(c.fills_90d))
        cutoff = int((time.time() - days * 86400) * 1000)
        report = analyzer.report([x for x in fills if int(x.get("time", 0) or 0) >= cutoff], balance_value)
        exposure = sum(float(x.get("position_value", 0) or 0) for x in positions)
        pf = "∞" if report.profit_factor == float("inf") else f"{report.profit_factor:.2f}"
        message = await client.send_message(uid, f"<b>🧾 {title}</b>\n\nBalance: <b>${balance_value:,.2f}</b>\nExposure: <b>${exposure:,.2f}</b> · Positions: <b>{len(positions)}</b>\nRealized PnL: <b>${report.net_pnl:+,.2f}</b>\nWin rate: <b>{report.win_rate:.1f}%</b> · PF: <b>{pf}</b>", parse_mode="html")
        remember_notification(uid, p, message)
    except Exception as exc: print("[REPORT]", uid, type(exc).__name__)

async def watcher_cycle():
    # Bound concurrent users, but never cancel a possibly submitted execution.
    # SDK transport timeouts bound individual network calls instead.
    limit = asyncio.Semaphore(4)
    async def process(uid_text, p):
        async with limit:
            try:
                if not p.get("copy_enabled") or not p.get("account") or not p.get("leaders"): return
                uid = int(uid_text); c = await asyncio.to_thread(account_client, uid, p); snapshots = []
                for wallet in p["leaders"][:3]:
                    positions, bal = await asyncio.gather(asyncio.to_thread(reader.positions, wallet, p.get("crypto_enabled", True), p.get("stocks_enabled", True)), asyncio.to_thread(reader.balance, wallet))
                    snapshots.append({"wallet":wallet, "positions":positions, "balance":bal, "enabled":leader_is_enabled(p, wallet)})
                await engine.sync_profile(uid, p, c, snapshots, lambda result, account: notify(uid, p, result, account))
                now = datetime.now().astimezone()
                runtime = p.setdefault("runtime", {})
                if p.get("notifications") and now.hour >= 20 and runtime.get("daily_report") != now.date().isoformat():
                    await scheduled_report(uid, p, c, 1, "ЕЖЕДНЕВНЫЙ ОТЧЁТ")
                    runtime["daily_report"] = now.date().isoformat()
                week = f"{now.isocalendar().year}-{now.isocalendar().week}"
                if p.get("notifications") and now.weekday() == 0 and now.hour >= 20 and runtime.get("weekly_report") != week:
                    await scheduled_report(uid, p, c, 7, "ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ")
                    runtime["weekly_report"] = week
                store.update_runtime(uid, runtime)
            except Exception as exc:
                print("[WATCHER USER]", uid_text, type(exc).__name__)
    await asyncio.gather(*(process(uid, p) for uid, p in store.load().get("profiles", {}).items()))

async def watcher():
    while True:
        try:
            await watcher_cycle()
        except Exception as exc: print("[WATCHER CYCLE]", type(exc).__name__)
        await asyncio.sleep(S.watch_interval)


async def ai_review_watcher():
    # Separate from copying: declining, pausing copy, or an empty AI source
    # slot does not stop the review of the user's existing positions.
    while True:
        try:
            profiles = store.load().get("profiles", {})
            for uid_text, p in profiles.items():
                if not p.get("account"): continue
                uid = int(uid_text)
                try:
                    account_reader = public_account_reader(p)
                    own_positions = await asyncio.to_thread(account_reader.positions, True, True)
                    await asyncio.to_thread(ai_assistant.observe, uid, own_positions)
                    await asyncio.to_thread(ai_review.generate, uid, p, account_reader, reader)
                    await asyncio.to_thread(ai_review.research.poll, uid, reader, int(time.time() * 1000))
                    _, notification_profile = store.profile(uid)
                    if not notification_profile.get("notifications", True) or not notification_profile.get("ai_review_enabled", True): continue
                    for row in ai_review.list(uid):
                        if row["notified"] or row["status"] != "PENDING" or row["expires"] < time.time(): continue
                        en = notification_profile.get("language") == "en"
                        buttons = [[Button.inline("Confirm" if en else "Подтвердить", f'air:yes:{row["id"]}'.encode()),
                                    Button.inline("No" if en else "Нет", f'air:no:{row["id"]}'.encode())]] if row["status"] == "PENDING" else None
                        message = await client.send_message(uid, review_text(row, en), buttons=buttons, parse_mode=None)
                        ai_review.mark_notified(row["id"])
                        _, fresh = profile(uid)
                        remember_notification(uid, fresh, message)
                except Exception as exc:
                    print("[AI REVIEW]", uid, type(exc).__name__)
        except Exception as exc:
            print("[AI REVIEW LOOP]", type(exc).__name__)
        await asyncio.sleep(60)

def reconcile_ai_lifecycle(uid):
    # Separate from the read-only signal observer. No signing client is created.
    from core.ai_review import account_guard
    with account_guard(ROOT, f"telegram-profile:{uid}"):
        _, p = store.profile(uid)
        account = p.get("account")
        if not account: return
        with account_guard(ROOT, account["address"]):
            _, fresh = store.profile(uid)
            if fresh.get("account") != account: return
            ai_user_orders.reconcile_closed(uid, fresh, public_account_reader(fresh),
                lambda: store.update_runtime(uid, fresh["runtime"]), int(time.time() * 1000))

async def ai_lifecycle_watcher():
    while True:
        try:
            for uid in store.load().get("profiles", {}):
                try: await asyncio.to_thread(reconcile_ai_lifecycle, int(uid))
                except Exception as exc: print("[AI RECONCILIATION]", uid, type(exc).__name__)
        except Exception as exc: print("[AI RECONCILIATION LOOP]", type(exc).__name__)
        await asyncio.sleep(60)

async def main():
    await client.start(bot_token=S.telegram_bot_token)
    try:
        response = await asyncio.to_thread(
            requests.post,
            f"https://api.telegram.org/bot{S.telegram_bot_token}/setChatMenuButton",
            json={"menu_button": {"type": "web_app", "text": "🚀 ОТКРЫТЬ APP", "web_app": {"url": WEBAPP_URL}}},
            timeout=15,
        )
        response.raise_for_status()
    except Exception as exc:
        print("[WEBAPP MENU]", type(exc).__name__)
    print("[RUN] WalletHunter V07 running")
    asyncio.create_task(watcher())
    asyncio.create_task(ai_trader_watcher())
    asyncio.create_task(ai_learning_watcher())
    asyncio.create_task(ai_review_watcher())
    asyncio.create_task(ai_position_watcher())
    asyncio.create_task(ai_entry_watcher())
    asyncio.create_task(ai_lifecycle_watcher())
    await client.run_until_disconnected()

if __name__ == "__main__": asyncio.run(main())
