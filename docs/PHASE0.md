# Phase 0 — reproducible, secret-safe baseline

Date: 2026-09-07. Scope: the local repository only. **Overall status: PARTIAL**,
not a production readiness certificate. The offline unit baseline is PASS; the
four additional browser harnesses and the Linux CI job have not been executed
here. No Phase 1 application fix is included.

The [final local validation follow-up](PHASE0_FINAL_VALIDATION.md) records a
fresh matching unit run and separates locally passing baseline checks from
external browser/Linux execution limits. Original evidence below is retained.

## What changed

- Git exclusions and byte-preserving attributes; no credential rotation, history
  rewrite, deletion of ignored files, remote push or production deployment.
- A project-only, hash-locked dependency graph and a fresh disposable environment.
  The only dependency-input repair declares the already imported Pydantic.
- A blank-secret `.env.example`, documentation, release identity tooling and CI.
- New test-only isolation/repository guards. Existing application, tests and
  operational scripts are protected by the 131 hashes in `protected-source.json`.

The sample environment is intentionally TESTNET with empty secrets. This does
not change the existing code's MAINNET default or the running configuration.

## Baseline verification

| Check | Result / limitation |
| --- | --- |
| Existing Python suite | 657 discovered; 656 passed, 0 failures/errors, 1 existing Windows skip |
| Existing Node suite | 96 discovered; 96 passed, 0 failures/skips |
| Existing unit duration | 103.015 seconds total in the final recorded run |
| Isolation self-checks | 9 Python + 5 Node passed; 0.392 seconds |
| Repository guard tests | 19 passed; path case, encodings, staged modes and synthetic-token exemption |
| Expected failures | 0 Python expected failures / unexpected successes |
| Existing suite repeat | Two complete runs with identical pass/skip outcomes |
| Protected application/source | 131 original files byte-for-byte unchanged |
| Fresh dependencies | CPython 3.12.14; exactly 51 locked packages, no extras/conflicts |
| Linux artifacts | Hash/metadata checks passed for x86_64 and aarch64; not execution tests |
| Auxiliary browser tests | 4 Edge/Playwright scripts, 20 scenarios NOT_RUN; see `browser-baseline.json` |
| Linux CI | Configured; not executed locally (no available Docker/WSL) or hosted (no Git remote) |
| Production | Not accessed, restarted, deployed, traded or rewritten |
| Recovery | Procedure documented; actual production archive/restore unverified |

Detailed evidence is in `baseline-tests.json`, `baseline-guards.json`,
`dependency-inventory.json`, and `browser-baseline.json`. Separate repository
scanner regression tests exercise only the new Phase 0 guard code.

### Warnings and failure classification

- The existing POSIX archive-permissions test skips itself on Windows. Class A
  (environment/platform), not a successful Linux permission test.
- The locked FastAPI/Starlette TestClient emits a deprecation warning about
  HTTPX and recommends HTTPX2. The current suite passes; no dependency upgrade
  or test rewrite was made to silence it.
- urllib3 probes IPv6 support with a socket bind during import. The sandbox
  blocks that capability probe; it is not a Hyperliquid request. The report
  records the call location without request payloads or credentials.
- Setup-only fixes in the new runner: fixed TAP reporter, preserved Linux venv
  executable symlinks, disabled dotenv discovery and guarded SQLite URI paths.
- Categories retain the user's meanings: A environment/setup; B reproducibility;
  C existing application defect; D flaky/nondeterministic; E unknown.

No application defect or existing test was repaired in this phase. All ten
findings in `PHASE1_FINDINGS.md` remain OPEN regardless of the passing unit suite.

## Safe local commands

First create the clean environment using `DEPENDENCIES.md`. From the repository
root, use its Python (Windows shown):

```powershell
& .\.phase0\venv\Scripts\python.exe -B tools/baseline/check_repository.py --candidates
& .\.phase0\venv\Scripts\python.exe -B tools/baseline/check_protected_source.py
& .\.phase0\venv\Scripts\python.exe -B tools/baseline/run_tests.py --python .phase0/venv/Scripts/python.exe --node node --report .phase0/local-tests.json
```

The runner copies allowlisted application/test files to a fresh temporary tree.
It never copies real `.env`, data, keys, sessions, archives or deployment secrets.
Child processes receive a minimal environment and synthetic credentials. Socket,
DNS and subprocess guards run before application test discovery. All writable
test state stays in the disposable tree. Real source/state is not the test root.

These Python/Node guards protect trusted tests against accidental I/O; they are
**not an adversarial native-code security sandbox** on this Windows workstation.
Do not run the old suites directly against the working application directory.

## CI contract

`.github/workflows/baseline.yml` pins checkout/setup actions by full commit SHA,
Python 3.12.14 and Node 24.19.0. Checkout does not persist credentials; workflow
permissions are read-only. There is no deployment step, production secret input,
SSH command, server mount, or exchange client startup.

Dependency provisioning has network access to install the exact hash-approved
packages; it does not execute repository application modules. Before repository
checks/tests, `ci_offline.sh` creates a Linux network namespace and drops back to
the non-root runner with all capabilities removed and no-new-privileges set.
It clears the environment, verifies only loopback is present, then runs source
checks and the copied test harness. **There is no fallback to network-enabled
testing if isolation fails.**

CI parses Python/JavaScript without importing live entrypoints. Unit-test imports
occur only in the copied sandbox. Browser harnesses are explicitly not covered.
Hosted execution must still verify the new workflow; no green CI run is claimed.

Reference practices: [hash-checked installs](https://pip.pypa.io/en/latest/topics/repeatable-installs/),
[pinned GitHub actions](https://docs.github.com/en/actions/reference/security/secure-use).

## Git and release identity

The initial inspection found zero commits and zero tracked files. The baseline
commit is created only after candidate/index secret checks. The one reviewed
token-shaped value allowed by the scanner is a synthetic offline HMAC fixture,
allowlisted by exact file/rule/value hash. No operational token value is printed.

The exclusions preserve local files: keys/certificates, `.env`, JSON/SQLite state,
Telegram sessions, backups, release archives, logs, virtual environments, caches,
screenshots and historical operational reports stay untracked. Pattern scanning
is a guard, not proof against arbitrary encoded/steganographic secrets.

`release-baseline.json` is a committed descriptor. A commit cannot include its own
hash without changing that hash, so the exact release manifest is generated
**after** committing from a clean HEAD:

```powershell
& .\.phase0\venv\Scripts\python.exe -B tools/baseline/release_manifest.py
```

The ignored `.phase0/release-manifest.json` records the actual commit/tree, lock
SHA-256 values, test-baseline hash, entrypoints and current schema inventory. It
is deterministic and regenerable; it does not prove a deployment took place.
Source hashes deliberately freeze this phase; changing that boundary in Phase 1
requires an explicit reviewed decision, not silently updating hashes to hide a diff.

## Recovery and remaining acceptance limits

Follow `RECOVERY.md`: recover code/configuration/state/session/keys as an evidenced
set, keep secret material out of Git, and never overwrite newer live execution
records with an old snapshot. A tar.gz containing `.env` plus encrypted keys is
not an encrypted backup. No new backup encryption scheme was introduced.

To close the remaining Phase 0 validation gaps, run the Linux offline workflow
in an approved runner and establish a reproducible, isolated browser stage for
the existing Edge-dependent scripts. These are baseline-validation tasks, not
permission for Phase 1, a production deployment, or a real order.
