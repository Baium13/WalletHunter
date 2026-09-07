"""Optimistic three-way merging for JSON state shared by bot and web workers."""
from copy import deepcopy
import json


class StateConflict(RuntimeError):
    pass


class Snapshot(dict):
    def __init__(self, value):
        super().__init__({k: snapshot(v) for k, v in value.items()})
        self.base = plain(value)


def plain(value):
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return deepcopy(value)


def snapshot(value):
    if isinstance(value, dict):
        return Snapshot(value)
    if isinstance(value, list):
        return [snapshot(v) for v in value]
    return deepcopy(value)


def merge(base, incoming, current, path="state"):
    """Keep independent changes; never silently choose between conflicting edits."""
    if incoming == base:
        return plain(current)
    if current == base or incoming == current:
        return plain(incoming)
    if all(isinstance(v, list) for v in (base, incoming, current)) and path.rsplit(".", 1)[-1] in {
        "managed", "detached_keys", "notification_message_ids", "chat_message_ids",
        "entry_block_notified", "min_notional_notified", "journal",
    }:
        key = lambda value: json.dumps(value, sort_keys=True)
        old, new, disk = ({key(v): v for v in rows} for rows in (base, incoming, current))
        removed = (old.keys() - new.keys()) | (old.keys() - disk.keys())
        combined = {**disk, **new}
        rows = [plain(v) for k, v in combined.items() if k not in removed]
        if path.endswith(".journal"):
            return sorted(rows, key=lambda v: int(v.get("time", 0)))[-200:]
        return sorted(rows)
    if all(isinstance(v, dict) for v in (base, incoming, current)):
        result = {}
        missing = object()
        for key in base.keys() | incoming.keys() | current.keys():
            old, new, disk = (v.get(key, missing) for v in (base, incoming, current))
            if new == old:
                chosen = disk
            elif disk == old or new == disk:
                chosen = new
            elif missing not in (old, new, disk):
                chosen = merge(old, new, disk, f"{path}.{key}")
            else:
                raise StateConflict(f"Concurrent state change at {path}.{key}; reload before retry")
            if chosen is not missing:
                result[key] = plain(chosen)
        return result
    raise StateConflict(f"Concurrent state change at {path}; reload before retry")
