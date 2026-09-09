"""Guard self-checks run before application discovery; all credentials are fake."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import unittest


class SandboxGuards(unittest.TestCase):
    def test_guard_active_and_environment_is_safe(self):
        import sitecustomize
        self.assertTrue(sitecustomize.ACTIVE)
        self.assertEqual(os.environ["HL_MODE"], "TESTNET")
        self.assertEqual(os.environ["AUTO_TRADING"], "false")
        self.assertEqual(os.environ["PYTHON_DOTENV_DISABLED"], "1")
        self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "000000000:baseline-fake-token")
        self.assertFalse(any(k.upper() in {"HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","SSH_AUTH_SOCK"} for k in os.environ))

    def test_copy_contains_only_allowlisted_files_no_runtime_secrets(self):
        root = Path(os.environ["WALLETHUNTER_BASELINE_APP"])
        files = json.loads(Path(os.environ["WALLETHUNTER_BASELINE_MANIFEST"]).read_text())
        phase = os.environ["WALLETHUNTER_BASELINE_PHASE"]
        self.assertIn(phase, {"0", "1.1", "1.2", "1", "block1"})
        self.assertEqual(len(list((root/"tests").glob("test_*.py"))), {"0": 46, "1.1": 47, "1.2": 48, "1": 48, "block1": 63}[phase])
        if phase == "block1": self.assertIn("tests/test_journal_bridge.py", files)
        if phase == "block1": self.assertIn("tests/test_foundation.py", files)
        if phase in {"1.1", "1.2", "1", "block1"}:
            self.assertIn("tests/test_ownership_history_cache.py", files)
        if phase in {"1.2", "1", "block1"}:
            self.assertIn("tests/test_source_allocation.py", files)
        self.assertEqual(len(list((root/"tests").glob("*.cjs"))), 8 if phase == "block1" else 6)
        for name in files:
            p = Path(name)
            self.assertNotIn("data", p.parts)
            self.assertNotIn("backups", p.parts)
            self.assertNotIn("__pycache__", p.parts)
            self.assertNotEqual(p.name, ".env")
            self.assertNotIn(p.suffix.lower(), {".key", ".session", ".sqlite3", ".gz", ".zip", ".pem"})
        self.assertFalse((root/"data").exists())

    def test_original_application_files_cannot_be_opened(self):
        with self.assertRaisesRegex(PermissionError, "BASELINE_DENIED_FILESYSTEM"):
            (Path(os.environ["WALLETHUNTER_BASELINE_SOURCE"])/"core"/"settings.py").read_text()

    def test_original_sqlite_uri_cannot_bypass_filesystem_guard(self):
        target=Path(os.environ["WALLETHUNTER_BASELINE_SOURCE"])/"data"/"must-never-be-opened.sqlite3"
        with self.assertRaisesRegex(PermissionError,"BASELINE_DENIED_FILESYSTEM"):
            sqlite3.connect(target.as_uri()+"?mode=ro",uri=True)

    def test_subprocess_and_shell_are_blocked(self):
        with self.assertRaisesRegex(PermissionError, "BASELINE_DENIED_PROCESS"):
            subprocess.run([sys.executable, "-c", "raise AssertionError('must never execute')"])
        with self.assertRaisesRegex(PermissionError, "BASELINE_DENIED_PROCESS"):
            os.system("baseline-command-must-never-execute")

    def test_direct_network_and_dns_are_blocked(self):
        with self.assertRaisesRegex(PermissionError, "BASELINE_DENIED_NETWORK"):
            socket.getaddrinfo("api.hyperliquid.xyz", 443)
        with socket.socket() as sock:
            with self.assertRaisesRegex(PermissionError, "BASELINE_DENIED_NETWORK"):
                sock.connect(("127.0.0.1", 9))

    def test_asyncio_local_self_pipe_remains_available(self):
        async def local_only(): return 42
        self.assertEqual(asyncio.run(local_only()), 42)

    def test_requests_to_both_exchange_origins_cannot_leave_process(self):
        import requests
        import sitecustomize
        for base in ("https://api.hyperliquid.xyz", "https://api.hyperliquid-testnet.xyz"):
            before = sitecustomize.DENIED["network"]
            with self.assertRaises(Exception):
                requests.post(base+"/info", json={"type":"meta"}, timeout=.1)
            self.assertGreater(sitecustomize.DENIED["network"], before)

    def test_accidental_public_sdk_constructor_cannot_connect(self):
        from hyperliquid.info import Info
        import sitecustomize
        before = sitecustomize.DENIED["network"]
        with self.assertRaises(Exception):
            Info("https://api.hyperliquid-testnet.xyz", skip_ws=True)
        self.assertGreater(sitecustomize.DENIED["network"], before)


if __name__ == "__main__": unittest.main()
