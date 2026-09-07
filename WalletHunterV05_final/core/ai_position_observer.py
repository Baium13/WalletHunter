"""Public-only position review scheduling and notification text; no signer."""
import math
import re
import time
from urllib.parse import urlsplit, urlunsplit
from core.ai_review import account_guard


class AiPositionObserver:
    def __init__(self, root, storage, actions, public_factory, reader):
        self.root, self.storage, self.actions = root, storage, actions
        self.public_factory, self.reader = public_factory, reader

    def prepare(self, uid):
        with account_guard(self.root, f"telegram-profile:{int(uid)}"):
            _, profile = self.storage.profile(uid)
            account = profile.get("account")
            if not account or not profile.get("ai_review_enabled", True):
                return None
            with account_guard(self.root, account["address"]):
                _, fresh = self.storage.profile(uid)
                if fresh.get("account") != account or not fresh.get("ai_review_enabled", True):
                    return None
                return self.actions.prepare(uid, fresh, self.public_factory(fresh), self.reader,
                                            int(time.time()*1000), force_refresh=False)

    def notices(self, uid):
        _, profile = self.storage.profile(uid)
        if not profile.get("account") or not profile.get("ai_review_enabled", True) or not profile.get("notifications", True):
            return [], profile
        rows = self.actions.pending_for_notification(uid, profile, int(time.time()*1000))
        groups = {}
        for row in rows:
            payload = row.get("payload") or {}
            market = (payload.get("coin"), payload.get("dex") or "")
            groups.setdefault(market, []).append(row)
        return list(groups.values()), profile


def position_notice(rows, english=False):
    """No account balance or private wallet identifier in Telegram previews."""
    t = lambda ru, en: en if english else ru
    first = (rows[0].get("payload") or {}) if rows else {}
    coin = str(first.get("coin") or "")
    if not re.fullmatch(r"[A-Za-z0-9_:.\-]{1,40}", coin): coin = "—"
    before = first.get("position_before") or {}
    value = before.get("roe", before.get("roe_pct"))
    roe = f"{value:+.2f}%" if isinstance(value, (float,int)) and not isinstance(value,bool) and math.isfinite(value) else "—"
    names = {"REDUCE":t("сократить позицию", "reduce the position"),
             "AVERAGE":t("рассмотреть добор", "consider adding to the position")}
    actions = list(dict.fromkeys(names[p["payload"].get("action")] for p in rows
                                if isinstance(p.get("payload"),dict) and p["payload"].get("action") in names))
    return "\n".join([
        t("🧠 AI ПОПРАВКА", "🧠 AI POSITION REVIEW") + f" · {coin}",
        f"ROE: {roe}",
        t("Подготовлены варианты: ", "Prepared options: ") + "; ".join(actions),
        t("Вероятность успеха неизвестна. Результат не гарантирован.", "Success probability is unknown. Results are not guaranteed."),
        t("Ничего не исполнено. В APP → AI проверьте причину, сумму и последствия; затем подтвердите или отклоните.",
          "Nothing was executed. In APP → AI, review the reason, amount and consequences, then confirm or decline."),
        t("Если форма устарела, подготовьте свежую проверку в приложении.", "If the form has expired, prepare a fresh review in the app.")])


def position_app_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
        raise ValueError("A valid existing HTTPS Mini App URL is required")
    return urlunsplit((parts.scheme,parts.netloc,parts.path,parts.query,"ai-position"))
