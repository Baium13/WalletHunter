# Autonomous recovery and backups

No deployment or LIVE_AUTO enablement is part of this change.

The inbox, analysis checkpoint, authorization, canonical intent/reservation and
episode action keep stable identities. Re-delivery never resubmits an existing
intent. Recovery queries UNKNOWN/PARTIAL outcomes; unavailable proof keeps HOLD.
Malformed non-financial deliveries are quarantined while later records proceed.

New PAPER/SHADOW runs use `paper-limit-taker-v1`: approved-limit fills, 5 bps fees
per filled notional, observed bid/ask spread included, zero additional slippage.
Configure `costs` in the PAPER runtime JSON; changing a stored cost model requires
a separate simulation namespace. These are assumptions, not exchange results.
LIVE fees/MAE/MFE remain unavailable without actual evidence. No weights auto-promote.

Backup operators should supply `--manifest /absolute/runtime-state-manifest.json`
(or place it at the application root). Example structure:

```json
{"version":1,"capacity_warning_bytes":5368709120,"artifacts":[
  {"path":"data/state.json","archive":"data/state.json","kind":"state","required":true},
  {"path":".env","archive":".env","kind":"file","required":true},
  {"path":"data/executions.sqlite3","archive":"data/executions.sqlite3","kind":"sqlite","required":true},
  {"path":"/configured/research.db","archive":"research/research.db","kind":"sqlite","required":true},
  {"path":"/configured/paper/autonomy.sqlite","archive":"paper/autonomy.sqlite","kind":"sqlite","required":true},
  {"path":"/configured/paper/fake.sqlite","archive":"paper/fake.sqlite","kind":"sqlite","required":true},
  {"path":"/configured/paper.json","archive":"paper/config.json","kind":"file","required":true}
]}
```

Use actual configured paths, including every tenant/mode state directory, Telegram
session, profile encryption material and runtime configuration. Missing required
artifacts or omitted detected stores fail backup. Without an explicit manifest,
legacy local inventory emits a warning: external stores cannot be inferred.
The journal DB also contains canonical events/reservations and Manual Copy state;
the autonomy DB contains episodes, outcomes and calibration records.

Archives are NOT encrypted. Owner-only permissions/Windows private ACLs are
required. Retention is explicitly KEEP_ALL; capacity warnings do not delete data.
SQLite snapshots are individually consistent, not a global cross-store snapshot.
Restore matching source/dependencies and keys, verify hashes/integrity into an
empty isolated directory, then run query-only recovery before new authorization.
Restore never starts a worker. Keep unresolved state when cross-store proof is
unavailable; never reconstruct a missing fill from a position delta alone.
