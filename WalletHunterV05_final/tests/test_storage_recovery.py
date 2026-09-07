import json
import os
import tempfile
import unittest
from cryptography.fernet import Fernet
from core.storage import Storage
from core.state_snapshot import StateConflict


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = Fernet.generate_key()
        self.store = Storage(self.directory.name, self.key)
        self.store.profile(1)

    def test_reload_keeps_ownership(self):
        _, p = self.store.profile(1)
        p["runtime"]["managed"] = ["BTC|"]
        self.store.update_runtime(1, p["runtime"])
        restarted = Storage(self.directory.name, self.key)
        self.assertEqual(restarted.profile(1)[1]["runtime"]["managed"], ["BTC|"])

    def test_stale_runtime_does_not_erase_new_ownership(self):
        _, first = self.store.profile(1)
        _, stale = self.store.profile(1)
        first["runtime"]["managed"] = ["ETH|"]
        self.store.update_runtime(1, first["runtime"])
        stale["runtime"]["managed"] = ["BTC|"]
        self.store.update_runtime(1, stale["runtime"])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], ["BTC|", "ETH|"])

    def test_new_message_ids_after_clear_are_saved(self):
        data, p = self.store.profile(1)
        p["runtime"].update(journal_cleared_at=100, chat_message_ids=[1])
        self.store.save(data)
        _, p = self.store.profile(1)
        p["runtime"]["chat_message_ids"].append(2)
        self.store.update_runtime(1, p["runtime"])
        self.assertEqual(self.store.profile(1)[1]["runtime"]["chat_message_ids"], [1, 2])

    def test_independent_profile_updates_survive(self):
        a, pa = self.store.profile(1)
        b, pb = self.store.profile(1)
        pa["language"] = "en"
        self.store.save(a)
        pb["notifications"] = False
        self.store.save(b)
        p = self.store.profile(1)[1]
        self.assertEqual(p["language"], "en")
        self.assertFalse(p["notifications"])

    def test_conflicting_settings_fail_without_overwrite(self):
        a, pa = self.store.profile(1)
        b, pb = self.store.profile(1)
        pa["max_leverage"] = 5
        pb["max_leverage"] = 20
        self.store.save(a)
        with self.assertRaises(StateConflict): self.store.save(b)
        self.assertEqual(self.store.profile(1)[1]["max_leverage"], 5)

    def test_corrupt_file_is_not_silently_reset(self):
        with open(self.store.path, "w") as target: target.write("invalid json")
        with self.assertRaises(json.JSONDecodeError): self.store.load()

    def test_stale_profile_save_preserves_runtime(self):
        _, stale = self.store.profile(1)
        _, fresh = self.store.profile(1)
        fresh["runtime"]["managed"] = ["BTC|"]
        self.store.update_runtime(1, fresh["runtime"])
        stale["language"] = "en"
        self.store.update_profile(1, stale)
        self.assertEqual(self.store.profile(1)[1]["runtime"]["managed"], ["BTC|"])


if __name__ == "__main__": unittest.main()
