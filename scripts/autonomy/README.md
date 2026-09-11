# Autonomous runtime configuration

Two templates, deliberately identical apart from the three things that must
differ. Running PAPER on the same allocation and risk numbers as LIVE is the
only way PAPER is a control rather than a decoration.

| | `paper-auto.example.json` | `live-auto.example.json` |
|---|---|---|
| `authorization.mode` | `PAPER_AUTO` | `LIVE_AUTO` |
| `initial_paper_equity` | opening balance | ignored — a live scope opens on exchange evidence |
| `live_guard` | absent | **required**; the backend refuses to construct without it |

Copy a template, replace `TELEGRAM_USER_ID` and the zero address, and review
every number. Nothing below is a recommendation — they are placeholders that
parse.

## What the numbers mean

`allocation.allocation_limit` is the total USD of margin this strategy may ever
hold. `other_allocation_limits` lists margin already committed to manual copy
or AI slots, which is subtracted from the base so two strategies cannot spend
the same dollar. `entry_fraction` is the share of what remains that a single
entry may take **at full conviction** — weighted consensus scales it down from
there, so this is a ceiling, not a typical size.

`allocation.max_leverage` and `risk.max_leverage` are both ceilings. The actual
leverage is the lowest of: the leverage the leader is running, the venue's
ceiling for that market, and these two. A stock capped at 3x is sent 3x no
matter what is written here.

`risk.min_notional` is where an order stops being worth its fees. Sizing lifts a
small order to the smallest lot the venue can express if the allocation covers
it, and refuses otherwise — it never rounds silently to zero.

`risk.max_spread_bps` and `risk.minimum_depth_usd` are enforced by the gateway
on **entry only**. An exit is never blocked by a bad book.

`multi_instrument: true` lets one policy follow a leader into every market on
the same network and venue. With it false, anything but `risk.instrument` is a
scope error — which is what silently stopped the pipeline before. `symbols`
empty means no allow-list; fill it to restrict which markets may be copied.

## The live guard

Account-level stops that sit above the per-order risk checks. They answer a
different question: not "is this order sound" but "should this account be
opening anything at all right now".

- `max_concurrent_positions` — refuses new entries while this many are open.
- `daily_loss_limit` — USD drop from the UTC-day opening equity that halts new
  entries. It latches: equity recovering does not re-arm it, only a new UTC
  day does. `0` disables it.
- `halted` — operator kill switch. Set true, restart, and no new entry is
  authorized.

**Reducing actions are never gated by any of these.** A stop that traps an open
position is worse than the condition it reacts to.

## Environment

The live consumer is built only when all three are set. Any one missing and the
watcher does nothing at all — that is the off switch.

    LIVE_AUTO_CONFIG=/path/to/live-auto.json
    LIVE_AUTO_STATE_DIRECTORY=/path/to/state/live
    LIVE_AUTO_RESEARCH_DB=/path/to/data/intelligence.sqlite
    LIVE_AUTO_INTERVAL=5

`PAPER_STATE_DIRECTORY`, `SHADOW_STATE_DIRECTORY` and `LIVE_AUTO_STATE_DIRECTORY`
also decide which runtimes appear in `/api/autonomy`. A runtime that is not
configured is reported as not configured; it is never faked.

Each runtime needs **its own** state directory. Two consumers sharing one would
share a ledger and an episode table, and the mode guard inside refuses anyway.

## Before enabling LIVE

1. `authorization.scope.account` must be the address already connected to that
   Telegram profile. `_live_auto_ready()` re-reads the profile and refuses if
   they disagree — it will not trade an account you have not connected.
2. Let PAPER run on the same numbers first and read `/api/autonomy`. If PAPER
   shows no outcomes, LIVE will not produce any either.
3. Start with an `allocation_limit` you would be relaxed about losing entirely,
   and a `daily_loss_limit` well inside it.
4. The signing client is constructed by `desktop/main.py` alone. Neither the
   research worker nor this configuration can sign: `load_paper_backend`
   rejects a `LIVE_AUTO` config unless the caller hands it an adapter, which is
   what keeps the worker unable to place a live order even if pointed at a live
   config by mistake.
