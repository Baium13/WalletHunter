"""Safe checks only; run in ci_offline.sh's unprivileged network namespace."""
import argparse
import ast
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def phase_arguments(root):
    # main() first verifies this exact, narrowly scoped hash transition. Never
    # change the historical runner default or infer phase from test counts.
    if (root / "docs/protected-source-p1.2.json").is_file():
        return ["--phase", "1.2"]
    return ["--phase", "1.1"] if (root / "docs/protected-source-p1.1.json").is_file() else []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    if sys.platform != "linux" or os.environ.get("BASELINE_OS_NETWORK_SANDBOX") != "linux-network-namespace":
        raise SystemExit("Run this entrypoint through ci_offline.sh; no unisolated fallback")
    if os.geteuid() == 0:
        raise SystemExit("Tests must not run as root")
    # Namespace initially contains only a down loopback device. Verify that a
    # host ethernet/virtual device was not inherited before running repo code.
    interfaces = {name for _, name in socket.if_nameindex()}
    if interfaces != {"lo"}:
        raise SystemExit("Expected isolated network namespace")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    if any(int(status[name].strip(), 16) != 0 for name in ("CapEff", "CapPrm", "CapBnd", "CapAmb")) or status["NoNewPrivs"].strip() != "1":
        raise SystemExit("Test process must have no capabilities and no new privileges")
    def run(*command):
        subprocess.run(command, cwd=ROOT, check=True)
    run(args.python, "-B", "tools/baseline/check_repository.py")
    run(args.python, "-B", "tools/baseline/check_protected_source.py")
    run(args.python, "-B", "-m", "unittest", "tools.baseline.test_repository_guard")
    # Parsing never executes startup constructors or creates bytecode caches.
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    for name in filter(None, files):
        path = ROOT / name
        if path.suffix == ".py":
            ast.parse(path.read_bytes(), filename=name)
        elif path.suffix in {".js", ".cjs"}:
            run(args.node, "--check", str(path))
    run(args.python, "-B", "tools/baseline/run_tests.py", "--python", args.python,
        "--node", args.node, "--report", ".phase0/ci-tests.json", *phase_arguments(ROOT))


if __name__ == "__main__":
    main()
