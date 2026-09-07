# P1.1 — ownership-history cache correctness

Scope: P1-01 only, on top of Phase 0 commit
`182bd1bbed1eeb1c630ff2ea6544c11c52e1323b`. No production access or deployment.
The implementation changes one application file: `core/trading_engine.py`.
Validation results are recorded separately in `p1.1-validation.json`.

**Status: PASS for P1.1's applicable local checks.** New regression tests 18/18;
affected ownership/execution tests 92/92; full Python 674 passed of 675 discovered
(one pre-existing Windows/POSIX skip), JavaScript 96/96, safety guards 49/49.
The full Python/JavaScript run took 110.641 seconds. No failures/errors or
expected failures. Existing HTTPX deprecation and the denied urllib3 IPv6
capability probe are retained as warnings, not hidden or repaired.

## Cause and call path

The former cache stored a wall-clock timestamp. A hit substituted `[]` for a
history response, then renewed that timestamp. Watcher cycles less than 60
seconds apart could therefore suppress subsequent history reads indefinitely.

The sole production caller is `desktop/main.py`'s watcher, which invokes
`CopyEngine.sync_profile()` and then `_sync_locked()`. The latter owns the only
read/write site for `ownership_checks`; `__init__` creates the empty cache.
The web module constructs an engine but does not call this history-verification
path. Existing per-user locks, account guard and fresh profile checks are kept.

## Narrow correction

- The immutable cached proof explicitly records `no_later_fills` and
  `fetched_at`. The latter is monotonic **request-start** time, stored only when
  an actual history fetch and the existing evidence checks succeed.
- `checked_at` is the current cache check/access time. It is not persisted into
  an existing proof. A hit is valid only for `0 <= age < 60`; the TTL remains
  the original hardcoded 60 seconds.
- At expiry, the next required verification performs the existing
  `userFillsByTime` request with the original journal `verified_at_ms` as Unix
  `startTime` and `aggregateByTime=False`. No additional request is made on a
  valid hit. There is no within-cycle retry loop.
- The original list/type, 2,000-row cap and same-market-fill checks are kept.
  A response that itself reaches/exceeds the evidence TTL is also rejected.
  Failure leaves the prior proof stale and enters the existing reconciliation
  HOLD path; unavailable history is never converted into empty evidence.
- A later same-market fill is thus detected on refresh even if the current
  position has the same size, side and entry price. HOLD prevents a copied
  adjustment/close using that ambiguous ownership.
- The in-memory key is now `(Telegram user ID, lowercased follower address,
  reader endpoint, execution-client endpoint, canonical market, verified_at_ms)`.
  A different user, address, endpoint or ownership generation cannot inherit
  that proof. Existing synthetic readers without endpoint attributes use `None`.

This does not promise instant fill detection inside a valid 60-second cache
window. Shape mismatches still use the existing immediate HOLD path. No new
ownership is inferred from an error, a symbol match or a cache timestamp.

## Unchanged boundaries

No sizing, leverage, allocation, risk threshold, AI policy, discovery, frontend,
dashboard or MAINNET/PAPER behavior is changed. The durable execution journal,
original verification watermark, `source_targets`, source attribution, locks,
manual/AI holds and reconciliation behavior are preserved. There is no state
migration, adoption of real positions or new autonomous execution path.

The cache is process-local and empty after restart, which requires a new first
verification. Its endpoint isolation is **not** a redesign of the persistent
journal's network identity and does not close P1-10. P1-02 through P1-10 remain
separate, unimplemented findings.

## Reproducible validation and source protection

New tests live only in `tests/test_ownership_history_cache.py`, reusing synthetic
exchange/storage helpers rather than duplicating the existing test suites.
They replace only the engine module's clock reference; asyncio and process-wide
time remain real. Temporary JSON/SQLite files are test fixtures, not production
state. No test sleeps for the real TTL or contacts an exchange.

The original `docs/protected-source.json` is unchanged. The separately reviewed
`docs/protected-source-p1.1.json` authorizes exactly one old-to-new hash transition
for `core/trading_engine.py`. The checker rejects extra paths, wrong original
hashes, further edits and unrelated protected-file changes. The other 130
original protected files remain byte-identical.

Small test-tool adaptations allow explicit `--phase 1.1` and selected copied
Python modules, require a separate report, preserve the historical Phase 0
reports/defaults, and admit exactly 47 test modules (the original 46 plus this
one). CI selects Phase 1.1 only after checking the explicit hash transition.
All source-copy, synthetic environment, network/process/file guards and Linux
namespace controls remain intact. No CI workflow or dependency lock is changed.

From the repository root, with the existing locked environment:

```powershell
& .\.phase0\venv\Scripts\python.exe -B tools/baseline/run_tests.py --phase 1.1 --python .phase0/venv/Scripts/python.exe --node node --python-module test_ownership_history_cache --report .phase0/p1.1-targeted.json
& .\.phase0\venv\Scripts\python.exe -B tools/baseline/run_tests.py --phase 1.1 --python .phase0/venv/Scripts/python.exe --node node --report .phase0/p1.1-full.json
& .\.phase0\venv\Scripts\python.exe -B -m unittest tools.baseline.test_repository_guard
& .\.phase0\venv\Scripts\python.exe -B tools/baseline/check_protected_source.py
& .\.phase0\venv\Scripts\python.exe -B tools/baseline/check_repository.py --index
```

Selected runs remain explicitly labelled selected, not a full-suite result.
The new source-protection/runner regressions run in the existing CI guard suite.
Historical unexecuted browser/Linux checks are not converted into passing tests
by this change; their Phase 0 environment limitations remain recorded.

## Rollback and handoff

This local source commit can be reverted together with its tests and reviewed
hash transition. No financial snapshot restore or database migration is needed.
A rollback restores the known sliding-cache defect; it is not an improvement
to ownership safety. No production service was restarted or deployed here.

P1.2 requires explicit approval. This result does not certify the remaining
execution, allocation, account-binding or risk-policy findings as safe.
