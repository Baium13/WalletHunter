# Block 1 completion pass — PARTIAL

No production process, credentials, database or orders were used. Existing
execution routes and the previously protected Phase 1/core files are unchanged.

Implemented, but not wired into production authorization:

- `journal_bridge.read_journal`: bounded, account-filtered, read-only SQLite
  transaction. Missing/truncated/corrupt evidence is an error, not empty state.
- `SynchronizedLedger`: reuses the unchanged P1.2 accounting book, reserves the
  incremental pending envelope without double-counting held exposure, and rejects
  legacy/network/attribution inconsistencies. Journal weights cannot create fresh
  VERIFIED provenance. It requires the existing account lock around collection.
- `live_reconciliation.reconcile_order`: read-only Hyperliquid-compatible client-ID,
  order-status, exact order fields, fill IDs and position-delta proof for IOC
  OPEN/ADD/REDUCE/CLOSE. Supports terminal partial execution and lost submission
  acknowledgement. Missing proof remains UNKNOWN; no retry or reservation release.
  It does not adopt legacy client IDs or persist ownership itself.

Minimum remaining implementation (not an external/API impossibility):

1. `foundation/data.account_snapshot` still returns incomplete live account evidence.
   Implement bounded coherent reads with collateral-pool identity, real timestamps,
   open orders and fresh ownership history. Do not substitute receipt time for
   exchange time or sum unified collateral twice. Multi-request REST does not, by
   itself, make such a conservative adapter impossible.
2. `foundation/risk.evaluate` remains OPEN/FAKE-only. `OrderIntent` does not encode
   protective stops/cancellation/leverage-only operations. Extend these contracts
   with explicit authorization/scope, preserving existing route-specific policies.
3. `ExecutionGateway.__init__` accepts only `FakeExchange`. Implement the signing
   adapter and atomic reservation/intent identity mapping between core storage,
   legacy journal, manual holds and AI lifecycle. Overlapping reservations currently
   HOLD: they are not silently deduplicated or released.
4. Wire reconciliation into durable settlement and crash/restart recovery before
   switching writers. Current read-only helper returns proof, not ownership changes.

Execution call-site inventory (all still legacy, NOT MIGRATED):

- Copy: `core/trading_engine.py` leverage/open/reduce/close.
- Manual: `core/manual_positions.py` stop placement/cancellation and market close.
- Confirmed AI: `core/ai_user_orders.py`, `core/ai_position_actions.py`, and
  `core/ai_review.py` (review-confirmed reduction is also reachable from web/Telegram).
- Recovery: `CopyEngine.emergency_stop`; retains existing explicit ownership scope.
- Signer construction: `desktop/main.py`, `webapp/server.py`; SDK writers remain
  inside `integrations/hyperliquid.py`, reachable by the above legacy callers.

The exclusive-writer guarantee is NOT established. No new LIVE writer was enabled.
Historical reports/manifests are preserved. Additive helper rollback requires only
source rollback, never financial-state rollback. Block 2 must not start.

Validation: `block1-completion-validation.json`. First targeted run exposed Windows
fixture connection-handle cleanup errors; fixtures now explicitly close connections.
Subsequent targeted and full runs pass; no failures were waived.
