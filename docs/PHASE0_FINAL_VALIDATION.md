# Phase 0 — final local validation

Date: 2026-09-07. Source under test: `18549f5b24156e86d5e451bb20d553f817a9ef37`.
This follow-up changes validation evidence only. The original test baseline is
retained; [the new summary](phase0-final-validation.json) records this fresh run.

## Classification

- **Application/baseline readiness: PASS for the locally executable Phase 0
  checks.** This is readiness for reviewed architectural work, not a claim that
  every UI scenario, production deployment or trading policy is correct.
- **Full validation coverage: PARTIAL.** Twenty browser scenarios and actual
  Linux execution remain unverified because no suitable isolated runtime was
  available within the permitted host-change constraints.
- **External CI execution verification: PARTIAL / NOT_RUN.** No Git remote is
  configured and no GitHub Actions run was dispatched. A green hosted run is
  not claimed or inferred from static validation.
- All ten Phase 1 findings remain **OPEN**. Phase 1 requires separate approval;
  neither deployment nor live trading is authorized by this result.

## Fresh local tests

| Check | Actual result |
| --- | --- |
| Existing Python | 657 discovered; 656 passed; 0 failed/errors; 1 skipped; 102.157 s |
| Existing JavaScript | 96 discovered/passed; 0 failed/skipped; 1.828 s |
| Existing suite total | 753 discovered; 752 passed; 1 skipped; 103.985 s |
| Python / Node isolation guards | 9 + 5 passed; 0.312 s |
| Repository guard regressions | 19 passed; 0.020 s |
| Syntax without application imports | 101 Python AST parses + 27 JavaScript syntax checks passed |
| Protected source | All 131 original protected hashes match |
| Git index secret/path guard | 165 baseline files checked; 0 findings; one reviewed synthetic fixture exemption |
| Locked environment | `pip check` passed; no packages changed |

No expected failures or unexpected successes. Counts and JavaScript suite names
match the previously committed baseline. The fresh raw report is a local ignored
artifact at `.phase0/final-validation-tests.json`; its hash is in the summary.
It is not a production report and contains only synthetic test activity.

Warnings are preserved, not fixed to obtain a pass:

- Existing POSIX archive-permissions test skips on Windows (category A,
  environment/platform); Linux permission behavior is still untested here.
- Starlette's TestClient emits its existing HTTPX deprecation warning. No
  dependency upgrade or test rewrite was made.
- The language guard blocks urllib3's import-time IPv6 socket-bind capability
  probe. This is an expected setup limitation, not an exchange request.

Reproduce the local unit check from the repository root using the previously
provisioned, locked environment and trusted Node executable:

```powershell
& .\.phase0\venv\Scripts\python.exe -B tools/baseline/run_tests.py --python .phase0/venv/Scripts/python.exe --node node --report .phase0/final-validation-tests.json
& .\.phase0\venv\Scripts\python.exe -B -m unittest tools.baseline.test_repository_guard
& .\.phase0\venv\Scripts\python.exe -B tools/baseline/check_repository.py --index
& .\.phase0\venv\Scripts\python.exe -B tools/baseline/check_protected_source.py
```

Do not run the old suites directly against the runtime application directory.
The copied test runner, synthetic environment and I/O guards remain unchanged.

## Browser: externally blocked, not an application failure

The four original harnesses and their hashes match
[`browser-baseline.json`](browser-baseline.json). All four pass Node syntax
checking. Scenario inventory: modes 4, user orders 4, position actions 4,
startup/deep links 8. **Executed: 0; passed: 0; failed: 0; skipped/not run: 20.**

Installed Playwright/Core is 1.62.1. The Microsoft-signed Edge executable is
152.0.4191.66; its version is observed, not pinned by the repository. The original
scripts explicitly request `channel: 'msedge'`; bundled Chromium is a different
browser and was not substituted. No browser was launched.

All active Windows firewall profiles allow outbound traffic by default. The
observed Edge-specific rules permit inbound mDNS, not an outbound deny boundary.
Windows Sandbox was not available. The remaining local Linux/runtime inventory
is recorded below; no alternative isolated browser host was established.

The harnesses need only fake HTTP/API data, copied static assets and temporary
outputs, not credentials or a running app server. However, `page.route()` is not
browser-wide egress isolation. Service workers are not explicitly blocked and
WebSocket routing is absent; current inspected frontend usage of either was not
found. The existing Node safety preload intentionally denies browser child
processes and screenshot writes. It was not weakened to make these scripts run.

Two pre-existing fixture warnings remain: modes has rescue `-50` instead of the
current `-40`; position-actions supporting trader data uses `1%/20x` instead of
the current `10%/40x`. These are static fixture mismatches, not executed failures.
Neither fixtures nor policies were changed.

