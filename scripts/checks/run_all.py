"""Run every offline check. No network, no credentials, no live store touched.

    python3 scripts/checks/run_all.py

Each script exits non-zero on the first failed assertion and prints the value
that failed, so a break names itself rather than needing a bisect.
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def main():
    failed = []
    for script in sorted(HERE.glob('check_*.py')):
        print('=' * 72)
        print(script.name)
        print('=' * 72)
        result = subprocess.run([sys.executable, str(script)], cwd=ROOT)
        if result.returncode: failed.append(script.name)
    print('=' * 72)
    if failed:
        print('FAILED: ' + ', '.join(failed))
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
