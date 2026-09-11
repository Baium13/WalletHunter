import json, os, threading, tempfile, hashlib, math
from contextlib import contextmanager
from copy import deepcopy
from cryptography.fernet import Fernet
from core.state_snapshot import Snapshot, StateConflict, merge, plain, snapshot

DEFAULT = {
    "version": 2,
    "profiles": {},
}

_MESSAGE_FIELDS = ("notification_message_ids", "chat_message_ids")
_MISSING = object()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError(f"Duplicate state field: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Non-finite value in state document: {value}")


def _timestamp(value):
    if isinstance(value, bool): raise ValueError("Invalid state timestamp")
    parsed = float(value or 0)
    if not math.isfinite(parsed) or parsed < 0 or parsed != int(parsed):
        raise ValueError("Invalid state timestamp")
    return int(parsed)


def _ids(value):
    if not isinstance(value, list): raise ValueError("Message IDs must be a list")
    result = set()
    for item in value:
        if isinstance(item, bool) or not str(item).isdigit(): raise ValueError("Invalid message ID")
        result.add(int(item))
    return result


def _validate_runtime(runtime):
    if not isinstance(runtime, dict): raise ValueError("Invalid runtime: expected an object")
    for field in ("managed", "detached_keys", "journal", "pending_notifications")+_MESSAGE_FIELDS:
        if field in runtime and not isinstance(runtime[field], list):
            raise ValueError(f"Invalid runtime {field}: expected a list")
    for field in _MESSAGE_FIELDS:
        if field in runtime: _ids(runtime[field])
    for row in runtime.get("journal", []):
        if not isinstance(row, dict): raise ValueError("Invalid journal entry")
        _timestamp(row.get("time", 0))
    for row in runtime.get("pending_notifications", []):
        if not isinstance(row, dict) or not isinstance(row.get("result"), dict):
            raise ValueError("Invalid pending notification")
        _timestamp(row.get("created_ms", 0))  # Legacy rows may have no timestamp.
        _timestamp(row.get("attempts", 0))
        retry = float(row.get("next_retry", 0))
        if not math.isfinite(retry) or retry < 0: raise ValueError("Invalid notification retry time")
        if "id" in row and (not isinstance(row["id"], str) or not row["id"]):
            raise ValueError("Invalid notification identity")
    if "manual_stops" in runtime and not isinstance(runtime["manual_stops"], dict):
        raise ValueError("Invalid manual stop registry")
    _timestamp(runtime.get("journal_cleared_at", 0))
    tombstones = runtime.get("message_tombstones", {})
    if not isinstance(tombstones, dict): raise ValueError("Invalid message tombstones")
    for field in _MESSAGE_FIELDS:
        if field in tombstones: _ids(tombstones[field])
    if "paper_runtime" in runtime: _validate_runtime(runtime["paper_runtime"])


def _validate_state(data):
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        raise ValueError("Invalid state profiles: expected an object")
    for value in data["profiles"].values():
        if not isinstance(value, dict): raise ValueError("Invalid profile: expected an object")
        if "runtime" in value: _validate_runtime(value["runtime"])
        if "account" in value and value["account"] is not None and not isinstance(value["account"], dict):
            raise ValueError("Invalid account: expected an object or null")
        if "leaders" in value and not isinstance(value["leaders"], list):
            raise ValueError("Invalid wallet list")


def _validate_unique_accounts(data):
    owners = {}
    for uid, profile in data["profiles"].items():
        account = profile.get("account") or {}
        address = str(account.get("address") or "").strip().lower()
        if not address: continue
        if address in owners and owners[address] != uid:
            raise StateConflict("Hyperliquid account is already bound to another Telegram profile")
        owners[address] = uid


