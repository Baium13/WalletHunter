"""Verify original protected hashes and narrowly reviewed, chained phase edits."""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
TRANSITION_FILE = "docs/protected-source-p1.1.json"
TRANSITION_PATH = "WalletHunterV05_final/core/trading_engine.py"
P12_TRANSITION_FILE = "docs/protected-source-p1.2.json"
P12_NEW_PATH = "WalletHunterV05_final/core/source_allocation.py"
P12_PATHS = {TRANSITION_PATH, "WalletHunterV05_final/core/execution_journal.py", P12_NEW_PATH,
             "WalletHunterV05_final/tests/test_engine_safety.py"}
P12_BASELINE_SHA256 = "6f37f3758c3848ad02e58680fb3c6e75b991a5903c276ab1eb90f279ebf10d97"
P12_PARENT_SHA256 = "7e302162316f26082b1b3ef91efb7386e2029a92e7304d71446be9844dfc99e3"
P11_REGRESSION_PATH = "WalletHunterV05_final/tests/test_ownership_history_cache.py"
P11_REGRESSION_SHA256 = "059ec37273dd9d72f3d6afa49eb785e70581f862f20c24e2c1ebc7d3b62a135f"


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


def reviewed_phase12_transition(previous, document, baseline_digest, parent_digest):
    fields = {"schema_version", "phase", "baseline_manifest_sha256", "parent_manifest_sha256", "files"}
    if (not isinstance(document, dict) or set(document) != fields
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or document["phase"] != "1.2"):
        raise ValueError("invalid_p1.2_transition_schema")
    if (baseline_digest != P12_BASELINE_SHA256 or parent_digest != P12_PARENT_SHA256
            or document["baseline_manifest_sha256"] != baseline_digest
            or document["parent_manifest_sha256"] != parent_digest):
        raise ValueError("p1.2_immutable_manifest_chain_mismatch")
    files = document["files"]
    if not isinstance(files, dict) or set(files) != P12_PATHS:
        raise ValueError("p1.2_transition_must_only_name_approved_paths")
    for name, row in files.items():
        if not isinstance(row, dict) or set(row) != {"previous_sha256", "reviewed_sha256"}:
            raise ValueError("invalid_p1.2_hash_transition")
        digest = row["reviewed_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid_p1.2_reviewed_sha256")
        if name == P12_NEW_PATH:
            if name in previous or row["previous_sha256"] is not None:
                raise ValueError("p1.2_new_file_must_have_no_previous_hash")
        elif name not in previous or row["previous_sha256"] != previous[name]:
            raise ValueError("p1.2_previous_hash_mismatch")
        if row["previous_sha256"] == digest:
            raise ValueError("p1.2_transition_has_no_change")
    return files


def check(root):
    baseline_path = root / "docs/protected-source.json"
    baseline_bytes = baseline_path.read_bytes()
    expected = json.loads(baseline_bytes)["files"]
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
    phase12 = root / P12_TRANSITION_FILE
    if phase12.exists() or phase12.is_symlink():
        try:
            if baseline_path.is_symlink() or phase12.is_symlink():
                raise ValueError("p1.2_manifest_symlink_not_allowed")
            if report["phase"] != "1.1":
                raise ValueError("p1.2_requires_valid_p1.1_parent")
            rows = reviewed_phase12_transition(effective, json.loads(phase12.read_bytes()),
                hashlib.sha256(baseline_bytes).hexdigest(), hashlib.sha256(transition.read_bytes()).hexdigest())
        except (ValueError, OSError) as exc:
            report.update(status="FAIL", reason=str(exc))
            return report
        effective.update({name: row["reviewed_sha256"] for name, row in rows.items()})
        effective[P11_REGRESSION_PATH] = P11_REGRESSION_SHA256
        report.update(phase="1.2", reviewed_transitions=rows,
            transition_chain=[{"phase": "1.1", "files": {TRANSITION_PATH: row}}, {"phase": "1.2", "files": rows}],
            effective_protected_files=len(effective))
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
