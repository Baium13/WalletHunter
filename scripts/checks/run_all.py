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


def interpreter():
    """The service venv, when the system python cannot import the deps.

    Running these with the system python3 produced three identical pydantic
    tracebacks and no hint that the interpreter was the problem. Say it once,
    and use the venv if the deployment has one.
    """
    try:
        import pydantic  # noqa: F401
        return sys.executable
    except ImportError:
        pass
    for candidate in (ROOT / '.venv/bin/python', ROOT / 'venv/bin/python'):
        if candidate.is_file():
            print('note: using %s (the system python cannot import pydantic)' % candidate)
            return str(candidate)
    print('The dependencies are not importable with %s and no .venv was found '
          'next to the tree. Run this with the interpreter the service uses.' % sys.executable)
    sys.exit(2)


def main():
    python = interpreter()
    failed = []
    for script in sorted(HERE.glob('check_*.py')):
        print('=' * 72)
        print(script.name)
        print('=' * 72)
        result = subprocess.run([python, str(script)], cwd=ROOT)
        if result.returncode: failed.append(script.name)
    print('=' * 72)
    if failed:
        print('FAILED: ' + ', '.join(failed))
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
