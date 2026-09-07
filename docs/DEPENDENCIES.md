# Dependency baseline — Phase 0

This is a reproducibility baseline for the **local audited checkout**, not an inventory of the Oracle server and not a security certification. No application module was imported to discover dependencies. No production connection, trade, or existing virtual environment was changed.

## Scope and provenance

The starting interpreter was CPython **3.12.14**, Windows AMD64. The existing application `.venv-audit/pyvenv.cfg` has `include-system-site-packages = true`; consequently `pip freeze` would include unrelated bundled software. Instead, `importlib.metadata` was used to follow only the required dependency graph from the project's imports and requirements. Environment markers were evaluated for CPython 3.12 on Windows and Linux, with no optional package extras enabled. All active dependency constraints matched the installed versions.

The resulting graph has **45 runtime packages**, **2 additional test packages**, and **4 build/bootstrap packages**. Every version was preserved from that local environment; no version upgrade or downgrade was performed. The sole input-requirements addition is `pydantic>=2.9,<3`, already imported directly by `webapp/server.py`; previously it was present only as a transitive dependency.

| Direct dependency | Locked version | Use |
| --- | --- | --- |
| telethon | 1.44.0 | Telegram client |
| python-dotenv | 1.2.3 | Existing configuration loading |
| hyperliquid-python-sdk | 0.24.0 | Exchange adapter |
| eth-account | 0.13.7 | Signing primitives |
| cryptography | 47.0.0 | Existing encrypted storage |
| requests | 2.34.2 | Public HTTP reads |
| fastapi | 0.141.1 | App API |
| uvicorn | 0.52.4 | ASGI server entry point |
| pydantic | 2.13.5 | API request validation |
| httpx (tests only) | 0.28.1 | FastAPI TestClient transport |

Python tests use the standard-library `unittest`; pytest is neither required nor added.

## Files and installation policy

- `WalletHunterV05_final/requirements.txt` and `requirements-dev.txt` remain human-maintained direct-dependency inputs. Their broad ranges are **not** the reproducible installation path.
- `WalletHunterV05_final/requirements.lock` pins the complete runtime graph and SHA-256 artifact hashes.
- `WalletHunterV05_final/requirements-dev.lock` includes the runtime lock and pins HTTPX/HTTPCore.
- `WalletHunterV05_final/requirements-build.lock` pins pip 25.0.1, setuptools 84.0.0, wheel 0.48.0, and packaging 26.3.
- `docs/dependency-inventory.json` records versions, active metadata edges, direct/transitive roles, approved artifact names, upstream URLs, SHA-256 hashes and target mapping. It intentionally contains no account, key, environment-variable or absolute home-directory inventory.

Hashes came from each exact release's official `https://pypi.org/pypi/{name}/{version}/json` metadata. Selected wheels cover CPython 3.12 on **Windows AMD64**, **Linux x86_64**, and **Linux aarch64**, using a Linux glibc 2.35 compatibility baseline (Ubuntu 22.04). The selected cryptography wheel requires glibc 2.34 or newer. Native macOS, Alpine/musl, 32-bit, other Python minor versions and older glibc systems are not covered by this lock. Compatible newer glibc may work but still needs its own execution check.

Only `pyaes==1.6.1` is source-only. Its upstream tarball is hash-pinned. The lock permits source installation **only for pyaes**; all other packages must use wheels. Install the build lock first and use `--no-build-isolation` for application dependencies, so pip cannot silently download an unpinned build backend. The built pyaes wheel is a local derived artifact: its hash is not claimed to be reproducible byte-for-byte. The source hash, build-tool versions and resulting package version are the baseline.

## Fresh local environment

Run from the repository root, choosing an existing trusted **3.12.14** interpreter. Do not activate or mutate `.venv-audit`, and do not use `--system-site-packages`. Downloads, pip cache and this disposable environment belong under the ignored `.phase0/` directory.

Windows PowerShell (the existing audit interpreter may be used only to create the new environment):

