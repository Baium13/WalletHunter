# Terminal UI v1

The primary entrypoint loads only product-model.js and product.js. Old scripts
remain available for compatibility tests but do not own the primary DOM.
Snapshot/event reads use product-v1. The only command routes are existing
Manual Copy PUT and exact ID/hash LIVE confirmation POST. Display mode is not
execution authority. No LIVE_AUTO control exists.

Product market/candles is a minimal authenticated, network-scoped wrapper around
the existing cached chart and mid-price reads. Prices are REST receipt evidence,
not fabricated exchange timestamps. Position PnL is a marked estimate excluding
fees; closed episode PnL remains the authoritative outcome. No candle is used as
a substitute for a missing live mark. Missing stop prices are not fabricated.

Episode IDs identify autonomous detail. Account position identity includes scope,
instrument, side and exact exchange order IDs; its timeline includes only linked
canonical intents/risk/receipts, not an invented autonomous decision history.
The backend's top-32 leader detail/history limits remain explicit. Charts show
latest-100-outcome realized-PnL history, not an invented account equity curve.

Keyed DOM reconciliation preserves canvases, focus, scroll, detail expansion and
dirty Manual Copy input. One authenticated SSE connection resumes its cursor,
deduplicates IDs, bounds buffers and reconciles snapshots. No legacy polling.
Private data is masked; financial charts disappear in privacy mode. Preferences
are device-local only. No financial state is stored in localStorage.

Build/check: `python scripts/build_frontend.py [--check]`. Asset URL versions
derive from SHA256; product-release.json records the exact served assets.
Browser QA: `node scripts/ui_terminal_visual.cjs OUTPUT_DIRECTORY` with an
already-installed Playwright on NODE_PATH. It uses fresh synthetic Edge contexts,
blocks external page requests/WebSockets and service workers, and never starts
the production server. This is not an OS-level browser egress certificate.

Deployment attempt: SSH with StrictHostKeyChecking=yes and BatchMode=yes failed:
`Load key .ssh_wallethunter_tmp.key: Permission denied` then publickey failure.
Operator must make the intended SSH key readable by the current Windows user
with owner-only ACLs, or provide an accessible approved identity; verify the
known host. No ACLs or host verification were relaxed. Linux path, services,
Telegram menu URL and served release remain unverified. Do not deploy only assets
without the product market API wrapper. Do not copy databases/configuration.

Original futuristic reference image was not attached to this request or present
in the project assets; the implementation follows the supplied visual direction.
