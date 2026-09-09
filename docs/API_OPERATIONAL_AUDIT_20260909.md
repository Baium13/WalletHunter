# Hyperliquid API budget and functional audit — 2026-09-09

## Scope and evidence

Production application source is `d8cea2e`, release `b11891aff9d2e0aa`. All 109 deployed source-file hashes and served index/JS/CSS hashes were verified. The final documentation commit does not change deployed application source.

- [109 call edges and exact source locations](API_CALL_INVENTORY_20260909.md)
- [111-function register: runtime, tests, API costs and limitations](FUNCTION_REGISTER_20260909.md)
- Official references checked 2026-09-09: [rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits), [info/history API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint).

Weights below are calculated from observed requests and response sizes using the documented model, **not an exchange-reported IP quota counter**. Item increments are rounded up conservatively. Inflight/timeout requests retain maximum possible response cost. No real order was submitted to establish these figures.

## Limits and protection

| Constraint | Official limit | Deployed state |
|---|---:|---|
| REST/IP | 1,200 weighted/min | Shared SQLite admission across three services: 600 low-priority, 840 safety ceiling |
| Exchange/IP | 1 + floor(batch/40) | Zero exchange requests during audit |
| WS connections | 10 | 1 production public socket |
| New WS connections/min | 30 | Exponential backoff with jitter, capped 300s |
| WS subscriptions | 1,000 | BTC/ETH/HYPE: 3 |
| Distinct user-specific WS users | 10 | 0 |
| WS outgoing messages/min | 2,000 | 3 subscription messages per connect; no order posts |
| Inflight WS posts | 100 | 0 |
| Official EVM RPC/min | 100 | Not used |
| Address action allowance | 10,000 initial + cumulative traded USDC | Read-only counter 299/12,917; surplus 0; cumulative volume 2,917.65 |

Order status, account state, spot account state, allMids, l2Book and exchangeStatus have base weight 2; userRole is 60; other info calls default to 20. Listed history responses add one unit per 20 items, candles per 60 bars. Address action limits differ from IP weights; batched actions count individually. Rate-limited addresses are constrained to one action per ten seconds; cancellation allowance is min(limit+100,000, 2×limit).

The 240-unit safety reserve is admission headroom, not unlimited reconciliation capacity. P0/P1 share the 840 ceiling. Excess work fails closed; no automatic order retry is introduced. Unrelated processes sharing the public IP are outside this governor.

## Before versus after

Unsafe BEFORE observation was stopped after 114.76 seconds rather than deliberately sustaining an overload. It includes startup/deep-history bursts and must not be extrapolated as steady-state demand.

| Window | Duration | Requests/min | Weight/min | Rolling 60s peak | Peak / 1,200 | 429 / timeouts |
|---|---:|---:|---:|---:|---:|---:|
| Before enforcement | 1.913 min | 61.17 | 1,958.98 | 2471 | 205.92% | 0 / 0 |
| Initial settled protection | 11.811 min | 30.99 | 420.20 | 594 | 49.50% | 0 / 0 |
| Final deployed build, 07:05:51–07:15:51 UTC | 10.000 min | 30.80 | 440.20 | 601 | 50.08% | 0 / 0 |

Final sample: 308 requests, 4402 weight, zero HTTP/transport errors and zero inflight requests at completion. Rolling peak includes every observed request, not only 30-second samples. Sampled peak was 581; exact peak was 601. A separate protected cold restart peaked at 739 (61.58%); it was outside this settled window. Under the requested bands (GREEN up to 50%, YELLOW 50–70%, ORANGE 70–85%, RED above 85%), the final window is YELLOW at 50.08%, although the runtime admission component itself reports HEALTHY.

Final window: 106 requests deferred **before** transport, zero metadata-cache hits (four observed over the full instrumented audit). Cache/singleflight is tested, but this steady-state window does not demonstrate a material metadata-cache saving. Most improvement comes from admission control. One WS connection, three subscriptions, zero reconnects/new connections/outbound messages during the final window.

Final mean is 77.53% below the short pre-protection mean, but unequal windows/startup mix mean this is not a controlled throughput benchmark. Work is deferred, not made free.

## Per-source measured usage and conditional projection

These linear projections assume the final ten-minute workload remains constant; they are capacity estimates, not forecasts of actual trading/discovery traffic.

