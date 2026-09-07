"""Verify Phase 0 did not alter application, existing tests or operational code."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    expected = json.loads((ROOT / "docs/protected-source.json").read_text(encoding="utf-8"))["files"]
    changed = [name for name, digest in expected.items()
               if not (ROOT / name).is_file() or hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
    print(json.dumps({"status": "FAIL" if changed else "PASS", "protected_files": len(expected), "changed": changed}, indent=2))
    return bool(changed)


if __name__ == "__main__":
    sys.exit(main())