An approved, reproducibly identified Edge image with OS-level egress denial is
still needed. Run the unchanged harnesses against allowlisted copied assets,
without secrets, using new per-suite output directories under ignored `.phase0/`.
Do not count historical screenshots or an ordinary user browser as test evidence.

## Linux dependencies and CI boundary

Environment inventory was read-only: WSL `--status` / `--list` returned code 50
(not installed). WSL/VirtualMachinePlatform/HypervisorPlatform reported disabled
optional-feature state (`InstallState=2`);
no usable Hyper-V management module/CIM namespace or Windows Sandbox executable
was found. No Docker, Podman, nerdctl, QEMU, VirtualBox, VMware or Rancher runtime
was found in the checked command paths, service/installed-app inventories and
common configured locations. `HypervisorPresent=True` alone does not provide an
accessible Linux guest. Available tools supplied no alternative isolated Linux
execution service. This is a limitation of the available environment, not a claim
that installing or enabling another runtime would be technically impossible.

Syntax checks passed using existing trusted tools, without installing anything:

- The YAML parser exported by `playwright-core@1.62.1/lib/utilsBundle.js`, under
  the unchanged Node network/process/write guard: YAML 1.2, strict parsing and
  unique keys; zero errors/warnings. Expected triggers and the baseline job exist.
- Bundled Git GNU Bash 5.2.37 (`usr/bin/sh.exe -n`): the LF-only
  `tools/baseline/ci_offline.sh` and both YAML-extracted workflow `run` blocks
  passed parsing. No shell test/provisioning command was executed by `-n`.
- `actionlint` and `shellcheck` were not available. The result is genuine YAML
  and Bash syntax validation plus static contract review, not a hosted Actions
  scheduler/schema/runtime acceptance result or a Linux Bash execution test.

All 51 pins and approved hash sets match the dependency inventory. Each Linux
target (x86_64 and aarch64) has 47 already downloaded artifacts: 46 wheels plus
the pyaes source archive. All hashes, wheel tags, Python constraints, ELF target
architectures and 66 active dependency edges per target passed metadata checks.
The highest required glibc wheel tag is 2.34, within the documented 2.35 baseline.
All four build-package wheels are compatible by metadata.

`pyaes` remains source-only. Its Linux build has **not** been executed. The
workflow installs the hash-locked build tools first, then application/test
dependencies with `--require-hashes --no-build-isolation`; no unpinned build
backend is intentionally downloaded. Application modules are not imported during
these dependency checks. Network access is allowed for package provisioning,
not for application tests.

Published runtime availability was reaffirmed from the official
[Node 24.19.0 checksums](https://nodejs.org/dist/v24.19.0/SHASUMS256.txt) and
[setup-python manifest](https://raw.githubusercontent.com/actions/python-versions/main/versions-manifest.json):
Node Linux x64/arm64 archives and Python 3.12.14 Ubuntu 24.04 x64/arm64 archives
are listed. This is metadata evidence, not installation/execution evidence.

The workflow uses read-only permissions, full-SHA action pins, no persisted
checkout credentials, no production secrets, no deployment and no exchange
startup. Tests must enter `unshare --net`, drop to the unprivileged runner, clear
all capabilities and the inherited environment, set no-new-privileges, and pass
the namespace checks before any repository test runs. The copied runner adds
language-level network/process/filesystem guards. No network-enabled fallback
exists. Calling `ci_checks.py` on Windows was tested: it refused with exit code
1 before running application tests, as required.

The Ubuntu runner image remains a changing hosted image, not a digest-pinned
container. ARM64 artifact compatibility does not imply a separate ARM64 CI job.
Actual Linux provisioning, namespace creation, POSIX permissions and the hosted
job must still be verified externally. No production server was used as a test
host, and no remote was created or pushed.

Non-blocking operational observations, deliberately left unchanged: CI reports
are not uploaded as artifacts; the temporary HOME has no explicit cleanup on
the ephemeral runner; the shell drops inheritable capabilities, but the Python
entry check does not independently assert `CapInh`. No critical static CI defect
was found. These observations do not justify weakening any existing boundary.

## Handoff

No application file, trading/sizing/copy/AI/risk policy, dependency lock, original
test, runtime configuration, financial state or CI safety boundary was changed
by this validation follow-up. No host feature was installed/enabled, firewall
rule changed, service restarted, production credential loaded or real order submitted.

The remaining environment checks may be completed on an approved isolated
runner; no application change is needed merely to obtain that evidence. It is
reasonable to review and approve Phase 1 work with these explicit coverage
limits, but **Phase 1 has not started**. All original risk findings remain in
[`PHASE1_FINDINGS.md`](PHASE1_FINDINGS.md).