| Source | Requests/min | Weight/min | Share | Requests/hour | Requests/day | Requests/14d |
|---|---:|---:|---:|---:|---:|---:|
| wallethunter-intelligence/position_owned.detect | 6.00 | 120.40 | 27.35% | 360 | 8,640 | 120,959 |
| wallethunter-intelligence/service.detect | 5.60 | 112.40 | 25.53% | 336 | 8,064 | 112,895 |
| wallethunter/manual_copy_worker._cycle | 10.00 | 92.00 | 20.90% | 600 | 14,400 | 201,599 |
| wallethunter-intelligence/service.analyze_one | 1.20 | 73.00 | 16.58% | 72 | 1,728 | 24,192 |
| wallethunter/hyperliquid.<lambda> | 1.50 | 17.40 | 3.95% | 90 | 2,160 | 30,240 |
| wallethunter/hyperliquid.positions | 5.40 | 10.80 | 2.45% | 324 | 7,776 | 108,863 |
| wallethunter-intelligence/service.research | 0.70 | 7.40 | 1.68% | 42 | 1,008 | 14,112 |
| wallethunter/hyperliquid._info | 0.20 | 4.40 | 1.00% | 12 | 288 | 4,032 |
| wallethunter-web/hyperliquid._info | 0.20 | 2.40 | 0.55% | 12 | 288 | 4,032 |
| **Total** | **30.80** | **440.20** | **100%** | **1,848** | **44,352** | **620,925** |

Total projected weight: 26,412/hour; 633,885/day; 8,874,388/14d. OFF Manual Copy account refresh remains about 92 weight/min and is not a trade. The three extra rejected-episode leader watches cost about 120.4 weight/min; those are not open positions.

| Endpoint | Requests | Returned items | Weight | Weight/min |
|---|---:|---:|---:|---:|
| userFillsByTime | 128 | 9714 | 3058 | 305.80 |
| userAbstraction | 28 | 0 | 560 | 56.00 |
| frontendOpenOrders | 20 | 0 | 400 | 40.00 |
| clearinghouseState | 94 | 0 | 188 | 18.80 |
| candleSnapshot | 6 | 426 | 132 | 13.20 |
| spotClearinghouseState | 27 | 0 | 54 | 5.40 |
| l2Book | 4 | 0 | 8 | 0.80 |
| allMids | 1 | 0 | 2 | 0.20 |

## Scheduling, formulae and burst costs

| Loop | Actual gate | Cost before governor |
|---|---|---|
| intelligence.worker | 30s nominal, 32 logical requests, 45s cycle deadline, 20s HTTP timeout | Logical request count is not weighted budget |
| PublicTrades.poll | Five messages/cycle, bounded observations | 0 REST; three subscriptions/start |
| Candidate extraction | Registry cap 2,000 | 0 REST |
| Leader detection | Active max 8; owned max 32; cursor minus 60s overlap; up to four fill pages/wallet | 20–480/wallet/cycle |
| Research | Up to four events/cycle; shared book + 64 candles for all seven agents | Approximately 24/event, 96/cycle, 192/min |
| Cheap filter | One candidate/cycle, 24h, max two history calls | 20–240/candidate attempt |
| Deep analysis | Same candidate after cheap pass; 180d, max eight history calls; windows computed locally | 20–960 additional per attempt |
| Ranking | SQLite score sort/top eight | 0 REST |
| PAPER drain | After analysis, isolated fake state | 0 exchange calls |
| desktop watcher | 3s loop, max four profiles; Manual OFF account refresh gated at 60s | Approximately 92/account refresh |
| AI review/position/entry loops | 60s; observer contention intervals 7/11s, IO 15s | Account pool reads; extra factors only when required |
| Legacy PAPER loop | 60s, separate legacy model | Must not mix with current autonomous session |
| Learning | 60s check; successful bar bucket every 15min; BTC/ETH | Approximately 44/15min steady, 92 cold start |
| AI lifecycle | 60s, query-only pending lifecycle | 0 without pending lifecycle; status/fills when needed |
| Product notification | 10s, max eight users, one delivery/user | 0 Hyperliquid |
| Product SSE | 2s, 15 pages/connection; max two/user, 64 total | DB only; 0 Hyperliquid |
| Visible position chart | About 15s; server candles TTL20s, price TTL1.5s | 24–106/cache miss; governor may return 503 |
| User analysis/preview | Explicit request, 15/10min; cache TTL300s | Bounded history tree plus account evidence |
| Backup | Daily 00:00 UTC | 0 Hyperliquid |