def _queue_identity(item):
    if item.get("id"):
        return str(item["id"])
    # Older queues have no ID. Retry counters/errors are mutable and must not
    # cause one failed delivery to become two notifications during a merge.
    immutable = {"created_ms": item.get("created_ms", 0), "result": item["result"]}
    return "legacy:"+hashlib.sha256(json.dumps(immutable, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _merge_queue(base, incoming, current, cutoff):
    def index(rows):
        found = {}
        for item in rows:
            if cutoff and _timestamp(item.get("created_ms", 0)) <= cutoff: continue
            key = _queue_identity(item)
            if key in found:
                found[key] = combine(found[key], item)
            else: found[key] = plain(item)
        return found
    def combine(a, b):
        if a.get("result") != b.get("result") or a.get("created_ms", 0) != b.get("created_ms", 0):
            raise StateConflict("Pending notification ID has conflicting immutable content")
        # Preserve the most advanced failed retry. Acknowledged removals below
        # take precedence, so stale failures cannot revive delivered messages.
        score = lambda row: (int(row.get("attempts", 0)), float(row.get("next_retry", 0)),
                             json.dumps(row, sort_keys=True))
        return plain(max((a, b), key=score))
    old, new, disk = (index(rows) for rows in (base, incoming, current))
    removed = (old.keys()-new.keys()) | (old.keys()-disk.keys())
    output = {}
    for key in new.keys() | disk.keys():
        if key in removed: continue
        if key in new and key in disk:
            if new[key].get("result") != disk[key].get("result") or new[key].get("created_ms", 0) != disk[key].get("created_ms", 0):
                raise StateConflict("Pending notification ID has conflicting immutable content")
            if key in old and new[key] == old[key]: output[key] = plain(disk[key])
            elif key in old and disk[key] == old[key]: output[key] = plain(new[key])
            else: output[key] = combine(new[key], disk[key])
        else: output[key] = plain(new.get(key, disk.get(key)))
    return sorted(output.values(), key=lambda row: (_timestamp(row.get("created_ms", 0)), _queue_identity(row)))


def _merge_runtime(base, incoming, current, path, inherited=(0, 0, 0)):
    versions = [plain(v) for v in (base, incoming, current)]
    for value in versions: _validate_runtime(value)
    marks = [max(_timestamp(v.get("journal_cleared_at", 0)), inherited[i]) for i, v in enumerate(versions)]
    cutoff = max(marks)
    tombstones = {field: set().union(*(_ids(v.get("message_tombstones", {}).get(field, [])) for v in versions))
                  for field in _MESSAGE_FIELDS}
    for branch in (1, 2):
        if marks[branch] > marks[0]:
            for field in _MESSAGE_FIELDS:
                # IDs intentionally retained by the clear operation (failed
                # deletions/pinned controller) are NOT tombstoned.
                tombstones[field] |= _ids(versions[0].get(field, []))-_ids(versions[branch].get(field, []))
    has_queue = any("pending_notifications" in v for v in versions)
    queue = _merge_queue(*(v.get("pending_notifications", []) for v in versions), cutoff) if has_queue else None
    paper = _MISSING
    if any("paper_runtime" in v for v in versions):
        values = [v.get("paper_runtime", _MISSING) for v in versions]
        if all(isinstance(v, dict) for v in values):
            paper = _merge_runtime(*values, f"{path}.paper_runtime", tuple(marks))
        else:
            # Missing/deleted branches retain normal three-way conflict rules.
            branch = _merge_tree(*({"paper_runtime": v} if v is not _MISSING else {} for v in values), f"{path}.paper_branch")
            if "paper_runtime" in branch:
                paper = _merge_runtime({}, branch["paper_runtime"], branch["paper_runtime"],
                                       f"{path}.paper_runtime", (0, cutoff, cutoff))
    for value in versions:
        value.pop("message_tombstones", None)
        value.pop("pending_notifications", None)
        value.pop("paper_runtime", None)
        if cutoff: value["journal_cleared_at"] = cutoff
        if cutoff and "journal" in value:
            value["journal"] = [row for row in value["journal"] if _timestamp(row.get("time", 0)) > cutoff]
        for field in _MESSAGE_FIELDS:
            if any(field in v for v in versions) or tombstones[field]:
                value[field] = sorted(_ids(value.get(field, []))-tombstones[field])
    result = merge(*versions, path)
    if any(tombstones.values()): result["message_tombstones"] = {k: sorted(v) for k, v in tombstones.items()}
    if has_queue: result["pending_notifications"] = queue
    if paper is not _MISSING: result["paper_runtime"] = paper
    return result


def _merge_tree(base, incoming, current, path="state"):
    if all(isinstance(v, dict) for v in (base, incoming, current)):
        if path.rsplit(".", 1)[-1] in ("runtime", "paper_runtime"):
            return _merge_runtime(base, incoming, current, path)
        result = {}
        for key in base.keys() | incoming.keys() | current.keys():
            old, new, disk = (v.get(key, _MISSING) for v in (base, incoming, current))
            if all(isinstance(v, dict) for v in (old, new, disk)):
                chosen = _merge_tree(old, new, disk, f"{path}.{key}")
            elif new == old: chosen = disk
            elif disk == old or new == disk: chosen = new
            elif _MISSING not in (old, new, disk): chosen = merge(old, new, disk, f"{path}.{key}")
            else: raise StateConflict(f"Concurrent state change at {path}.{key}; reload before retry")
            if chosen is not _MISSING: result[key] = plain(chosen)
        return result
    return merge(base, incoming, current, path)

def default_profile(user_id):
    return {
        "user_id": int(user_id),
        "account": None,
        "leaders": [],
        # The leader address remains the stable identifier.  The flag is kept
        # separately so existing profiles remain compatible with v07.
        "leader_enabled": {},
        "crypto_enabled": True,
        "stocks_enabled": True,
        "notifications": True,
        "copy_enabled": False,
        "ai_trader_enabled": False,  # Always PAPER, independent of copying.
        "ai_review_enabled": True,  # Observe copied positions, never auto-execute.
        # Follow the source's exit; manual SL/market-close remain available.
        "leader_exit_only": True,
        "risk_mode": "standard",
        "strategy_mode": "swing",
        "language": "ru",
        "max_leverage": None,
        "leader_models": {},
        "runtime": {"managed": [], "journal": [], "last_sync_ms": 0, "last_error": ""},
    }

class Storage:
    def __init__(self, root, master_key):
        self.path=os.path.join(root,"data","state.json")
        os.makedirs(os.path.dirname(self.path),exist_ok=True)
        self.lock=threading.RLock()
        self.fernet=Fernet(master_key.encode() if isinstance(master_key,str) else master_key)
        with self._file_lock():
            if not os.path.exists(self.path): self._write(DEFAULT)
        try: os.chmod(self.path,0o600)
        except OSError: pass

    def load(self):
        with self.lock:
            with open(self.path,encoding="utf-8") as source:
                data=json.load(source, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            if not isinstance(data, dict):
                raise ValueError("Invalid state document: expected an object")
            if "profiles" not in data:
                if data.get("version") == 2 or not any(k in data for k in ("accounts", "users", "leader_wallet")):
                    raise ValueError("Invalid state: missing profiles and no recognised legacy schema")
                data = self._migrate_legacy(data)
            out=deepcopy(DEFAULT); out.update(data)
            _validate_state(out)
            return snapshot(out)

    def _migrate_legacy(self, legacy):
        """Keep the existing owner's target account, but isolate it in one profile."""
        if "accounts" in legacy and not isinstance(legacy["accounts"], list):
            raise ValueError("Invalid legacy account list")
        if "users" in legacy and not isinstance(legacy["users"], list):
            raise ValueError("Invalid legacy user list")
        if any(not isinstance(a, dict) for a in legacy.get("accounts", [])):
            raise ValueError("Invalid legacy account record")
        if len(legacy.get("accounts", [])) > 1 or len(legacy.get("users", [])) > 1:
            raise ValueError("Legacy multi-account state requires an explicit ownership migration")
        user_id = 0
        users = legacy.get("users") or []
        if users:
            try: user_id = int(users[0])
            except (TypeError, ValueError) as exc: raise ValueError("Invalid legacy owner ID") from exc
        profile = default_profile(user_id)
        accounts = legacy.get("accounts") or []
        if accounts:
            account = deepcopy(accounts[0])
            account.pop("copy_enabled", None)
            profile["account"] = account
        leader = str(legacy.get("leader_wallet") or "").strip().lower()
        if leader:
            profile["leaders"] = [leader]
        profile["crypto_enabled"] = bool(legacy.get("crypto_enabled", True))
        profile["stocks_enabled"] = bool(legacy.get("stocks_enabled", True))
        profile["notifications"] = bool(legacy.get("notifications", True))
        profile["copy_enabled"] = bool(accounts and accounts[0].get("copy_enabled"))
        return {"version": 2, "profiles": {str(user_id): profile}}

    def profile(self, user_id):
        data = self.load()
        key = str(int(user_id))
        if key not in data["profiles"]:
            data["profiles"][key] = default_profile(user_id)
            self.save(data)
        return data, data["profiles"][key]

    def update_runtime(self, user_id, runtime):
        with self._file_lock():
            data = self.load()
            key = str(int(user_id))
            if key not in data["profiles"]:
                data["profiles"][key] = default_profile(user_id)
            current = data["profiles"][key].get("runtime") or {}
            incoming = plain(runtime)
            base = getattr(runtime, "base", None)
            if base is None and current and incoming != current:
                raise StateConflict("Runtime update requires a loaded snapshot")
            merged = _merge_runtime(base or {}, incoming, current, "runtime")
            data["profiles"][key]["runtime"] = merged
            self._write(data)
            if isinstance(runtime, Snapshot):
                runtime.clear()
                runtime.update(snapshot(merged))
                runtime.base = plain(merged)

    def update_profile(self, user_id, profile):
        with self._file_lock():
            data = self.load()
            key = str(int(user_id))
            if not isinstance(profile, Snapshot):
                raise StateConflict("Profile update requires a loaded snapshot")
            merged = _merge_tree(profile.base, plain(profile), data["profiles"].get(key, {}), "profile")
            data["profiles"][key] = merged
            self._write(data)
            profile.clear()
            profile.update(snapshot(merged))
            profile.base = plain(merged)

    @contextmanager
    def _file_lock(self):
        # Advisory OS lock shared by all Storage instances and processes.
        with self.lock:
            with open(self.path + ".lock", "a+b") as handle:
                if os.name == "nt":
                    import msvcrt
                    if os.fstat(handle.fileno()).st_size == 0:
                        handle.write(b"0"); handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write(self, data):
        _validate_state(data)
        _validate_unique_accounts(data)
        descriptor, tmp = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(descriptor,"w",encoding="utf-8") as target:
                json.dump(plain(data),target,ensure_ascii=False,indent=2,allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(tmp,self.path)
            if os.name != "nt":
                directory = os.open(os.path.dirname(self.path), os.O_RDONLY)
                try: os.fsync(directory)
                finally: os.close(directory)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def save(self,data):
        with self._file_lock():
            current = self.load()
            base = getattr(data, "base", None)
            if base is None:
                raise StateConflict("State save requires a loaded snapshot")
            merged = _merge_tree(base, plain(data), current)
            self._write(merged)
            data.clear()
            data.update(snapshot(merged))
            data.base = plain(merged)
            try: os.chmod(self.path,0o600)
            except OSError: pass
    def encrypt(self,value): return self.fernet.encrypt(value.encode()).decode()
    def decrypt(self,value): return self.fernet.decrypt(value.encode()).decode()
