# Trading / compact UX repair — 2026-09-09

- Production Manual Copy parent `09be505a6a6e4399943d8877b1863d90` reached Hyperliquid after the user's START. Cloid `0x8f8c4b978c1b1ea5e5d7636369004e0f`, order `540103021344`, SELL BTC 0.00142 at 79641: `iocCancelRejected`. Order status and historical orders agree; subsequent fills and open positions are empty.
- The reconciler previously admitted only four terminal status strings. An explicit documented placement rejection was incorrectly kept UNKNOWN. The allowlist now includes documented placement rejections; identity, timestamps, fills, position-delta and scope proofs remain mandatory. Unknown/unrecognized responses remain UNKNOWN.
- Copy price rounding previously fell back to the midpoint when nearest rounding crossed the slippage boundary. Inward directional tick rounding now retains the approved price allowance without widening it. Default non-copy rounding is unchanged.
- Query-only canonical recovery resolved the intent and parent as REJECTED after a validated 19-database backup. The historical reservation envelope remains audit evidence; terminal status removes it from active reservation accounting. No resubmission or manual row deletion occurred. Manual Copy was paused with its existing generation preserved.
- Two historical exchange endpoint calls predate that pause. They are not two fills. Current BTC leverage reads CROSS 40; its prior value is not proven. No claim of unchanged historical leverage is made.
- PAPER remains session `paper-20260908T121351Z-07515b5c`, virtual $100, BTC-only. Fresh production read: $100 available, zero unresolved executions and zero actionable backlog. Latest CELO CLOSE was SKIP / NO_FOLLOWER_POSITION. The observed BTC OPEN had WAIT / STRONG_DISAGREEMENT / CONSENSUS_BELOW_THRESHOLD. These are not authorization to weaken the fixed policy.
- Main navigation: Home, Copy, AI, More. Positions, leader details, analytics, Activity and diagnostics remain accessible. Home distinguishes the financial runtime's last decision from public research. Technical panels are collapsible; no audit events are removed.
- A paused flat Manual Copy generation no longer polls an unowned selected leader at trading priority. Query recovery and owned-position HOLD observation remain intact.

Exchange status reference: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
