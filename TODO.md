# TODO

Known follow-up work, roughly in priority order.

## redo hotkeys for Sonos panel

Stop isn't currently available and the status bar is a mess


## Keep grouped speakers' status in sync on the expanded panel

**Priority:** medium · **Scope:** `home_control/systems/sonos.py`
(`SonosController` polling)

### Problem

When speakers are grouped, the expanded panel's per-speaker status rows drift out
of sync after a transport action. Example: speakers grouped and paused. Hit ENTER
on Living Room — it goes LOADING for ~1s then PLAYING, but Kitchen keeps showing
PAUSED for ~3 more seconds until the next full poll catches up:

```
▶ Living Room   ▶ PLAYING    +  ━━━━●───────  40%  Calabria 2008  —  ...
  Kitchen       ⏸ PAUSED     +  ━━━●────────  35%  Calabria 2008  —  ...
```

Physically they're one group and change transport together, so the stale row is
purely a display lag: `_poll_active_fast` only refreshes the active zone, and
grouped members share the coordinator's transport state.

### Proposed direction

When the active zone is grouped, fan the fast-refresh's transport/track state out
to every zone in the same group (they share the coordinator), instead of updating
only `active_idx`. Alternatively, after a transport command on a grouped
coordinator, mark all group members dirty so the next frame reflects the change.

## Make Sonos command handlers non-blocking (main-thread network)

**Priority:** high · **Scope:** `home_control/systems/sonos.py` (Sonos first;
other systems have the same latent issue but far shorter timeouts)

### Problem

`SonosSystem.handle_key` runs on the main thread, and its handlers make blocking
SoCo calls **while holding `SonosController._lock`**. This freezes the entire TUI
for the duration of the network round-trip — against a dead *or* a working
speaker:

- **Transport actions** (`play_pause`, `stop`, `next_track`, `prev_track`,
  `play_queue_index`) call `_refresh_active_after`, which does
  `time.sleep(0.3)` **on the main thread** plus two more round-trips. That's a
  guaranteed ~0.4s hitch on every transport keypress, even on a healthy LAN.
- **`fetch_device_info`** (the `d` overlay) reads ~20 SoCo properties
  sequentially under the lock — ~0.6–1s of frozen UI on a healthy speaker.
- **`load_queue` / `load_favorites`** — one list fetch each, tens–hundreds of ms.
- Against an *unreachable* speaker, any of these blocks up to the request
  timeout (4s, or 10s on SoCo's hardcoded SCPD-fetch path — see below).

This is the same class of bug as the idle-freeze fixed on this branch
(`f5cb834`), just on the keypress path instead of the poll path. The governing
invariant: **neither the poll thread nor a command handler may block the main
thread on network I/O, nor hold `_lock` across it.**

### Proposed design

Mirror the poll-thread model: a small per-controller **command queue + single
worker thread**. `handle_key` enqueues an action and returns immediately; the
worker executes the (networked) command off the main thread and updates the
cached snapshot, which the next render picks up.

This fits cleanly because most handlers **already** update the cached state
optimistically (`zone.volume`, `zone.muted`, play-mode flags), so the UI reflects
the change on the very next frame while the round-trip and the 0.3s settle happen
off-screen. Move `_refresh_active_after`'s `sleep`+re-poll onto the worker too.

### Done when

- No Sonos keypress blocks the main loop (verify with the `HOME_CONTROL_PROFILE`
  main-loop profiler used to diagnose the idle freeze — no `SLOW PANEL` lines
  while hammering transport keys against a reachable *and* an unplugged speaker).
- Optimistic UI still updates instantly; failed commands reconcile from the
  device's real state (never leave an optimistic value the device rejected).
- A regression test guards that command handlers don't hold `_lock` across the
  network (same second-thread lock-probe pattern as
  `test_discover_resolves_order_without_holding_lock`).

### Notes / gotchas

- SoCo hardcodes `timeout=10` for the SCPD service-description fetch
  (`soco/services.py:712,778`) — it ignores `config.REQUEST_TIMEOUT`, so our 4s
  cap doesn't cover that path. Off the lock it's only background churn, but it's
  why a first-touch of an unreachable speaker can still take ~10s to fail.
- Then apply the same worker-dispatch pattern to the other systems (Hue
  `_set_light`, Roku `_post`, Router auth) — lower priority (short timeouts), but
  the same main-thread-blocking smell.

## Unify row-selection styling into shared ui helpers

**Priority:** low · **Scope:** `home_control/ui.py`, all panels (`hue.py`,
`sonos.py`, `midea.py`, `roku.py`)

### Problem

Every panel re-implements "this row is selected" styling, and they've drifted,
so selecting a light, a speaker, and an AC don't guarantee the same look:

- **Cursor**: `Seg("▶ ", self.color, bold=True) if sel else Seg("  ")` is
  copy-pasted in ~6 places (Hue `_room_row`/`_light_row`/scene/device rows,
  Sonos `_zone_row`, Midea).
- **Whole-row bold**: Hue has a `_highlight()` method (bold every seg, clear
  dim); Sonos `_zone_row` inlines the same loop; `ui.select_row` bolds only the
  single string it draws. Three implementations of one idea.
- **Widget brightening**: the Hue brightness bar and the Sonos volume bar each
  need an *explicit* `lighten()` on select (bold can't brighten their colours,
  and on the heavy bar glyphs bold weight barely reads) — computed separately in
  each file. Meanwhile badge/text colours still lean on the terminal rendering
  bold as bright, so the brightening story is inconsistent even within one row.
- **Two incompatible abstractions**: `ui.select_row` draws straight into a
  Region, so rows built as a `Line` (list of `Seg`) can't use it and roll their
  own instead.

Any tweak to the selection cue currently has to be made in several places.

### Proposed direction

One `Line`-based selection primitive in `ui.py`, e.g.
`select_line(line, *, selected, accent) -> Line` that:

- prepends the standard `▶ `/`  ` cursor in the accent colour,
- applies the whole-row bold + un-dim,
- brightens marked segments explicitly instead of relying on bold-as-bright —
  likely via a `Seg` flag (e.g. `brighten=True`) tagging accent-coloured widgets
  (bars, knobs) so the helper swaps in `lighten(color)` on select. Text keeps
  bold (its weight reads as selected regardless of terminal).

`ui.select_row` then becomes a thin wrapper that draws the result; each panel's
`_*_row` builds a plain `Line` and hands it to the helper. Retire Hue's
`_highlight` and Sonos's inline bold loop.

### Done when

- Hue, Sonos, and Midea selectable rows all route through the shared helper; no
  panel re-implements the cursor or the bold loop.
- Filled-bar/slider brightening is terminal-independent (explicit lighten) for
  every panel, consistently.
- Selection looks identical across panels, modulo each system's accent colour.
