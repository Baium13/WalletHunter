# P1.2 — held / committed source capital

## Pre-change capital flow (traced before application edits)

`capital_snapshot.py` supplies the mode-aware account sizing balance. Unified
accounts count USDC collateral once; ordinary accounts use supported perp account
values. This is not the same quantity as currently available exchange margin.

`CopyEngine._plan` uses this balance divided by **three**, computes source targets,
then applies the existing leverage, asset and gross-exposure limits. It caps only
the positions in that desired plan. It runs before reading actual positions.

The watcher subsequently reads follower positions, runtime managed/HOLD state,
SQLite ownership and unresolved operations. P1.1 verifies ownership history with
a bounded cache. Paused, removed, manual, AI and reconciliation holds prevent
execution but their collateral did not reduce the next market's source budget.

Reconciliation runs sequentially. Account margin preflight precedes PREPARED
intents; confirmed snapshots update ownership, while ambiguous submissions remain
UNKNOWN. The original cycle cached pending markets once and reconstructed results
without retaining actual `margin_used`. Planned reductions could therefore appear
to free source budget before any successful exchange confirmation.

Live execution provenance already stores `source_targets` in `executions.sqlite3`.
These are strategy target weights, explicitly **not individual fill ownership**.
PAPER stores isolated positions in its account-specific runtime, without equivalent
source weights for legacy positions. No financial data will be migrated or invented.

## Scope

Only source-capital admission is being corrected. Fixed thirds, leader targets,
leverage limits, HOLD/exit policy, P1.1 history verification and account collateral
checks remain in place. No production service or state is accessed or rewritten.

## Accounting and execution contract

`core/source_allocation.py` is a pure, ephemeral view; it has no network, order or
database writes. Each configured source has `allocation_limit = sizing_balance/3`,
`committed_margin`, `reserved_margin` and nonnegative `available_source_budget`.
The planner is unchanged. Immediately before each reconciliation the watcher
rebuilds this view using actual positions and the latest journal evidence.

An observed market is charged once at the greater of its explicit margin used
and its nominal initial margin (`position_value/leverage`). This conservatively
retains extra isolated collateral. Only an absent explicit field uses the nominal
estimate (including PAPER); supplied invalid values never become zero. Valid
same-direction strategy weights divide that one commitment between sources.
Partial fills do not cause the full planned target to be charged as a second
position. This allocation is not a new assertion about individual fill ownership.

All attributable open markets count, including desired, HOLD, paused, detached
and source-owned AI/manual intervention positions. Unmanaged external positions
are not assigned a source. Only separately evidenced independent AI ownership is
excluded from copy-source accounting. Uncertain provenance blocks new commitment.

For unresolved PREPARED/UNKNOWN operations, the existing intent and pre-operation
evidence give a conservative per-source envelope. Only its excess over the already
counted market commitment is reserved. An intended reduction/close cannot release
the pre-operation commitment. Unsupported or malformed intents block admission.
`ExecutionJournal.pending_intents` is read-only; no table or status transition
changes. No retry or order lifecycle is introduced.

For an existing desired market, its own commitment is credited only when computing
the replacement target; held commitments in *other* markets are never credited.
The tightest source capacity caps the target proportionally. Leverage and source
slot budgets remain unchanged. A cap cannot turn an increase into liquidation or
intensify a requested reduction. A genuine reduction can proceed with original
provenance and unchanged leverage when the budget is unavailable, but its coin
quantity is revalidated against the fresh execution price first. Failed ledger
reads do not authorize even this fallback. Existing reversals still require a
confirmed flat old side before a new order.

Existing fresh account collateral preflight and its fee/slippage buffer remain.
Outstanding source reservations are also subtracted from account capacity so a
different source cannot reuse collateral promised to an unresolved operation.
This can double-reserve funds already reflected by the exchange; that conservative
underutilization is intentional until the intent is safely resolved.

