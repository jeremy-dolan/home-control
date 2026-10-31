# TODO

Known follow-up work.

## redo hotkeys for Sonos panel

Stop isn't currently available and the status bar is a mess

## Roku badge work

Done: badge labels pad to `BADGE_W` and both the collapsed line and the
expanded header start their detail at `DETAIL_COL` (`BADGE_W + 2` = 11,
matching Sonos), so the column no longer shifts between `▶ PLAYING` /
`⏸ PAUSED` / `■ IDLE` / `● ASLEEP`, and the two panels line up with each other.
"Roku Dynamic Menu" is rewritten as "Roku Menu", detected via the active-app
`type="home"` attribute rather than the display string (which varies by
firmware).

Still open: which media-player states deserve a badge of their own. An idle
player reports `state="close"` (live, OS 15.3.4) and that is the only state
observed so far; Roku also documents `none`, `startup`, `buffer`, `play`,
`pause`, `finished`, `stopped`, and `error`, and `badge()` funnels every
unobserved one into `■ IDLE`. Two look wrong there: `buffer` is active work and
wants a LOADING badge like Sonos's `⟳`, and `error` renders as a calm IDLE
rather than FAULT. Confirming either needs live observation of a state we can't
trigger on demand. (Standby turned out *not* to be one of these — it's a
separate `power-mode` axis, now handled.)

## Block find_remote when the device says it can't

`/query/device-info` reports `supports-find-remote: true` next to
`find-remote-is-possible: false` (live Roku Ultra, 2026-08-01) — the second is
the one that matters, and it's the one we don't read. The `find_remote` voice
action fires the ECP keypress regardless, so it silently does nothing and
reports success. Gate the action on `find-remote-is-possible` and say why it
can't run instead of pretending it worked.

## Roku device-info fields we don't surface

An audit of the ECP `query/` endpoints against what `roku.py` parses (live Roku
Ultra, OS 15.3.4, 2026-08-01) found we read 5 fields of ~75. Enough for a
device-info overlay someday — Roku has none, Sonos and Hue both do:
`network-type` (ethernet/wifi) plus the wifi/ethernet/bluetooth MACs,
`user-device-location`, `serial-number`, `model-number`, `ui-resolution`,
`uptime`, and the timezone block (`time-zone`, `clock-format`,
`time-zone-offset`).

## Hue: a bridge that answers with an error isn't unreachable

Since phue2 0.1, `Bridge.request` raises `PhueAPIError` when the bridge answers
with a Hue error object — proof the bridge is up. `HueController.poll()` still
treats every exception from `_fetch_state` as unreachability: it drops the
handle and calls `reach.failed()`, so after the grace period the panel says the
bridge is unreachable. The likely real case is a revoked app key (Hue error 1,
"unauthorized user"), and rebuilding the handle can't recover from it: with a
username cached in `~/.python_hue`, `Bridge()` never re-registers. Catch
`PhueAPIError` separately — report it through `error` rather than `reach` — and
on error 1 offer re-pairing (forget the cached username, prompt for the link
button).

## Hue: reuse one HTTP connection

phue2 0.1 accepts `Bridge(http_client=httpx.Client(...))`; without it every
request opens a fresh client and TCP connection — two per poll tick, every
second while the panel is focused. The client is caller-owned, so its timeout
replaces `Bridge(timeout=CONNECT_TIMEOUT)`, and it should live on the
controller, not the handle `poll()` drops and rebuilds on failure. Confirm it's
safe to share between the poll thread and main-thread commands.

## Midea: port to current midea-local

`pyproject.toml` caps midea-local below 7 (a3d5866): a fresh install had
resolved 15.0.0, which breaks the Midea code. 6.11.1 is the version verified on
real units; 7 through 14 are untested, so first find which major broke us. The
two 15.0 changes found so far (the commit message has the detail):

- `dev.capabilities` is gone. `midea.py` reads it in `_caps_ready`,
  `_unit_from_device` and `device_info`. 6.11's property just returns
  `self._capabilities`, which 15.0 keeps, so reading that works on both — or
  find 15's public replacement, if there is one. The suite's fake device still
  has `.capabilities`, which is why the tests never caught it.
- `dev.attributes` hides `screen_display`, `indoor_humidity`, the swing angles,
  `anion`, `rate_select` and `sound` until the unit's B5 reply reports the
  matching capability. A unit with a display that doesn't claim
  `display_control` would lose its display chip. The A0 push replay tests need
  a B5 frame first; the real ones are in
  `~/midea-samples/frames-display-off-2026-08-02.txt` (t=4.5 and 4.6), and
  that unit does report `display_control`.

The captured C0/A0 frames parse identically on 6.11 and 15.0, and the A0 push
still clobbers `screen_display`, so `_ignore_push_padding` stays. Connecting,
cloud pairing, commands and whatever else changed across nine majors can only
be checked against the units — all three, since capabilities vary per unit.

## setup CI on GH

Run pytest, ruff and pyright on push. The docs-sync test
(`tests/test_docs_sync.py`) only catches drift if something runs it.

## have claude investigate linting issues
I think we have a bunch of unrefered vars

