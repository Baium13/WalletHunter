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
from tools.baseline import check_protected_source as protected
from tools.baseline import run_tests as runner
from tools.baseline import ci_checks


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


class Phase11ToolGuards(unittest.TestCase):
    """Only synthetic files/modules are loaded; no application imports."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "docs").mkdir()
        self.engine = self.root / protected.TRANSITION_PATH
        self.engine.parent.mkdir(parents=True)
        self.engine.write_bytes(b"original engine")
        self.other_name = "WalletHunterV05_final/tests/test_original.py"
        self.other = self.root / self.other_name
        self.other.parent.mkdir(parents=True)
        self.other.write_bytes(b"original test")
        self.baseline = {protected.TRANSITION_PATH: self.digest(self.engine), self.other_name: self.digest(self.other)}
        self.manifest = self.root / "docs/protected-source.json"
        self.manifest.write_text(json.dumps({"schema_version": 1, "files": self.baseline}), encoding="utf-8")
        self.original_manifest_bytes = self.manifest.read_bytes()

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def transition(self):
        self.engine.write_bytes(b"reviewed P1.1 engine")
        return {"schema_version": 1, "phase": "1.1", "files": {
            protected.TRANSITION_PATH: {"baseline_sha256": self.baseline[protected.TRANSITION_PATH],
                "reviewed_sha256": self.digest(self.engine)}}}

    def store_transition(self, value):
        (self.root / protected.TRANSITION_FILE).write_text(json.dumps(value), encoding="utf-8")

    def test_phase0_original_contract_without_transition(self):
        result = protected.check(self.root)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["phase"], "0")
        self.engine.write_bytes(b"unreviewed")
        self.assertEqual(protected.check(self.root)["changed"], [protected.TRANSITION_PATH])

    def test_ci_phase_argument_follows_explicit_transition_not_test_count(self):
        self.assertEqual(ci_checks.phase_arguments(self.root), [])
        (self.other.parent / "test_ownership_history_cache.py").write_text("# synthetic", encoding="utf-8")
        self.assertEqual(ci_checks.phase_arguments(self.root), [])
        self.store_transition(self.transition())
        self.assertEqual(ci_checks.phase_arguments(self.root), ["--phase", "1.1"])

    def test_ci_transition_failure_stops_before_test_runner(self):
        import subprocess
        self.store_transition(self.transition())
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if "tools/baseline/check_protected_source.py" in command:
                raise subprocess.CalledProcessError(1, command)

        status = "CapEff: 0\nCapPrm: 0\nCapBnd: 0\nCapAmb: 0\nNoNewPrivs: 1\n"
        with patch.object(ci_checks, "ROOT", self.root), patch("sys.argv", ["ci_checks.py"]), \
                patch.object(ci_checks.sys, "platform", "linux"), \
                patch.object(ci_checks.os, "geteuid", return_value=1000, create=True), \
                patch.dict("os.environ", {"BASELINE_OS_NETWORK_SANDBOX": "linux-network-namespace"}), \
                patch.object(ci_checks.socket, "if_nameindex", return_value=[(1, "lo")]), \
                patch.object(Path, "read_text", return_value=status), \
                patch.object(ci_checks.subprocess, "run", side_effect=run), \
                patch.object(ci_checks.subprocess, "check_output") as output:
            with self.assertRaises(subprocess.CalledProcessError):
                ci_checks.main()
            output.assert_not_called()
        self.assertEqual(len(calls), 2)
        self.assertIn("tools/baseline/check_repository.py", calls[0])
        self.assertIn("tools/baseline/check_protected_source.py", calls[1])

    def test_only_exact_reviewed_engine_passes_preserving_baseline_manifest(self):
        self.store_transition(self.transition())
        result = protected.check(self.root)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["phase"], "1.1")
        self.assertEqual(result["protected_files"], 2)
        self.assertEqual(self.manifest.read_bytes(), self.original_manifest_bytes)

    def test_further_engine_change_is_not_covered_by_prior_review(self):
        self.store_transition(self.transition())
        self.engine.write_bytes(b"additional unreviewed change")
        self.assertEqual(protected.check(self.root)["changed"], [protected.TRANSITION_PATH])

    def test_original_test_change_still_fails_with_reviewed_engine(self):
        self.store_transition(self.transition())
        self.other.write_bytes(b"changed test")
        self.assertEqual(protected.check(self.root)["changed"], [self.other_name])

    def test_transition_cannot_add_another_path_or_replace_baseline_hash(self):
        for invalid_kind in ("other_path", "old_hash", "bad_digest", "no_change", "phase", "version"):
            with self.subTest(invalid_kind=invalid_kind):
                value = self.transition()
                row = value["files"][protected.TRANSITION_PATH]
                if invalid_kind == "other_path": value["files"][self.other_name] = dict(row)
                if invalid_kind == "old_hash": row["baseline_sha256"] = "0" * 64
                if invalid_kind == "bad_digest": row["reviewed_sha256"] = "*"
                if invalid_kind == "no_change": row["reviewed_sha256"] = row["baseline_sha256"]
                if invalid_kind == "phase": value["phase"] = "2"
                if invalid_kind == "version": value["schema_version"] = True
                self.store_transition(value)
                self.assertEqual(protected.check(self.root)["status"], "FAIL")

    def test_malformed_transition_fails_closed(self):
        (self.root / protected.TRANSITION_FILE).write_text("{bad json", encoding="utf-8")
        self.assertEqual(protected.check(self.root)["status"], "FAIL")

    def test_missing_protected_file_is_not_accepted(self):
        self.store_transition(self.transition())
        self.other.unlink()
        self.assertEqual(protected.check(self.root)["changed"], [self.other_name])

    def test_selector_rejects_paths_import_paths_missing_and_duplicate_modules(self):
        location = self.other.parent
        for names in (["../core/trading_engine"], ["test_original.py"], ["tests.test_original"],
                      ["test_missing"], ["test_original", "test_original"], ["test_*"]):
            with self.subTest(names=names):
                with self.assertRaises(ValueError):
                    runner.selected_modules(location, names)
        self.assertEqual(runner.selected_modules(location, ["test_original"]), ["test_original"])

    def test_phase0_selector_is_rejected_instead_of_reporting_partial_baseline(self):
        args = runner.argument_parser().parse_args(["--python-module", "test_original"])
        with self.assertRaisesRegex(ValueError, "Phase 0 remains a full baseline"):
            runner.validate_options(args, self.root / "WalletHunterV05_final")

    def test_phase11_requires_report_and_cannot_overwrite_historical_reports(self):
        for arguments in (["--phase", "1.1"],
                          ["--phase", "1.1", "--report", str(runner.REPOSITORY / "docs/baseline-tests.json")],
                          ["--phase", "1.1", "--report", str(runner.REPOSITORY / "docs/baseline-guards.json")]):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    runner.validate_options(runner.argument_parser().parse_args(arguments), self.root / "WalletHunterV05_final")

    def test_phase11_explicit_targeted_selection_and_full_run_are_supported(self):
        common = ["--phase", "1.1", "--report", str(self.root / "p1.1.json")]
        for names in ([], ["--python-module", "test_original"]):
            args = runner.argument_parser().parse_args(common + names)
            self.assertEqual(runner.validate_options(args, self.root / "WalletHunterV05_final"), ["test_original"] if names else [])

    def test_guard_only_never_claims_to_run_selected_tests(self):
        args = runner.argument_parser().parse_args(["--phase", "1.1", "--report", str(self.root / "p1.1.json"),
                                                   "--guards-only", "--python-module", "test_original"])
        with self.assertRaisesRegex(ValueError, "guards-only"):
            runner.validate_options(args, self.root / "WalletHunterV05_final")

    def test_phase_marker_and_environment_are_not_inherited(self):
        with patch.dict("os.environ", {"WALLETHUNTER_BASELINE_PHASE": "malicious", "HL_MODE": "MAINNET", "AUTO_TRADING": "true"}):
            env = runner.safe_environment(self.root, self.root / "source", self.root / "copy", self.root / "manifest", "python", "1.1")
        self.assertEqual(env["WALLETHUNTER_BASELINE_PHASE"], "1.1")
        self.assertEqual(env["HL_MODE"], "TESTNET")
        self.assertEqual(env["AUTO_TRADING"], "false")

    def test_selected_suite_does_not_discover_unselected_module(self):
        location = self.root / "synthetic_tests"
        location.mkdir()
        name = "test_phase11_guard_synthetic_selected"
        (location / (name + ".py")).write_text("import unittest\nclass Case(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
        (location / "test_phase11_guard_must_not_import.py").write_text("raise AssertionError('unselected import')\n", encoding="utf-8")
        import sys
        before = list(sys.path)
        try:
            suite = runner.python_test_suite(location, [name])
            self.assertEqual(suite.countTestCases(), 1)
            result = unittest.TestResult()
            suite.run(result)
            self.assertTrue(result.wasSuccessful())
            self.assertNotIn("test_phase11_guard_must_not_import", sys.modules)
        finally:
            sys.path[:] = before
            sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
