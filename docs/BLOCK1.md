# Block 1 — additive core (PARTIAL, live migration blocked)

Entry points are library classes under `core/foundation`; neither bot nor web
startup imports them. No production configuration or financial state was changed.

Implemented: frozen/versioned contracts; Phase 1.2-backed source ledger; SQLite
append-only events and transactional local consumer offsets; scoped replay;
bounded shared REST cache with gap recovery; deterministic risk decisions; one
FAKE/PAPER execution gateway; durable reservations; fill/order/delta reconciliation.
Risk evidence includes exact intent, market and policy hashes; policy/prestate
are persisted. Partial fills settle actual commitment; acknowledgement loss and
crashes query durable fake exchange evidence without submitting again.

## Use and constraints

- Instantiate `Store` with an explicitly isolated database path. Do not point it
  at an existing financial database. Tables are additive and use separate names.
- `MarketData` consumes the existing bounded public reader. Cache/resource limits
  are explicit. Stream ingress is an adapter seam, not an enabled websocket;
  disconnect/gaps require REST recovery. REST throttling fails rather than queues
  unbounded work. Unknown exchange timestamps are not synthesized.
- Publish a validated FAKE portfolio, supply explicit `RiskPolicy`, then construct
  `ExecutionGateway` with `FakeExchange`. Only an explicitly granted FAKE/PAPER
  OPEN intent can execute. All LIVE, ADD/REDUCE/CLOSE routes fail closed here.
- `authorize_fake` is test/operator setup, not Telegram authentication. There is
  no public API exposing grants or raw storage. Future observers get `ReplayReader`,
  never a gateway/signing handle. No signing SDK/credential dependency exists in core.
- One configured store is authoritative for the new route. Reconcile and settle
  events, portfolio and reservations in the same SQLite transaction. A consumer
  may update local projections in its supplied transaction; external notifications
  are at-least-once and must deduplicate event IDs. Replay never dispatches orders.
- Back up the entire new SQLite store, not events alone: grants, intent prestate,
  policy and reservations are recovery evidence. Keep private filesystem permissions;
  Windows needs an operator-managed private ACL. No encryption contract is added.

## Exact live migration blockers

The new core is **not production-authoritative** and is not ready for autonomous
Wallet Discovery execution. Existing copy (`core/trading_engine.py`), manual
(`core/manual_positions.py`) and confirmed AI (`core/ai_user_orders.py`,
`core/ai_position_actions.py`) continue through their original hardened routes.
None has a second new live writer.

1. Live account bridge is deliberately UNKNOWN: existing multi-request REST account
   reads do not provide an atomic exchange watermark. Actual equity, collateral
   pool, positions and open orders require a coherent evidence/freshness contract.
   Do not fabricate exchange time from receipt time to enable risk.
2. Legacy execution journal/provenance/pending reservations are not migrated into
   the new ledger. An exclusive ownership/serialization and projection bridge is
   required before any route can switch. Replay of new events is not a substitute
   for historical financial provenance.
3. No real signing adapter is wired. The new reconciler implements fake order/fill/
   delta proof only; a live adapter must reuse Phase 1 proof and coordinate journal,
   timeout, partial/unknown lifecycle and legacy account locks before enabling it.
4. New risk/ledger policy is conservative OPEN-only. Reduction/closure, real fees,
   funding, collateral pools and source lifecycle remain in the existing routes.

These are safety blockers, not permission to weaken checks. Phase 1 source hashes
remain unchanged. Rollback of this unused additive library is source-only; never
roll back production financial state. Block 2 remains unstarted.

Validation: `docs/block1-validation.json`; isolated full suite:
`python tools/baseline/run_tests.py --phase block1 --report .phase0/block1-full.json`.
