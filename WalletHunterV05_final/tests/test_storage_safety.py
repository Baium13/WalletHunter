import json
import tempfile
import unittest
from copy import deepcopy

from cryptography.fernet import Fernet
from core.state_snapshot import StateConflict
from core.storage import Storage


class StorageSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.master = Fernet.generate_key()
        self.store = Storage(self.tmp.name, self.master)
        data, p = self.store.profile(1)
        p["runtime"].update({"managed": ["BTC|"], "manual_stops": {"BTC|": {"oid": 7}},
            "journal": [{"time": 50, "action": "OLD"}], "journal_cleared_at": 0,
            "chat_message_ids": [1, 2, 3], "notification_message_ids": [2, 3],
            "controller_message_id": 1, "pending_notifications": [self.item("old", 50)],
            "paper_runtime": {"managed": ["ETH|"], "positions": {"ETH|": {"size": 1}},
                              "journal": [{"time": 50, "action": "OLD_PAPER"}],
                              "pending_notifications": [self.item("old-paper", 50)]}})
        self.store.save(data)

    @staticmethod
    def item(identity, created, attempts=1):
        return {"id": identity, "created_ms": created, "result": {"action": "ERROR", "coin": identity},
                "attempts": attempts, "error": f"attempt {attempts}", "next_retry": 1000+attempts}

    def clear(self, profile=None, at=100, save_kind="profile"):
        data, p = self.store.profile(1) if profile is None else profile
        rt = p["runtime"]
        rt["journal"] = []
        rt["pending_notifications"] = []
        rt["journal_cleared_at"] = at
        rt["chat_message_ids"] = [1, 3]  # Pinned menu plus a failed deletion.
        rt["notification_message_ids"] = [3]
        rt["paper_runtime"]["journal"] = []
        rt["paper_runtime"]["pending_notifications"] = []
        if save_kind == "profile": self.store.update_profile(1, p)
        elif save_kind == "runtime": self.store.update_runtime(1, rt)
        else: self.store.save(data)

    def add_stale_and_fresh(self, p):
        rt = p["runtime"]
        rt["journal"].extend([{"time": 80, "action": "STALE"}, {"time": 101, "action": "NEW"}])
        rt["chat_message_ids"].append(4)
        rt["notification_message_ids"].append(4)
        rt["pending_notifications"].extend([self.item("stale", 80), self.item("new", 101)])
        rt["paper_runtime"]["journal"].extend([{"time": 80, "action": "STALE_PAPER"},
                                                {"time": 101, "action": "NEW_PAPER"}])
        rt["paper_runtime"]["pending_notifications"].extend([self.item("stale-paper", 80), self.item("new-paper", 101)])

    def assert_cleared_with_new(self):
        rt = self.store.profile(1)[1]["runtime"]
        self.assertEqual([e["action"] for e in rt["journal"]], ["NEW"])
        self.assertEqual(rt["chat_message_ids"], [1, 3, 4])
        self.assertEqual(rt["notification_message_ids"], [3, 4])
        self.assertEqual([e["id"] for e in rt["pending_notifications"]], ["new"])
        self.assertEqual([e["action"] for e in rt["paper_runtime"]["journal"]], ["NEW_PAPER"])
        self.assertEqual([e["id"] for e in rt["paper_runtime"]["pending_notifications"]], ["new-paper"])
        self.assertEqual(rt["paper_runtime"]["journal_cleared_at"], 100)
        self.assertEqual(rt["managed"], ["BTC|"])
        self.assertEqual(rt["manual_stops"], {"BTC|": {"oid": 7}})
        self.assertEqual(rt["paper_runtime"]["positions"], {"ETH|": {"size": 1}})

    def test_stale_engine_runtime_cannot_resurrect_cleared_events(self):
        _, stale = self.store.profile(1)
        self.clear()
        self.add_stale_and_fresh(stale)
        self.store.update_runtime(1, stale["runtime"])
        self.assert_cleared_with_new()

    def test_stale_bot_profile_cannot_resurrect_cleared_events(self):
        _, stale = self.store.profile(1)
        self.clear()
        self.add_stale_and_fresh(stale)
        stale["language"] = "en"
        self.store.update_profile(1, stale)
        self.assert_cleared_with_new()
        self.assertEqual(self.store.profile(1)[1]["language"], "en")

    def test_stale_web_whole_state_cannot_resurrect_cleared_events(self):
        data, stale = self.store.profile(1)
        self.clear()
        self.add_stale_and_fresh(stale)
        self.store.save(data)
        self.assert_cleared_with_new()

    def test_clearing_stale_snapshot_keeps_new_events_already_saved(self):
        stale = self.store.profile(1)
        _, newer = self.store.profile(1)
        self.add_stale_and_fresh(newer)
        self.store.update_runtime(1, newer["runtime"])
        self.clear(stale)
        self.assert_cleared_with_new()

    def test_clear_tombstones_work_via_every_entrypoint(self):
        for kind in ("runtime", "profile", "save"):
            with self.subTest(kind=kind):
                self.clear(at=100, save_kind=kind)
                rt = self.store.profile(1)[1]["runtime"]
                self.assertEqual(rt["chat_message_ids"], [1, 3])
                self.assertEqual(rt["message_tombstones"]["chat_message_ids"], [2])

    def test_pinned_menu_and_failed_deletions_stay_tracked(self):
        self.clear()
        _, p = self.store.profile(1)
        p["runtime"]["chat_message_ids"].append(5)
        self.store.update_profile(1, p)
        rt = self.store.profile(1)[1]["runtime"]
        self.assertEqual(rt["chat_message_ids"], [1, 3, 5])
        self.assertEqual(rt["notification_message_ids"], [3])
        self.assertNotIn(1, rt["message_tombstones"]["chat_message_ids"])
        self.assertNotIn(3, rt["message_tombstones"]["chat_message_ids"])

    def test_deleted_id_cannot_be_reinserted_by_fresh_snapshot_with_old_cache(self):
        self.clear()
        _, fresh = self.store.profile(1)
        fresh["runtime"]["chat_message_ids"].extend([2, 6])
        fresh["runtime"]["notification_message_ids"].extend([2, 6])
        self.store.update_runtime(1, fresh["runtime"])
        rt = self.store.profile(1)[1]["runtime"]
        self.assertEqual(rt["chat_message_ids"], [1, 3, 6])
        self.assertEqual(rt["notification_message_ids"], [3, 6])

    def test_repeat_clear_is_monotonic_and_cannot_lose_tombstones(self):
        _, before = self.store.profile(1)
        self.clear(at=100)
        _, p = self.store.profile(1)
        p["runtime"]["chat_message_ids"].append(4)
        self.store.update_profile(1, p)
        self.clear(at=200)
        before["runtime"]["journal_cleared_at"] = 150
        self.store.update_profile(1, before)
        rt = self.store.profile(1)[1]["runtime"]
        self.assertEqual(rt["journal_cleared_at"], 200)
        self.assertEqual(rt["chat_message_ids"], [1, 3])
        self.assertEqual(rt["message_tombstones"]["chat_message_ids"], [2, 4])

    def test_fresh_failures_after_clear_are_persisted(self):
        self.clear()
        _, p = self.store.profile(1)
        p["runtime"]["pending_notifications"].append(self.item("new", 101))
        p["runtime"]["paper_runtime"]["pending_notifications"].append(self.item("paper-new", 101))
        self.store.update_profile(1, p)
        rt = Storage(self.tmp.name, self.master).profile(1)[1]["runtime"]
        self.assertEqual(rt["pending_notifications"][0]["id"], "new")
        self.assertEqual(rt["paper_runtime"]["pending_notifications"][0]["id"], "paper-new")

    def test_legacy_or_at_cutoff_pending_items_do_not_reappear(self):
        _, stale = self.store.profile(1)
        self.clear()
        legacy = self.item("legacy", 0); legacy.pop("created_ms")
        stale["runtime"]["pending_notifications"].extend([legacy, self.item("at-cutoff", 100)])
        self.store.update_profile(1, stale)
        self.assertEqual(self.store.profile(1)[1]["runtime"]["pending_notifications"], [])

    def test_independent_queue_appends_merge_without_losing_failed_notifications(self):
        _, a = self.store.profile(1)
        _, b = self.store.profile(1)
        a["runtime"]["pending_notifications"].append(self.item("a", 60))
        b["runtime"]["pending_notifications"].append(self.item("b", 70))
        self.store.update_profile(1, a)
        self.store.update_runtime(1, b["runtime"])
        self.assertEqual({v["id"] for v in self.store.profile(1)[1]["runtime"]["pending_notifications"]}, {"old", "a", "b"})

    def test_acknowledgement_wins_over_stale_failed_retry(self):
        _, delivered = self.store.profile(1)
        _, retried = self.store.profile(1)
        delivered["runtime"]["pending_notifications"] = []
        retried["runtime"]["pending_notifications"][0]["attempts"] = 2
        self.store.update_profile(1, delivered)
        self.store.update_runtime(1, retried["runtime"])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["pending_notifications"], [])

    def test_parallel_retry_updates_keep_most_advanced_attempt(self):
        _, a = self.store.profile(1)
        _, b = self.store.profile(1)
        a["runtime"]["pending_notifications"][0] = self.item("old", 50, 3)
        b["runtime"]["pending_notifications"][0] = self.item("old", 50, 2)
        self.store.update_profile(1, a)
        self.store.update_profile(1, b)
        item = self.store.profile(1)[1]["runtime"]["pending_notifications"][0]
        self.assertEqual(item["attempts"], 3)
        self.assertEqual(item["error"], "attempt 3")

    def test_single_writer_can_reschedule_retry_earlier(self):
        _, p = self.store.profile(1)
        p["runtime"]["pending_notifications"][0]["next_retry"] = 0
        self.store.update_runtime(1, p["runtime"])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["pending_notifications"][0]["next_retry"], 0)

    def test_conflicting_queue_identity_fails_without_overwriting(self):
        _, a = self.store.profile(1)
        _, b = self.store.profile(1)
        a["runtime"]["pending_notifications"].append(self.item("collision", 60))
        other = self.item("collision", 60); other["result"]["coin"] = "DIFFERENT"
        b["runtime"]["pending_notifications"].append(other)
        self.store.update_profile(1, a)
        with self.assertRaises(StateConflict): self.store.update_profile(1, b)
        item = self.store.profile(1)[1]["runtime"]["pending_notifications"][-1]
        self.assertEqual(item["result"]["coin"], "collision")

    def test_existing_queue_identity_cannot_relabel_another_event(self):
        _, p = self.store.profile(1)
        p["runtime"]["pending_notifications"][0]["result"]["coin"] = "OTHER"
        with self.assertRaises(StateConflict): self.store.update_runtime(1, p["runtime"])

    def test_clear_and_stale_settings_preserve_new_managed_and_manual_stops(self):
        _, stale = self.store.profile(1)
        self.clear()
        _, fresh = self.store.profile(1)
        fresh["runtime"]["managed"].append("ETH|")
        fresh["runtime"]["manual_stops"]["ETH|"] = {"oid": 8}
        self.store.update_runtime(1, fresh["runtime"])
        stale["notifications"] = False
        self.store.update_profile(1, stale)
        rt = self.store.profile(1)[1]["runtime"]
        self.assertEqual(rt["managed"], ["BTC|", "ETH|"])
        self.assertEqual(rt["manual_stops"], {"BTC|": {"oid": 7}, "ETH|": {"oid": 8}})

    def test_malformed_state_is_rejected_and_never_rewritten(self):
        invalid = ["invalid json", "{}", '{"version":2}', '{"profiles":[]}',
                   '{"profiles":{"1":null}}', '{"profiles":{"1":{"runtime":[]}}}',
                   '{"profiles":{"1":{"runtime":{"journal":{}}}}}',
                   '{"profiles":{},"profiles":{}}', '{"profiles":{},"bad":NaN}',
                   '{"accounts":{}}', '{"users":"123"}', '{"users":["invalid"]}',
                   '{"accounts":[{},{}]}']
        for raw in invalid:
            with self.subTest(raw=raw):
                with open(self.store.path, "w", encoding="utf-8") as target: target.write(raw)
                with self.assertRaises((ValueError, json.JSONDecodeError)): self.store.load()
                with self.assertRaises((ValueError, json.JSONDecodeError)): self.store.profile(1)
                with open(self.store.path, encoding="utf-8") as source: self.assertEqual(source.read(), raw)

    def test_invalid_write_does_not_damage_previous_file(self):
        _, p = self.store.profile(1)
        p["runtime"]["pending_notifications"] = ["bad"]
        with self.assertRaises(ValueError): self.store.update_profile(1, p)
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], ["BTC|"])

    def test_concurrent_profiles_cannot_bind_the_same_exchange_account(self):
        self.store.profile(2)
        _, one = self.store.profile(1)
        _, two = self.store.profile(2)
        one["account"] = {"address": "0xAbC", "id": "a"}
        two["account"] = {"address": "0xabc", "id": "b"}
        self.store.update_profile(1, one)
        with self.assertRaisesRegex(StateConflict, "already bound"):
            self.store.update_profile(2, two)
        self.assertEqual(self.store.profile(1)[1]["account"]["address"], "0xAbC")
        self.assertIsNone(self.store.profile(2)[1]["account"])

    def test_existing_duplicate_bindings_remain_readable_for_explicit_repair(self):
        self.store.profile(2)
        data = self.store.load()
        for p in data["profiles"].values(): p["account"] = {"address": "0xabc"}
        with open(self.store.path, "w", encoding="utf-8") as target: json.dump(data, target)
        self.assertEqual(len(self.store.load()["profiles"]), 2)
        _, one = self.store.profile(1)
        one["language"] = "en"
        with self.assertRaises(StateConflict): self.store.update_profile(1, one)
        _, two = self.store.profile(2)
        two["account"] = None
        self.store.update_profile(2, two)
        self.assertIsNone(self.store.profile(2)[1]["account"])


if __name__ == "__main__": unittest.main()
