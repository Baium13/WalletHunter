# P1.3-P1.10 controlled stabilization

Scope: local source and disposable offline tests only, following P1.2 `a922749`.
No SSH, deployment, production-state rewrite, financial-history migration,
credential rotation, strategy redesign or autonomous MAINNET path was performed.
The original Phase 0/P1.1/P1.2 evidence remains immutable. See
`phase1-validation.json` for final measured results and source identity.

## Finding disposition and contracts

| Finding | Implemented boundary |
| --- | --- |
| P1.3 | Telegram research writes `wallet_research`, not copy admission `leader_models`. Existing explicit/legacy admission configuration is retained, not retrospectively reinterpreted. Web analysis remains informational. Regression compares actual plans before/after research and after storage reload. |
| P1.4 | Both position adapters reject absent/malformed/nonfinite position data; an exact valid size zero remains flat. Missing funding is an error, not zero. Entry context/spread must be finite and fresh. Balance fetch failures propagate rather than becoming zero. Legacy list/dict HOLD representations both remain HOLD. |
| P1.5 | Real copy adapter requires acknowledgement OID, terminal exchange order status, unique matching trade IDs, finite fill quantity/side and agreement with the observed position delta. Same-market external fills, delayed visibility and lost acknowledgements remain UNKNOWN. No blind retry or invented fills. Leverage-only updates retain the prior post-update identity/collateral checks. |
| P1.6 | Watcher uses per-user exception boundaries and four bounded concurrent user jobs. SDK public/signing transports have a 20-second request timeout. User A failure does not prevent user B processing. Exceptions remain observable by class without arbitrary secret-bearing text. |
| P1.7 | Existing confirmed AI entries release reservations only after complete entry/exit fill continuity, a fresh flat position and absence of resting orders. SQL RELEASED precedes idempotent cleanup of that proposal's JSON hold. User/account/network are checked. SUBMITTING/UNKNOWN cannot release from a flat snapshot alone. A separate read-only public-client reconciliation worker also runs when copying is paused; it does not open or exit positions. |
| P1.8 | New bindings derive the signer locally. Direct owner equality or the selected network's explicit `userRole=agent` delegation to the target is required. Existing cross-tenant binding and mutation guards remain active. Verification submits no trade and exposes no key. |
| P1.9 | Raw exception values removed from bot normal logging. Backup target/input symlinks rejected; POSIX directory/archive permissions enforced; KEEP ALL retention explicit. Encryption/key custody remains an operational requirement, not a hidden new contract. |
| P1.10 | Only MAINNET/TESTNET are accepted network names. New live intents and ownership carry network identity. Foreign/legacy ownership and foreign pending execution require HOLD/reconciliation. Open/reduce/close use explicit percentage slippage, with SDK price normalization for IOC reductions. Default cancellation scope is explicit owned IDs, not all account orders. Emergency managed-close independently checks ownership, network, exact position and later fills. |

## Execution modes (not a universal PAPER switch)

`HL_MODE` selects the exchange network; `AUTO_TRADING` selects copy LIVE/PAPER.
PAPER copying does **not** prohibit a separately confirmed manual or AI-form
order on the selected network. `/api/dashboard` publishes `execution_scope`
with this distinction; the compatibility `live` field still describes copying.
Independent AI suggestions require the existing explicit confirmation flow.
No automatic AI exits or new MAINNET authorization were added. Frontend layout,
wallet discovery, agent orchestration, consensus and allocation policy are unchanged.

## Preserved accounting and provenance

Fixed thirds and `source_allocation.py` are byte-identical to P1.2. The original
18 P1.1 tests and 48 P1.2 tests are unchanged. Recovery uncertainty still enters
the committed-capital book: held or unresolved exposure cannot release another
budget. No ownership is inferred solely from a matching symbol or current free
margin. New network fields do not retroactively assign a network to old records;
missing identity remains legacy/unknown. The historical recovery-import script
is not run and cannot establish new network identity by itself.

## Conservative limitations

- Uncertain acknowledgements or incomplete history stay reserved/HOLD. There is
  no new automatic repair/retry daemon for ambiguous executions. Manual evidence
  review is required; pausing is preferable to duplicate orders.
- Vault/subaccount routing is intentionally unsupported by new binding. API
  agent delegation is verified at binding, not a promise against later revocation.
- Transport timeouts are per request, not a hard wall-clock deadline for an entire
  multi-request cycle. Cancelling an executing thread would permit overlapping
  mutations; this sprint does not introduce that unsafe timeout pattern.
- Backup encryption/offsite key recovery and Windows ACL enforcement require an
  operational decision. See `phase1-backup-security.md`. KEEP ALL requires disk
  monitoring; tar.gz provides no encryption.
- Offline tests do not attest to deployed credentials, exchange availability,
  production provenance or hosted Linux CI execution. Existing external browser/
  hosted-CI limitations from Phase 0 are not reclassified as live validation.

## Validation and rollback

Targeted suites were run per finding, followed by complete Python/JavaScript and
baseline guards. Added adversarial cases include network-mismatched pending
capital, reader/signer mismatch, external fill ambiguity, invalid funding/size,
AI flat-without-proof, emergency ownership ambiguity and scoped cancellation.
Initial test fixture/expectation issues were corrected without weakening prior
P1.1/P1.2 tests. One intermediate run correctly rejected source edits made while
its isolated copy was executing; the final run uses stable source.

Each logical fix has its own commit; additional adversarial hardening is separate.
Before any deployment, retain the old source revision and private consistent
runtime backup, pause writers, and assess legacy network/ownership HOLDs. Roll
back source only with execution paused and explicit operator review. Never roll
financial state back merely to match an old binary; reconcile real exchange
activity first. This sprint does not perform that deployment or recovery.

Phase 2 remains unstarted and requires explicit approval.