For F(n)=20+ceil(n/20), 0≤n≤2,000, candidate cost is the sum over actual response pages. An inactive cheap rejection costs 20–21. Cheap+deep minimum is 40; loose configured maximum is 1,200 (ten calls ×120). With a successful one-page cheap pass and exhausted deep budget, the tighter bound is 1,080. Full pages may exhaust the binary coverage tree before success.

| Batch size | Cheap inactive | Cheap+deep minimum | Loose configured maximum |
|---:|---:|---:|---:|
| 100 | 2,000 | 4,000 | 120,000 |
| 500 | 10,000 | 20,000 | 600,000 |
| 1,000 | 20,000 | 40,000 | 1,200,000 |

Even with exclusive use of 600/min these maxima need 200/1,000/2,000 minutes. Other services reduce capacity. One candidate/30s independently caps throughput at 720 per six hours, below a 2,000-wallet six-hour reevaluation target.

Final window produced two LEADER_ANALYZED records (0.2/min), three leader events/decisions, zero completed cheap-filter transitions as recorded by the cheap counter. The 730 deep-source weight includes failed attempts; 365 weight per completed analysis is **amortized**, not the cost of either individual wallet.

Twenty simultaneous leaders with four full pages can demand 9,600 units (800% of one official minute). Ten unresolved reconciliations can require 20 status units plus up to 1,200 fill-history units, before account evidence. Cold starts, chart users and Manual Copy would add load. Shared admission caps reservations at 840 (70%); it defers excess work rather than guaranteeing immediate completion. Final normal peak headroom is 49.92%; configured minimum headroom is 30%.

## History, discovery and queue findings

- userFillsByTime returns at most 2,000 fills and exposes the last 10,000 retained fills. Binary window coverage can exceed the two-call cheap/eight-call deep limits. Missing windows are never fabricated.
- HISTORY_INCOMPLETE now preserves the safe causal reason, including HL_API_BUDGET_DEFERRED. Last sampled deep heartbeat was current, last success about five minutes earlier. It is operating with incomplete coverage, not simply offline.
- Overlapping 24h/180d reanalysis is not incrementally persisted. Failed candidates become due after 60s. New metadata caching does not solve historical retention or repeated fill fetches.
- Registry remains 2,000/2,000: ACTIVE 7, CANDIDATE 230, DISCOVERED 36, PROBATION 1,725, QUALIFIED 2. No retirement/eviction frees admission. 1,673 candidates were due; oldest due time was about 14.19h overdue.
- Watchlist monitors ten leaders: seven active and three rejected-episode leaders selected by the existing state != CLOSED predicate. Last scan completed 8/10 with WATCH_SCAN_INCOMPLETE; last full successful scan was approximately 114s before final probe. Priority protects owned scanning but does not guarantee complete scans.
- WS consumes five messages per 30s cycle. Observed TCP receive queue was 85,813 then 70,208 bytes; an independent bounded BTC socket obtained fresh trades. This supports receiver/drain lag, not an upstream outage. Production heartbeat alone is not exchange-timestamp freshness.
- Research follows real leader instruments, while this PAPER session remains BTC-only. Final research snapshot had six ACTIVE agents plus one WAITING Order Flow, seven available; current instrument ATOM. PAPER-specific BTC analysis was idle/stale, seven WAITING/available. These are distinct scopes, not a mode conversion.
- Order Flow lacks reliable aggressor flow and correctly returns insufficient evidence. Leader evidence can be stale even while the agent process is available. No agent/Consensus threshold was changed.

## PAPER quarantine, lifecycle and safety

All 35 historical quarantined jobs were reproduced in isolated PAPER stores with network denied: every one was ADD without a follower episode, raising `ValueError: Position episode unavailable`; fake submission count was zero. No production job was retried, deleted or released.

The final window created zero quarantines and completed three later jobs, proving poison jobs do not block the inbox cursor. Actionable backlog/lag were zero. The earlier historical accumulation remains unresolved; a ten-minute zero rate does not establish a long-term absence of recurrence. Health incorrectly adds 35 jobs and 35 quarantine records to show 70.

The production PAPER store has three REJECTED intents/episodes, no active reservations or unknown execution, no fills/open episodes, no outcomes/calibration labels. Full position lifecycle is isolated-E2E verified, not yet observed during this live-data session. Rejections include SCOPE_MISMATCH and NOTIONAL_LIMIT; no Risk policy was weakened.

Session `paper-20260908T121351Z-07515b5c` remains unchanged: 100 virtual USDC, BTC-only, 5x cap, entry fraction 10%, max margin 25; fee 5bps, APPROVED_LIMIT fill, extra slippage zero. All frozen source/config/policy hashes were verified. No baseline clock reset.

