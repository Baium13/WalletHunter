"""Focused stdlib tests for the repository guard; no application imports."""
import codecs
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.baseline import check_repository as guard


def synthetic_token():
    # Construct a non-secret fixture at runtime so the test source has no token.
    return "123456:" + "TEST" + "x" * 32


def rules(blob, path="notes.txt"):
    return {row["rule"] for row in guard.scan(path, blob)}


class RepositoryGuardTests(unittest.TestCase):
    def test_sensitive_paths_are_case_insensitive(self):
        for name in ("Data/state.json", ".ENV", "secret.KEY", "export.SQLITE3-WAL",
                     "CREDENTIALS/readme.txt", "x/.VENV-AUDIT/package.py", "LOGS/session.txt"):
            with self.subTest(path=name):
                self.assertTrue(guard.forbidden(name))

    def test_only_exact_reviewed_env_example_name_is_exempt(self):
        self.assertFalse(guard.forbidden("app/.env.example"))
        self.assertTrue(guard.forbidden("app/.ENV.EXAMPLE"))
        self.assertIn("telegram-token", rules(synthetic_token().encode(), "app/.env.example"))

    def test_generated_ignored_categories_cannot_be_force_staged(self):
        for name in ("venv/a.py", ".pytest_cache/README.md", "x/.mypy_cache/a.json",
                     "x/.ruff_cache/data", "htmlcov/index.html", "coverage/index.json",
                     "x/build/a.txt", "dist/app.txt", "test-results/results.json",
                     "playwright-report/index.html", "TESTS_PASSED", "x/.coverage.123",
                     "x/demo.egg-info/PKG-INFO", "x/session.lockfile", "x/edit.swp",
                     "x/edit.swo", "x/.DS_Store", "Thumbs.db", "desktop.ini",
                     "ui-audit/screen.png", "ui-user-orders-audit/screen.png"):
            with self.subTest(path=name):
                self.assertTrue(guard.forbidden(name))

    def test_historical_operational_report_exclusion_is_root_scoped(self):
        self.assertTrue(guard.forbidden("RECOVERY_AND_AI_STATUS_2026-09-06.md"))
        self.assertTrue(guard.forbidden("recovery_example.md"))
        self.assertFalse(guard.forbidden("docs/recovery_example.md"))

    def test_source_assets_and_hash_locks_remain_allowed(self):
        for name in ("app/core/storage.py", "docs/DEPENDENCIES.md", "app/requirements.lock",
                     "app/requirements-build.lock", "webapp/static/assets/skull.png"):
            with self.subTest(path=name):
                self.assertFalse(guard.forbidden(name))

    def test_plain_utf8_and_utf8_bom_are_scanned(self):
        token = synthetic_token().encode()
        for blob in (token, codecs.BOM_UTF8 + token):
            self.assertIn("telegram-token", rules(blob))

    def test_utf16_little_and_big_endian_with_bom_are_scanned(self):
        for codec, bom in (("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE)):
            with self.subTest(codec=codec):
                self.assertIn("telegram-token", rules(bom + synthetic_token().encode(codec)))

    def test_utf16_without_bom_is_scanned(self):
        for codec in ("utf-16-le", "utf-16-be"):
            with self.subTest(codec=codec):
                self.assertIn("telegram-token", rules(synthetic_token().encode(codec)))

    def test_utf32_with_and_without_bom_is_scanned(self):
        for codec, bom in (("utf-32-le", codecs.BOM_UTF32_LE), ("utf-32-be", codecs.BOM_UTF32_BE)):
            for prefix in (bom, b""):
                with self.subTest(codec=codec, bom=bool(prefix)):
                    self.assertIn("telegram-token", rules(prefix + synthetic_token().encode(codec)))

    def test_nul_prefix_does_not_skip_embedded_utf8_secret(self):
        self.assertIn("telegram-token", rules(b"\x00binary-prefix\x00" + synthetic_token().encode()))

    def test_invalid_bom_text_fails_closed(self):
        self.assertIn("invalid-text-encoding", rules(codecs.BOM_UTF16_LE + b"x"))

    def test_harmless_binary_content_is_not_automatically_a_secret(self):
        self.assertEqual(rules(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR", "assets/icon.png"), set())

    def test_findings_never_expose_the_matching_value(self):
        token = synthetic_token()
        result = guard.scan("notes.txt", token.encode())
        self.assertNotIn(token, json.dumps(result))
        self.assertEqual(result, [{"path": "notes.txt", "rule": "telegram-token"}])

    def test_exact_synthetic_exemption_is_path_and_hash_scoped(self):
        token = synthetic_token()
        allowed = {("fixture.py", "telegram-token", hashlib.sha256(token.encode()).hexdigest())}
        with patch.object(guard, "ALLOW", allowed):
            self.assertEqual(rules(token.encode(), "fixture.py"), set())
            self.assertIn("telegram-token", rules(token.encode(), "other.py"))
            self.assertIn("telegram-token", rules((token + "y").encode(), "fixture.py"))

    def test_index_parser_preserves_spaced_unicode_paths(self):
        name = "docs/пример file.txt"
        payload = ("100644 " + "a" * 40 + " 0\t" + name + "\x00").encode()
        self.assertEqual(guard.index_entries(payload), {name: [("100644", "0")]})

    def test_index_modes_reject_symlinks_gitlinks_and_conflicts(self):
        self.assertEqual(guard.index_rule([("120000", "0")]), "unreviewed-symlink")
        self.assertEqual(guard.index_rule([("160000", "0")]), "unsupported-index-mode")
        self.assertEqual(guard.index_rule([("100644", "1"), ("100644", "2")]), "unmerged-index-entry")
        self.assertIsNone(guard.index_rule([("100644", "0")]))
        self.assertIsNone(guard.index_rule([("100755", "0")]))

    def _main(self, folder, arguments, index_mode="100644", staged=b"clean", untracked=b""):
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args == ("ls-files", "--stage", "-z"):
                return (index_mode + " " + "a" * 40 + " 0\tnotes.txt\x00").encode()
            if args == ("ls-files", "-z", "--others", "--exclude-standard"):
                return untracked
            if args == ("show", ":notes.txt"):
                return staged
            raise AssertionError(args)

        output = io.StringIO()
        with patch.object(guard, "ROOT", Path(folder)), patch.object(guard, "git", fake_git), \
                patch("sys.argv", ["check_repository.py", *arguments]), redirect_stdout(output):
            failed = guard.main()
        return failed, json.loads(output.getvalue()), calls

    def test_staged_symlink_rejected_even_when_worktree_is_regular(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "notes.txt").write_text("ordinary working-tree file", encoding="utf-8")
            failed, result, calls = self._main(folder, ["--index"], index_mode="120000")
        self.assertTrue(failed)
        self.assertEqual(result["findings"], [{"path": "notes.txt", "rule": "unreviewed-symlink"}])
        self.assertNotIn(("show", ":notes.txt"), calls)

    def test_index_scans_staged_bytes_not_clean_worktree_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "notes.txt").write_text("clean working-tree file", encoding="utf-8")
            failed, result, _ = self._main(folder, ["--index"], staged=synthetic_token().encode("utf-16"))
        self.assertTrue(failed)
        self.assertEqual(result["findings"], [{"path": "notes.txt", "rule": "telegram-token"}])

    def test_candidate_mode_scans_untracked_files(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "notes.txt").write_text("clean", encoding="utf-8")
            Path(folder, "extra.txt").write_bytes(synthetic_token().encode())
            failed, result, _ = self._main(folder, ["--candidates"], untracked=b"extra.txt\x00")
        self.assertTrue(failed)
        self.assertEqual(result["files_checked"], 2)
        self.assertEqual(result["findings"], [{"path": "extra.txt", "rule": "telegram-token"}])


if __name__ == "__main__":
    unittest.main()
