"""Guard legacy profile edits without locking notification/history writes.

The lock namespace matches the Mini App API. This helper never executes orders
or rebases conflicting user edits over fresh settings. Storage retains its
three-way conflict checks under its own atomic file lock.
"""
import os
from contextlib import ExitStack

from core.ai_review import account_guard
from core.state_snapshot import Snapshot, StateConflict, plain


TRADING_FIELDS = frozenset({
    "account", "leaders", "leader_enabled", "copy_enabled", "crypto_enabled", "stocks_enabled",
    "risk_mode", "strategy_mode", "max_leverage", "leader_exit_only", "auto_trading",
    "max_position_pct", "max_total_exposure_usd", "max_slippage_pct", "entry_price_tolerance_pct",
    "budget", "capital_allocations", "risk_limits", "ai_slot_selected", "ai_active",
    "ai_live_enabled", "ai_auto_trading", "ai_review_enabled", "ai_review_policy", "ai_trader_enabled",
})
TRADING_RUNTIME_FIELDS = frozenset({
    "account", "managed", "detached_keys", "manual_stops", "manual_actions", "manual_hold_keys",
    "ai_hold_keys", "ai_budget_usage", "positions", "paused_source_markets", "blocked_reason",
    "ai_user_order_holds", "ai_user_order_positions", "ai_position_action_holds",
    "recovery_required", "cooldowns", "daily_limit", "risk_day", "daily_pnl", "paper_daily_pnl",
})
_MISSING = object()


def legacy_trading_callback(key):
    """Obsolete buttons navigate to the confirmed Mini App trading controls."""
    return key in {"account", "addleader", "copy", "crypto", "stocks", "risk", "strategy",
                   "leverage", "emergency"} or key.startswith(("delleader:", "toggleleader:", "risk:",
                                                              "strategy:", "leverage:", "estop:"))


def trading_changes(profile):
    if not isinstance(profile, Snapshot):
        raise StateConflict("Profile update requires a loaded snapshot")
    changed = {field for field in TRADING_FIELDS
               if (field in profile) != (field in profile.base) or
               (field in profile and plain(profile[field]) != profile.base[field])}

    def runtime_changes(runtime, fallback, path):
        if not isinstance(runtime, dict):
            if runtime != fallback: changed.add(path)
            return
        # A runtime Snapshot may already have been persisted/refreshed by the
        # engine while its parent profile snapshot is older. Those acknowledged
        # changes must not make a later chat-clear acquire the same trade lock.
        base = getattr(runtime, "base", fallback)
        base = base if isinstance(base, dict) else {}
        for field in TRADING_RUNTIME_FIELDS:
            if ((field in runtime) != (field in base) or
                    (field in runtime and plain(runtime[field]) != base[field])):
                changed.add(f"{path}.{field}")
        if "paper_runtime" in runtime or "paper_runtime" in base:
            runtime_changes(runtime.get("paper_runtime", _MISSING), base.get("paper_runtime", _MISSING),
                            f"{path}.paper_runtime")
    if "runtime" in profile or "runtime" in profile.base:
        runtime_changes(profile.get("runtime", _MISSING), profile.base.get("runtime", _MISSING), "runtime")
    return changed


def save_profile_guarded(storage, user_id, profile, root=None):
    """Save a loaded profile; busy trading edits fail without changing the disk.

    Language, notification delivery/clear history and leader_models are purely
    analytical/UI changes here and do not reacquire the account trading lock.
    """
    if not isinstance(profile, Snapshot): raise StateConflict("Profile update requires a loaded snapshot")
    if str(profile.get("user_id")) != str(int(user_id)):
        raise StateConflict("Profile owner changed; reload before saving")
    # Storage.update_runtime refreshes the nested Snapshot after committing.
    # Carry that acknowledged baseline into a later whole-profile save, or a
    # chat clear could replay an already-saved ownership change as a new edit.
    if isinstance(profile.get("runtime"), Snapshot):
        profile.base["runtime"] = plain(profile["runtime"].base)
    changes = trading_changes(profile)
    if not changes:
        storage.update_profile(user_id, profile)
        return
    root = root or os.path.dirname(os.path.dirname(storage.path))
    with ExitStack() as locks:
        try:
            locks.enter_context(account_guard(root, f"telegram-profile:{int(user_id)}"))
            _, initial = storage.profile(user_id)
            actual_account = initial.get("account")
            addresses = {(record or {}).get("address", "").strip().lower()
                         for record in (actual_account, profile.base.get("account"), profile.get("account"))}
            for address in sorted(addresses-{ "" }):
                locks.enter_context(account_guard(root, address))
        except OSError as exc:
            message = ("An account action is in progress. Refresh and try again." if profile.get("language") == "en"
                       else "Сейчас выполняется операция по аккаунту. Обновите экран и повторите действие.")
            raise ValueError(message) from exc
        _, fresh = storage.profile(user_id)
        if fresh.get("account") != actual_account or fresh.get("account") != profile.base.get("account"):
            raise StateConflict("Account changed; reload before applying trading settings")
        # Keep the ORIGINAL baseline, rather than grafting stale choices onto
        # the fresh profile. Storage raises on conflicting concurrent settings.
        storage.update_profile(user_id, profile)
