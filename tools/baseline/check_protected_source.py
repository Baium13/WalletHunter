"""Verify original protected hashes plus the one explicitly reviewed P1.1 edit."""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
TRANSITION_FILE = "docs/protected-source-p1.1.json"
TRANSITION_PATH = "WalletHunterV05_final/core/trading_engine.py"


def reviewed_transition(expected, document):
    """No wildcard, alternate path, or caller-supplied exception is supported."""
    if (not isinstance(document, dict) or set(document) != {"schema_version", "phase", "files"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or document["phase"] != "1.1"):
        raise ValueError("invalid_p1.1_transition_schema")
    files = document["files"]
    if not isinstance(files, dict) or set(files) != {TRANSITION_PATH}:
        raise ValueError("p1.1_transition_must_only_name_trading_engine")
    row = files[TRANSITION_PATH]
    if not isinstance(row, dict) or set(row) != {"baseline_sha256", "reviewed_sha256"}:
        raise ValueError("invalid_p1.1_hash_transition")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in row.values()):
        raise ValueError("invalid_p1.1_sha256")
    if expected.get(TRANSITION_PATH) != row["baseline_sha256"]:
        raise ValueError("p1.1_baseline_hash_mismatch")
    if row["reviewed_sha256"] == row["baseline_sha256"]:
        raise ValueError("p1.1_transition_has_no_change")
    return row


def check(root):
    expected = json.loads((root / "docs/protected-source.json").read_text(encoding="utf-8"))["files"]
    effective = dict(expected)
    report = {"status": "PASS", "phase": "0", "protected_files": len(expected), "changed": []}
    transition = root / TRANSITION_FILE
    if transition.exists() or transition.is_symlink():
        try:
            if transition.is_symlink():
                raise ValueError("p1.1_transition_symlink_not_allowed")
            row = reviewed_transition(expected, json.loads(transition.read_text(encoding="utf-8")))
        except (ValueError, OSError) as exc:
            report.update(status="FAIL", reason=str(exc))
            return report
        effective[TRANSITION_PATH] = row["reviewed_sha256"]
        report.update(phase="1.1", reviewed_transitions={TRANSITION_PATH: row})
    report["changed"] = [name for name, digest in effective.items()
                         if (root / name).is_symlink() or not (root / name).is_file()
                         or hashlib.sha256((root / name).read_bytes()).hexdigest() != digest]
    if report["changed"]:
        report["status"] = "FAIL"
    return report


def main():
    report = check(ROOT)
    print(json.dumps(report, indent=2))
    return report["status"] != "PASS"


if __name__ == "__main__":
    sys.exit(main())
