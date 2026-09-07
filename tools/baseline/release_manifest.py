"""Generate a commit-bound, secret-free release identity; no application import.

The committed descriptor cannot contain its own Git commit hash. This command
resolves a clean HEAD and derives a disposable manifest after the baseline commit.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], stderr=subprocess.PIPE).decode().strip()


def digest(path):
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".phase0/release-manifest.json")
    args = parser.parse_args()
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise SystemExit("Refusing release identity for dirty/non-ignored untracked source")
    manifest = json.loads((ROOT / "release-baseline.json").read_text(encoding="utf-8"))
    manifest["git_commit"] = git("rev-parse", "HEAD")
    manifest["git_tree"] = git("rev-parse", "HEAD^{tree}")
    manifest["dependency_locks"] = {path: digest(path) for path in manifest["dependency_locks"]}
    manifest["protected_source_manifest_sha256"] = digest(manifest["protected_source_manifest"])
    manifest["test_baseline_sha256"] = digest(manifest["test_baseline"])
    manifest["generation"] = "Deterministic from clean source; not evidence of a production deployment"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(args.output), "commit": manifest["git_commit"]}))


if __name__ == "__main__":
    main()
