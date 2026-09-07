"""Backup correctness using disposable state, fake secrets and local SQLite."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch


class BackupRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("data", "desktop", "backups"):
            (self.root/name).mkdir()
        self.state = b'{"version":2,"profiles":{"1":{"account":{"private_key":"gAAAAOfflineCiphertext"},"runtime":{"managed":["BTC|"]}}}}\n'
        (self.root/"data"/"state.json").write_bytes(self.state)
        self.keys = b"MASTER_KEY=offline-not-a-real-key\nTELEGRAM_BOT_TOKEN=offline-test-token\n"
        (self.root/".env").write_bytes(self.keys)
        self.databases = ["data/executions.sqlite3", "data/ai_shadow.sqlite3", "desktop/bot.session"]
        for index, relative in enumerate(self.databases):
            with closing(sqlite3.connect(self.root/relative)) as db:
                db.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)")
                db.execute("INSERT INTO records(value) VALUES(?)", (f"offline-{index}",))
                db.commit()
        source = Path(__file__).resolve().parents[1]/"scripts"/"backup_runtime.py"
        spec = importlib.util.spec_from_file_location("offline_backup_script", source)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.ROOT = self.root
        # run() is normally a separate process. Restore its umask when called
        # in a test so other tests inherit their original permissions policy.
        old_mask = os.umask(0o077)
        os.umask(old_mask)
        self.addCleanup(lambda: os.umask(old_mask))

    def run_backup(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.module.run()
        result = json.loads(output.getvalue())
        return self.root/"backups"/result["backup"], result

    def test_archive_preserves_state_bytes_and_contains_database_and_keys(self):
        output, summary = self.run_backup()
        self.assertEqual(summary["old_backups_deleted"], 0)
        self.assertEqual(summary["databases"], 3)
        with tarfile.open(output, "r:gz") as archive:
            self.assertEqual(set(archive.getnames()), {"data/state.json", ".env", *self.databases})
            self.assertEqual(archive.extractfile("data/state.json").read(), self.state)
            self.assertEqual(archive.extractfile(".env").read(), self.keys)
            for index, relative in enumerate(self.databases):
                check = self.root/f"check-{index}.sqlite3"
                check.write_bytes(archive.extractfile(relative).read())
                with closing(sqlite3.connect(check)) as db:
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(db.execute("SELECT value FROM records").fetchone()[0], f"offline-{index}")

    def test_preexisting_old_archives_are_never_removed(self):
        old = self.root/"backups"/"previous-user-backup.tar.gz"
        old.write_bytes(b"unchanged older backup")
        self.run_backup()
        self.assertEqual(old.read_bytes(), b"unchanged older backup")

    def test_backups_created_in_same_second_do_not_overwrite_each_other(self):
        fixed = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
        with patch.object(self.module, "datetime") as clock:
            clock.now.return_value = fixed
            first, _ = self.run_backup()
            original = first.read_bytes()
            state2 = self.state.replace(b"BTC|", b"ETH|")
            (self.root/"data"/"state.json").write_bytes(state2)
            second, _ = self.run_backup()
        self.assertNotEqual(first, second, "Same-second run overwrote an earlier backup")
        self.assertEqual(first.read_bytes(), original)
        with tarfile.open(first, "r:gz") as archive:
            self.assertEqual(archive.extractfile("data/state.json").read(), self.state)
        with tarfile.open(second, "r:gz") as archive:
            self.assertEqual(archive.extractfile("data/state.json").read(), state2)

    def test_corrupt_sqlite_leaves_no_completed_or_partial_new_archive(self):
        old = self.root/"backups"/"previous.tar.gz"
        old.write_bytes(b"retain me")
        (self.root/"data"/"corrupt.sqlite3").write_bytes(b"not a sqlite database")
        with self.assertRaises((sqlite3.DatabaseError, RuntimeError)):
            self.run_backup()
        self.assertEqual(list((self.root/"backups").iterdir()), [old])
        self.assertEqual(old.read_bytes(), b"retain me")

    def test_corrupt_json_leaves_no_completed_or_partial_archive(self):
        (self.root/"data"/"state.json").write_bytes(b"{broken")
        with self.assertRaises((json.JSONDecodeError, ValueError)):
            self.run_backup()
        self.assertEqual(list((self.root/"backups").iterdir()), [])

    def test_invalid_state_shape_is_not_published_as_valid_backup(self):
        (self.root/"data"/"state.json").write_bytes(b"[]")
        with self.assertRaises(ValueError):
            self.run_backup()
        self.assertEqual(list((self.root/"backups").iterdir()), [])

    def test_sqlite_backup_includes_committed_wal_rows(self):
        path = self.root/"data"/"wal-test.sqlite3"
        with closing(sqlite3.connect(path)) as source:
            self.assertEqual(source.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            source.execute("CREATE TABLE wal_rows(value TEXT)")
            source.execute("INSERT INTO wal_rows VALUES('committed offline WAL row')")
            source.commit()
            self.assertTrue(Path(str(path)+"-wal").exists())
            output, summary = self.run_backup()
            self.assertEqual(summary["databases"], 4)
            with tarfile.open(output, "r:gz") as archive:
                restored = self.root/"wal-restored.sqlite3"
                restored.write_bytes(archive.extractfile("data/wal-test.sqlite3").read())
            with closing(sqlite3.connect(restored)) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("SELECT value FROM wal_rows").fetchone()[0], "committed offline WAL row")

    @unittest.skipIf(os.name == "nt", "POSIX archive permissions are verified on Linux")
    def test_archive_and_sensitive_members_have_owner_only_permissions(self):
        os.chmod(self.root/".env", 0o644)
        output, _ = self.run_backup()
        self.assertEqual(output.stat().st_mode & 0o077, 0)
        with tarfile.open(output, "r:gz") as archive:
            for member in archive.getmembers():
                self.assertEqual(member.mode & 0o077, 0, member.name)


if __name__ == "__main__":
    unittest.main()
