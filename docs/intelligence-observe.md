# Public intelligence checkpoint — OBSERVE only

This checkpoint is not the finished Block 2/3 product. It adds a bounded public
research worker, deterministic descriptive ranking/agents/consensus, persisted
leader events and analytical replay. It never opens a financial-state store,
binds an account, changes copy admission or submits an order. Existing manual
and confirmed-AI routes are not migrated by this checkpoint.

From `WalletHunterV05_final`, using the locked project Python environment:

```
python -m core.intelligence.worker --network TESTNET --database data/intelligence.sqlite
```

The network argument is mandatory. Use a dedicated research database, never the
execution journal or profile database. This command starts public network reads;
it was not run against the exchange during development. No service was deployed
or enabled. Stopping this worker does not stop existing copy execution.

Discovery observes buyer/seller addresses on BTC/ETH/HYPE public trade streams:
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
It does not enumerate all Hyperliquid wallets. REST fills provide watchlist
recovery. Candidate registry (2,000), watchlist (8), deep analyses per cycle (1),
requests per cycle (32), and storage records (100,000) are bounded. At storage
capacity it stops adding records; archive/retention automation is not implemented.
Run this as a low-priority process; no deployment-level resource isolation has
been installed. Existing public REST request timeout remains 20 seconds.

## Evidence limitations

- Scores are versioned heuristics, not calibrated probabilities of profit.
- Metrics describe available fill cashflows, not reconstructed equity returns.
- Funding and account-equity drawdown are explicitly unavailable.
- 30/90/180-day reports are requested windows, not proof of account age or full
  exchange retention. Empty windows are unavailable, not zero performance.
- Order flow reports insufficient evidence; depth is not aggressive trade flow.
- REDUCE/CLOSE/REVERSE research returns `POSITION_LIFECYCLE_REQUIRED`, never a
  new opposite-side entry recommendation.
- Fill visibility overlap is 60 seconds. Older delayed exchange evidence needs
  a wider reconciliation mechanism before this may authorize execution.
- Only completed decisions replay deterministically. The persisted research
  queue recovers after restart; fresh data collected after a pre-analysis crash
  is new evidence, not falsely labelled as the original snapshot.

## Remaining product work

Manual/confirmed-AI gateway migration; authorization and autonomous allocation;
PAPER/SHADOW lifecycle and outcomes; calibration/promotion gates; authenticated
dashboard integration; Telegram workflow; full end-to-end financial recovery.
Do not enable autonomous MAINNET or label this checkpoint product-complete.