Successful operations preserve the confirmed position snapshot, including actual
margin. Leverage-only operations verify a post-update snapshot. An ambiguous
snapshot leaves UNKNOWN, not released capital. New PAPER executions retain their
source weights in the existing isolated PAPER position record; old records are
not migrated or retroactively attributed. Restart reconstructs from these same
existing storage mechanisms, not a second mutable live financial ledger.

## Deliberate limitations

- Mixed/opposing source weights or missing/inconsistent attribution cannot prove
  a safe source budget. New commitment is blocked; no ownership is invented.
- Removed wallets do not have persistent slot IDs in the existing model. Their
  retained capital is counted, and additional commitment is blocked rather than
  donating a historical third to a replacement wallet. Retained positions are not
  liquidated to solve this ambiguity.
- Genuine overcommitment after equity/price changes yields no available source
  capital, not an automatic close. This is not exchange-enforced segregation of
  cross margin; it is deterministic copy-admission accounting.
- Reservations use the durable evidence currently available. Full fill ownership,
  an account-wide execution lifecycle and independent AI reservation reconciliation
  remain P1-05/P1-07 work. No AI execution policy or manual execution route changes.
- Existing journal account/network semantics are preserved (P1-10 remains open).
  The new view is local to a single locked cycle, with no shared cache or ledger.
- An intermediate fixture run exposed an existing recovery input-shape edge:
  observed-flat recovery calls `.append()` on `manual_hold_keys`, which fails if
  legacy state supplies a dictionary. The unchanged P1.1 recovery loop was not
  altered to fix this separate issue (state validation/watcher isolation follow-up,
  P1-04/P1-06). The old HOLD-only test retains its explicit legacy managed-flag
  fixture rather than gaining unrelated ownership-recovery setup between subcases.

## Validation

Final local result: **PASS**. Machine-readable evidence is in
[`p1.2-validation.json`](p1.2-validation.json); reviewed hashes are in
[`protected-source-p1.2.json`](protected-source-p1.2.json).

| Suite | Result |
| --- | --- |
| New allocation regressions | 48/48 PASS (25 pure, 23 engine/journal) |
| P1.1 ownership cache | 18/18 PASS |
| Affected execution/capital/precision suites | 93/93 PASS |
| Complete Python | 723 discovered; 722 PASS, 0 FAIL/error, 1 existing Windows skip; 115.250 s |
| JavaScript | 96/96 PASS across 6 files |
| Safety | 14 sandbox + 49 repository/tooling = 63/63 PASS |

The sole skip is the POSIX archive-permission check. Existing Starlette/HTTPX
deprecation and denied urllib3 import-time local IPv6 probe remain; there were no
outbound exchange requests or production credentials. Hosted Linux/auxiliary
browser execution remains the prior external-environment limitation, not a new
P1.2 failure. All four final runner reports verified source bytes unchanged.

The first intermediate full run found two new-case failures: an overbroad test
expectation about legacy exits (narrowed to explicit HOLD), and same-cycle reuse
after invalid post-leverage collateral. The latter was fixed and regression-tested:
a newly UNKNOWN operation makes capital uncertain for the remaining cycle, while
the next cycle can reconstruct pending reservations from fresh data. No retries
were added. Final complete run has no failures.

Original/P1.1 manifests and the 18 P1.1 regression tests remain immutable. The
existing execution-test helper now seeds explicitly synthetic source provenance;
its assertions remain unchanged. Tests with absent provenance seed that absence
explicitly. All test credentials, orders and state are synthetic and isolated.

## Recovery / next boundary

This is a local source commit, not a production release. No service restart,
financial-state rewrite, secret rotation or journal migration occurred. A reviewed
code revert is the rollback boundary; it must not reset or replace runtime files.
New PAPER source metadata is additive to existing position JSON. P1-02 is closed
for this bounded copy-allocation correction; P1-03 through P1-10 remain open.
P1.3 must wait for explicit approval.