## add Roku to demo GIF


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
  the same main-thread-blocking smell. Midea's worst case is already gone
  (`45746be` dropped a `time.sleep(0.3)` + re-read from `apply_edit`, which
  stalled the UI on every keypress *and* raced the ack); what remains there is
  the fire-and-forget `socket.send` itself, with no round-trip to block on.

## Unify row-selection styling into shared ui helpers

**Priority:** low · **Scope:** `home_control/ui.py`, `roku.py`, `sonos.py`

### Status

Mostly done. `ui.cursor(accent, sel)` is the single source for the `▶ `/`  `
cursor, and `ui.highlight(line, accent)` does the whole-row treatment: bold
every segment, clear dim so the bold reads, and lift any segment carrying the
accent to `lighten(accent)`. Hue and Sonos route their rows through it; Hue's
`_highlight` and Sonos's inline bold loop are gone, and neither the brightness
bar nor the volume bar computes its own `lighten()` any more.

The implementation inverted the flag this entry originally proposed. Rather than
opting widgets *in* with `brighten=True`, `highlight()` lifts everything already
carrying the accent and segments opt *out* with `Seg(lift=False)` — which the
cursor uses, so the marker itself stays at the base shade while the row it marks
brightens.

### What's left

- **Two abstractions still.** `ui.select_row` draws straight into a Region
  (Roku's app list, Sonos's queue and favorites), so plain-text rows and
  `Line`-built rows take different paths. They share `cursor()` now, but there
  is no single entry point. Folding `select_row` into a thin wrapper over
  `highlight()` is the remaining cleanup.

  From the UI review: `select_row` writes plain strings via `region.text`, so
  its selected rows get cursor + bold but no accent lift — harmless only because
  a plain-text row carries no accent segment to lift. The migration is therefore
  mechanical: wrap the row text in a single default-colour `Seg`, run it through
  `cursor()` + `highlight()`, draw the resulting `Line` — behaviour preserved
  (bold, nothing to lift). Best done alongside the shared-widgets consolidation
  ("Shared editable-field + numeric-entry widget" / "Shared scroll-clamp helper"
  below): all three are the same "promote a per-panel pattern into `ui.py`" move.

### Not doing: identical selection in every panel

The original "done when" asked that selection look identical across panels. That
is too strong, and Midea is the counterexample — leave it as it is.

Midea marks selection with the cursor alone; it never calls `highlight()`. Its
`_dim()` is a *state* cue, not a selection one: an off or unreachable unit is
dimmed whether or not it is selected (which is why `_card_rows` restores the
accent cursor over the dimming — an off unit is still selectable, to power it
back on). Row-bolding a Midea card would collide with that, since dim already
means "off/unreachable", and the panel shows only ~3 always-expanded 3-line
cards where a whole bolded card reads as noise rather than focus.

The convention to document instead: **the cursor is the guaranteed selection
cue in every panel; row-bold + accent lift is an optional reinforcement that
dense scrolling lists (Hue, Sonos) use and card layouts (Midea) do not.** — now
done, stated in the "UI conventions" block at the top of `ui.py`.

## Shared editable-field + numeric-entry widget

**Priority:** low · **Scope:** `home_control/ui.py` (or a new `widgets.py`),
`home_control/systems/hue.py`, `home_control/systems/midea.py`

There's a latent "editable field + numeric entry" widget trying to exist.
`hue.py` and `midea.py` each define their own `EditableField` dataclass *and*
their own near-identical `_num_buf` / `_handle_num_entry` typed-entry state
machine (digit capture → backspace → ESC-cancel → ENTER-commit-with-clamp).
That's the same interaction implemented twice, differing only in field
metadata. A small shared widget — an enum-cycle + numeric-entry buffer living
in `ui.py` (or a new `widgets.py`) — would collapse both. This is the biggest
DRY win.

## Shared scroll-clamp helper

**Priority:** low · **Scope:** `home_control/ui.py`, `hue.py`, `midea.py`,
`roku.py`, `router.py`

Every panel re-implements scroll clamping. `hue._clamp_scroll_rows`,
`midea._clamp_scroll`, `roku._clamp_scroll` (free fn), and `router` inline all
do "keep cursor visible, clamp scroll to `[0, total-visible]`." Four copies of
one function. A single `clamp_scroll(cursor, scroll, visible, total)` in `ui.py`
(or a tiny `ScrollView`) removes them. Cheap, uncontroversial.

## Promote a shared toggle_chip

**Priority:** low · **Scope:** `home_control/ui.py`, `midea.py`, `sonos.py`

`midea._toggle_chip` is the nicest realization of the toggle convention
(mnemonic first letter accent, whole chip accent+bold when on). If it lived in
`ui.py`, Sonos's play-mode dots could adopt it — which would also settle the
"same glyph, different emphasis" question the task flagged as undecided. My
recommendation on that question: **enabled = accent+bold, disabled = dim**,
everywhere. Sonos's dots-stay-dim-both-ways reads as a bug next to Midea.

## YouTube Music search + playback in the Sonos panel

**Scope:** `home_control/systems/sonos.py`

### Goal

