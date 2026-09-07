"""Secret-safe Git candidate/index checks; never import the application.

Only path/rule names are reported, never matching values or source lines.
This is a focused guard, not a claim to recognize every possible secret format.
"""
from __future__ import annotations

import argparse
import codecs
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
DENIED_DIRS = {"data", "runtime", "backups", "logs", ".ssh", "secrets", "credentials",
               "__pycache__", "node_modules", ".idea", ".vscode", ".phase0", "venv",
               ".pytest_cache", ".mypy_cache", ".ruff_cache", "htmlcov", "coverage",
               "build", "dist", "test-results", "playwright-report", "ui-audit",
               "ui-user-orders-audit"}
DENIED_NAMES = {"tests_passed", ".ds_store", "thumbs.db", "desktop.ini"}
DENIED_SUFFIX = re.compile(r"\.(?:key|pem|p12|pfx|jks|keystore|crt|cer|session|db|sqlite3?)(?:$|[-.])|"
                           r"\.(?:tar(?:\.gz)?|tgz|zip|7z|log|pyc|pyo|pyd|tmp|temp|partial|lockfile|swp|swo)$|\.(?:backup|bak)")
HISTORICAL_REPORT = re.compile(r"^(?:ai_.*_ru|audit_.*_ru|recovery_.*|release_report_.*_ru)\.md$")
RULES = {
    "private-key-header": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "telegram-token": re.compile(r"\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "encrypted-credential": re.compile(r"\bgAAAAA[A-Za-z0-9_-]{70,}={0,2}"),
    "literal-private-key": re.compile(r"(?i)(?:private_key|api_secret|wallet_secret)\s*[=:]\s*[\"'](?:0x)?[0-9a-f]{64}[\"']"),
}
# Reviewed synthetic HMAC fixture, contains a TEST marker; exact value hash only.
# A changed token or the same token in another file is NOT automatically allowed.
ALLOW = {("WalletHunterV05_final/tests/test_api_safety.py", "telegram-token",
          "cc9d0ee80974453a8118361575f6623b060aacaf8dd2cef54b36c0f1e2be6ce1")}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], stderr=subprocess.PIPE)


def forbidden(path):
    item = PurePosixPath(path)
    parts = tuple(part.casefold() for part in item.parts)
    name = item.name.casefold()
    return (any(part in DENIED_DIRS or part.startswith(".venv") or part.startswith("ui-audit-")
                or part.endswith(".egg-info") for part in parts)
            or (name.startswith(".env") and item.name != ".env.example")
            or name in DENIED_NAMES or name.startswith(".coverage")
            or bool(DENIED_SUFFIX.search(name))
            or (len(parts) == 1 and bool(HISTORICAL_REPORT.fullmatch(name))))


def text_variants(blob):
    """Scan normal text and UTF-16/32, including BOM-less/NUL-containing text.

    An arbitrary binary blob is not proof of safety. Keep the UTF-8 view even
    when NUL bytes are present, and examine strict wide-character decodings.
    A recognized but malformed BOM encoding is an explicit failure.
    """
    for bom, encoding in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                          (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
                          (codecs.BOM_UTF8, "utf-8-sig")):
        if blob.startswith(bom):
            try:
                return [blob.decode(encoding)], False
            except UnicodeError:
                return [blob.decode("utf-8", errors="replace")], True
    variants = [blob.decode("utf-8", errors="replace")]
    if b"\x00" in blob:
        for encoding in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            try:
                variants.append(blob.decode(encoding))
            except UnicodeError:
                pass
    return variants, False


def index_entries(blob):
    """Use staged Git modes, not a possibly different working-tree file type."""
    entries = {}
    for record in blob.split(b"\x00"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, _object_id, stage = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        entries.setdefault(path, []).append((mode, stage))
    return entries


def index_rule(entries):
    if any(mode == "120000" for mode, _ in entries):
        return "unreviewed-symlink"
    if any(stage != "0" for _, stage in entries):
        return "unmerged-index-entry"
    if any(mode not in {"100644", "100755"} for mode, _ in entries):
        return "unsupported-index-mode"
    return None


def scan(path, blob):
    findings = []
    if forbidden(path):
        findings.append({"path": path, "rule": "sensitive-runtime-path"})
    variants, invalid_encoding = text_variants(blob)
    if invalid_encoding:
        findings.append({"path": path, "rule": "invalid-text-encoding"})
    seen = set()
    for text in variants:
        for name, pattern in RULES.items():
            for match in pattern.finditer(text):
                digest = hashlib.sha256(match.group().encode()).hexdigest()
                if (path, name, digest) not in ALLOW and name not in seen:
                    findings.append({"path": path, "rule": name})
                    seen.add(name)
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", action="store_true", help="Also inspect non-ignored untracked files")
    parser.add_argument("--index", action="store_true", help="Inspect staged content, not working-tree content")
    args = parser.parse_args()
    if args.candidates and args.index:
        parser.error("Choose candidates or index")
    indexed = index_entries(git("ls-files", "--stage", "-z"))
    paths = set(indexed)
    if args.candidates:
        paths.update(set(git("ls-files", "-z", "--others", "--exclude-standard").decode().split("\0")) - {""})
    paths = sorted(paths)
    findings = []
    if not paths:
        findings.append({"path": "<index>", "rule": "no-source-files"})
    for path in paths:
        local = ROOT / path
        staged_rule = index_rule(indexed.get(path, []))
        if staged_rule:
            findings.append({"path": path, "rule": staged_rule})
            continue
        if not args.index and local.is_symlink():
            findings.append({"path": path, "rule": "unreviewed-symlink"})
            continue
        blob = git("show", ":" + path) if args.index else local.read_bytes()
        findings.extend(scan(path, blob))
    result = {"status": "PASS" if not findings else "FAIL", "files_checked": len(paths),
              "mode": "index" if args.index else "candidates" if args.candidates else "tracked",
              "findings": findings, "reviewed_synthetic_exemptions": len(ALLOW),
              "limitations": "Pattern checks and prohibited paths; not a proof that arbitrary data contains no secrets."}
    print(json.dumps(result, indent=2))
    return bool(findings)


if __name__ == "__main__":
    sys.exit(main())