```powershell
& .\WalletHunterV05_final\.venv-audit\Scripts\python.exe -B -m venv .phase0/venv
& .\.phase0\venv\Scripts\python.exe -B -m pip --isolated --disable-pip-version-check --cache-dir .phase0/pip-cache install --require-hashes -r WalletHunterV05_final/requirements-build.lock
& .\.phase0\venv\Scripts\python.exe -B -m pip --isolated --disable-pip-version-check --cache-dir .phase0/pip-cache install --require-hashes --no-build-isolation -r WalletHunterV05_final/requirements-dev.lock
& .\.phase0\venv\Scripts\python.exe -B -m pip --isolated check
```

Linux provisioning (separate from the network-disabled test stage):

```sh
python3.12 -B -c 'import sys; assert sys.version_info[:3] == (3, 12, 14), sys.version'
python3.12 -B -m venv .phase0/venv
.phase0/venv/bin/python -B -m pip --isolated --disable-pip-version-check --cache-dir .phase0/pip-cache install --require-hashes -r WalletHunterV05_final/requirements-build.lock
.phase0/venv/bin/python -B -m pip --isolated --disable-pip-version-check --cache-dir .phase0/pip-cache install --require-hashes --no-build-isolation -r WalletHunterV05_final/requirements-dev.lock
.phase0/venv/bin/python -B -m pip --isolated check
```

Network access for package provisioning does not authorize running the bot, importing its startup entry points or contacting Telegram/Hyperliquid. Execute the project's separately maintained Phase 0 test harness only after provisioning, with its sandbox/network controls. Do not run `desktop/main.py`, `uvicorn webapp.server:app`, deployment or recovery scripts as dependency checks.

## Python and JavaScript availability

Python 3.12.14 is an official security release dated 2026-08-12, distributed by python.org as source rather than Windows/macOS installers. [Official release](https://www.python.org/downloads/release/python-31214/). The [actions/python-versions manifest](https://github.com/actions/python-versions/blob/main/versions-manifest.json) was also checked and lists 3.12.14 Linux builds for Ubuntu 22.04/24.04/26.04, x64 and arm64. This confirms availability, not that those environments were executed locally. CI must select the exact interpreter/container version and independently pin its image/action trust inputs.

The six `tests/ui_*_test.cjs` suites use only Node built-ins (`node:test`, `assert`, `fs`, `path`, `vm`); there is no npm application dependency graph to freeze. The observed local Node version is **24.19.0**. Optional `scripts/ui_*_visual_audit.cjs` scripts use externally supplied **Playwright 1.62.1** and a browser. They are outside the baseline test installation: no browser or npm package was installed, and their browser revisions are not pinned here. Do not silently add those scripts to a supposedly browser-free/offline baseline.

The runtime page also loads Telegram's remote Web App script. That browser-delivered service dependency is not controlled by Python locks and must be mocked/blocked by offline tests. No runtime HTML was changed in Phase 0.

## Verification record and limits

Verified on 2026-09-07:

- Fresh Windows CPython 3.12.14 environment at `.phase0/venv`, with `include-system-site-packages = false`.
- Installation from all three hash locks succeeded, including pyaes built from its approved source using the locked build tools.
- Metadata comparison: exactly **51 expected packages**, **0 version mismatches**, **0 unrelated packages**. `pip check`: **No broken requirements found.**
- Linux x86_64 and aarch64: `pip download --require-hashes --no-deps --no-build-isolation`, with explicit CPython 3.12/manylinux target tags, downloaded **47 artifacts per target: 46 wheels plus the pyaes source**. Actual downloaded wheel METADATA was then checked against the target Linux environment and pinned graph: **0 missing/incompatible edges**, and compatible Requires-Python values.
- Linux binaries were **not executed locally**: this Windows workstation has no available Docker/WSL runtime. Successful artifact checks must not be reported as a passed Linux test suite.
- No project application module was imported by these dependency checks. Existing `.venv-audit` was only inspected and used as an interpreter for creating the new venv/downloading artifacts; it was not installed into or modified.

These checks do not establish application correctness, authorize real trading, replace vulnerability review or attest the production installation.

For future changes, begin from these exact pins. Review only the intended dependency change and its reachable edges; fetch exact-release artifact hashes, validate target wheels and metadata, provision a new isolated environment, then run the sandboxed regression suite. Never regenerate the lock from the entire global environment or use an unconstrained upgrade to repair a failing test.