Search YouTube Music from the panel and get a result actually playing on a
speaker.

### Why this isn't a simple search box

Sonos's own app can search-and-play YTM because it's a registered client with
Google's SMAPI — Sonos owns the signed API key for that trust relationship, we
don't. Every path we tried to get around that is a dead end, live-verified:

- The old `x-sonos-http:{videoId}.mp4?sid=284` URI scheme is dead — UPnP
  error 800 on add; bare videoIds are rejected at `AddURIToQueue` time.
- Direct SMAPI search (`soco.music_services.MusicService.search()`) calls
  Google's cloud endpoint straight from our machine using an AppLink/OAuth
  token that requires Sonos's own client registration — `music.googleapis.com`
  returns 403 "unregistered callers". This is structural (confirmed at the
  `soco` source level: `soap_client.endpoint` is the service's own cloud URI,
  not the local speaker), not a bug we can work around.
- Sonos's official Cloud Control API (`api.ws.sonos.com`, OAuth-linkable like
  we do for Midea) doesn't help either — its content-discovery surface is
  only `getFavorites`/`loadFavorite` and `getPlaylists`/`loadPlaylist`, plus a
  `musicServiceAccounts.match` endpoint for *linking* an account. No
  search/browse-into-a-service endpoint exists. That capability lives in
  SMAPI, which is a Sonos↔Google trust relationship, not something exposed to
  third-party control apps.
- Tried having the *speaker itself* resolve a search via its local
  `ContentDirectory` UPnP service (it already holds a valid SMAPI session, so
  in theory it could proxy a query without us needing our own credentials).
  Live-tested: root `Browse` exposes only local containers (`A:` library,
  `S:` shares, `SQ:` saved queues, `R:` radio, `FV:` favorites, `Q:` queues)
  — no per-service container. Probing candidate service-root object IDs
  (built from the account's own desc token) returned `UPnP Error 701: No
  such object`. Not browsable on current firmware.
- No voice assistant gets around this either, for the same reason: Alexa
  doesn't support YouTube Music as a linked service at all (business
  decision, not technical), and Siri's Sonos control is hard-locked to Apple
  Music via AirPlay 2. The only assistant that ever had a real path (Google
  Assistant, talking to Google's own service) is being actively phased out of
  Sonos hardware amid Sonos/Google patent litigation, not expanded.
- Enqueuing a favorited YTM playlist/album *container*
  (`x-rincon-cpcontainer:…` + its `resource_meta_data`) **does** work — the
  speaker expands it into native HLS tracks via its own SMAPI session.
  Verified: a 21-track album expanded fine. This is the one thing we can
  build on.
- `ytmusicapi`: unauthenticated search works (no cookies needed). Writes
  (`create_playlist`, adding items) need authenticated cookies.

### Chosen design — playlist trampoline

1. **Search** — unauthenticated `ytmusicapi` search, shown in the panel.
2. **Write** — on selection, authenticated `ytmusicapi` replaces the contents
   of one dedicated private YTM playlist (e.g. "home-control") with the
   chosen track(s).
3. **One-time setup** — the user favorites that playlist in the Sonos app
   once, giving us a Sonos Favorite whose URI points at a container the
   speaker already knows how to expand.
4. **Play** — `AddURIToQueue` that Favorite; the speaker re-expands it with
   the new contents via its own linked session.

Not instant single-track playback — there's a beat of indirection (rewrite
playlist → reload favorite) instead of one direct "play this" call.

### Live bug to fix along the way

`soco`'s `add_uri_to_queue()` does *not* take metadata — its 2nd positional
arg is queue position — but `SonosController.play_favorite` currently passes
`fav.metadata` there (`home_control/systems/sonos.py`). Enqueuing a container
favorite needs the raw `avTransport.AddURIToQueue` SOAP call with hand-built
DIDL (musicTrack/container class + the account's desc token) instead.

### Open risks

- Unverified whether SMAPI serves *fresh* playlist contents on every
  expansion, or whether the speaker caches — needs a live test once we have
  working write auth.
- Current cookie file for `ytmusicapi` writes is stale (401 on
  `create_playlist`); needs to be refreshed before this can be built end to
  end.

### Live-testing safety

Safe-test protocol: only ever send commands to a speaker confirmed idle and
cleared for testing (never one that could wake people).

### Alternative worth considering: connect Spotify instead

Spotify assigns tracks a stable, public catalog ID (`spotify:track:…`) — the
same ID from Spotify's own public Web API as from inside the Sonos app —
unlike YTM's Sonos-minted opaque IDs. That means `x-sonos-spotify:` queue URIs
can be built directly from a search result and `AddURIToQueue`'d with no
trampoline needed: real single-track search-and-play, immediately. This is a
well-established community pattern (`node-sonos-http-api`, `sonoscli`,
multiple `SoCo` issues), not yet live-verified against our hardware — would
need Spotify actually linked to the Sonos system first. If Spotify covers the
use case, it's a substantially better UX than the YTM trampoline and worth
building instead.

## Config wizard

On first run, or with a command line arg. Use existing discovery code
interactively, populating ~/.config/home-control/config.toml with user
confirmation.
