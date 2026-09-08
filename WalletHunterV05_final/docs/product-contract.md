# Product contract v1 (no deployment)

Authenticated `/api/product` is the canonical snapshot. Legacy `/api/dashboard`
and `/api/intelligence` remain compatibility/research views, not autonomous
portfolio or execution authority. `/health` exposes HTTP/public discovery only;
private component health requires authenticated `/api/product/health`.

Operator-owned `data/product-runtime.json` binds existing stores; no path, mode,
allocation or signing configuration is accepted from a frontend request:

```json
{"version":1,"runtimes":[{"scope":{"tenant":"TELEGRAM_USER_ID","account":"ACCOUNT_ADDRESS","network":"TESTNET"},"mode":"PAPER_AUTO","state_path":"/configured/autonomy.sqlite","config_path":"/configured/paper.json"}]}
```

Use the existing runtime JSON containing `authorization`, `allocation`, `risk`.
Bindings must match the current private account, tenant, network and stored mode.
PAPER/SHADOW/LIVE_CONFIRM have distinct databases. Missing bindings/stores are
UNKNOWN, never a legacy-paper fallback. Add the registry, configuration and
`data/product.sqlite3` to the Block 2 explicit backup manifest. No live runtime
or production configuration is created by this change.

Sections: `mode`, `capital`, `manual-copy`, `positions`, `agents`, `consensus`,
`risk`, `execution`, `discovery`, `leaders`, `analytics`, `calibration`, `health`.
`positions/{episode_id}` and `timeline/{episode_id}` link exact action identities.
History windows are bounded: latest 100 private records, 32 ranked public leaders.
Metrics label this window, realized drawdown proxy, sample size and mode. Null
means unavailable (including unobserved current mark price and LIVE fees).
Heuristic confidence is not a calibrated profit probability. Historical
provenance does not independently authorize a new order.

`confirmations/pending` / `confirmations/{id}` expose immutable proposals.
POST `confirmations/{id}/approve` or `/reject` requires the exact `proposal_hash`
and Telegram authentication. No “latest” action. Rejection never loads a signer.
Approval uses existing account-control verification and the canonical gateway;
UNKNOWN duplicates are query-only. The account-scoped lock serializes controls.

`events/resume?after=N` returns a snapshot plus scoped events; `events/stream`
uses bounded SSE with initial snapshot, cursor, heartbeat, short connections,
re-authentication and reset notification after retention gaps (2,000 events).
The stream projects real state changes; it is not an exchange execution journal.
Polling/disconnect does not change financial state. Reconcile periodically with
the snapshot; public discovery coverage means observed markets, not all wallets.

Telegram `/status` is private-chat-only and uses this same read model. A separate
bounded task projects notifications and sends at most one per tenant per turn.
Delivery is at-least-once: acknowledgement loss may duplicate a notification;
five attempts maximum, durable lease, then DEAD. Never promise exactly-once
Telegram delivery. SENT/DEAD identity records are retained for dedup/audit.
Critical delivery problems remain inspectable in `product_outbox` and component
health. There is no delivery wait in trading/discovery/reconciliation workers.