Manual Copy remains OFF, leader/generation NONE, allocation zero. Historical ARCHIVED_UNRESOLVED retains original UNKNOWN evidence and permanent retry tombstones. Operator capital release occurred in the previous reset task, not this audit. Account evidence is fresh unified capital 100.043822; separate core-perp equity zero is not total account balance. Positions empty, no new fills/orders, live grants zero. LIVE_AUTO remains disabled.

SHADOW configuration exists but no parallel runtime binding is active. Product LIVE_CONFIRM is unconfigured (HTTP409); legacy explicit-confirmation routes remain gated. Legacy AUTO_TRADING/AI PAPER/research flags were not silently edited. One bot owner, one intelligence/PAPER owner and one API owner were verified.

## CPU, memory, storage and retention

Over 569.91s with stable PIDs, CPU usage as a percentage of one core: bot 2.459%, intelligence 0.288%, API 0.746% (total 3.493%). Final service memory: 105.48/37.65/78.62 MiB, total 221.75 MiB. Host memory is approximately 1GiB with about 378MiB available at the measured system check; no memory pressure incident observed.

Tracked runtime files total 76.95MB. Growth over 569.91s:
- API metrics +106,496 bytes;
- intelligence +94,208 bytes;
- PAPER autonomy +114,688 bytes;
- total +315,392 bytes, about 47.81MB/day or 669.40MB/14d if linear.

This is allocated file growth, not every logical inserted byte. WAL/checkpoint cycles affect short samples. API metrics have 24h retention, so linear API growth overstates retained rows after steady state. Physical disk free is 43.83GB; retained backups 113.34MB. Even conservatively backing up each day's projected full state adds only a few GB over fourteen days. Physical storage projection PASS; logical registry capacity is already exhausted.

Research records: 3,848, with ten new records in the final window. The 100,000-record ceiling has no archival migration; at one record/min there would be about 20,160 additional records/14d, but bursts require monitoring. Existing chart/price/analysis dictionary caches have no eviction. Immutable financial/audit data was not pruned.

## Hardening applied and limitations

1. Cross-process atomic weighted REST admission, response-size accounting and 240-unit safety reserve.
2. Maximum-response preflight reservation; failures retain conservative cost.
3. Shared 429 cooldown with numeric Retry-After; no blind order retries.
4. Fixed 30s cache for meta/spotMeta/perpDexs only; network/dex-separated keys.
5. Cross-process metadata singleflight lease; no financial/price/history cache.
6. WS exponential backoff with jitter, capped 300s.
7. Position-owned monitoring assigned safety resource priority.
8. HISTORY_INCOMPLETE causal evidence preserved.
9. Read-only API budget Health card.
10. Chart budget exhaustion returns 503 MARKET_API_BUDGET_DEFERRED, not 500 or invented empty candles.

The governor is admission priority, not a fair scheduler; P0/P1 share capacity. Conservative candle preflight reserves up to 5,000-bar cost even when fewer bars are requested. A dedicated sustained-critical-budget Telegram alert is not wired; Health exposes it. The unused operator recovery CLI has an ungoverned urllib transport and must not run concurrently without coordination.

Backup manifests were validated before each deployment; latest archive `runtime-20260909T070456Z-3c5dca30cd5d47df8ed9291eb9b699c8.tar.gz` includes 19 integrity-checked databases. An earlier backup restored 17 SQLite files into isolated temporary files successfully. Archives are owner-only but **not encrypted**, despite the old service description. No restore into production occurred.

## Verification and decision

- Full Python: 1,057/1,058, one unchanged environment skip, zero failures/errors.
- JavaScript: 141/141. Safety: 71/71.
- Targeted budget/history/product: 120/120; subsequent product/chart: 50/50.
- Browser: 462 checks, 108 screenshots, widths 375/390/430/1280/1440/1920; rendered Health inspected.
- Authenticated read models, leader detail, events/resume and SSE succeeded. BTC chart returned 97 candles plus price; configured deferral is separately tested.
- All three services active. Last chart fix restarted only Web, not PAPER. Served hashes and Telegram menu release verified.
- Zero real MAINNET orders. No production financial state administration during this audit.

**PARTIAL; fourteen-day unattended full-product readiness NO.** Resource protection is deployed and current weighted API load has headroom. Registry saturation, historical coverage, WS draining, incomplete scans and unproven deployed PAPER lifecycle remain. Continue only monitored PAPER collection with the unchanged baseline; no profitability/live certification is implied.
