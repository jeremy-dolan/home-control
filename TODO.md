# TODO

Known follow-up work.

## redo hotkeys for Sonos panel

Stop isn't currently available and the status bar is a mess

## Security

Midea token/key material is written to a JSON cache without explicitly setting
restrictive permissions, and the main config may hold plaintext router, Midea,
and Anthropic credentials. On a normal modern macOS setup the parent
directories are usually user-private, but the code should enforce 0600 itself.

see also: https://chatgpt.com/c/6a56a686-1e48-83ea-8eff-301762040029

## Roku badge work

■ IDLE    Roku Dynamic Menu   <-- when collapsed
■ IDLE      Roku Dynamic Menu <-- when expanded

if we add one to the collapsed, and remove one from expanded, it will align
with the display for Sonos.  Not sure if aesthetically aligning is good, or
"too straigtht"

There's also this comment:

def badge(state: str) -> tuple[str, bool]:
    """(label, dim) for a media-player state; unknown/idle states get IDLE."""
    return _BADGE.get(state, ("■ IDLE", False))

I would be curious to know more about what the 'unknown' states are, that we're
hiding by using IDLE as a default.

Finally, ■ IDLE should be in grey, not purple, and we should substitute "Roku
Dynamic Menu" for something less branded and wordy. Maybe "Home"?

## add tests/CI for docs sync
we've added a test to ensure example config doesn't drift, but still need one
for in-app help vs. README.md

setup CI on GH

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
  the same main-thread-blocking smell.

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
dense scrolling lists (Hue, Sonos) use and card layouts (Midea) do not.**

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

## Install directions

These would make sense:

python3 -m venv ~/.local/share/home-control/venv
source ~/.local/share/home-control/venv/bin/activate
pip install git+https://github.com/jeremy-dolan/home-control.git
ln -s ~/.local/share/home-control/venv/bin/home-control \
      ~/.local/bin/home-control

however, probably should direct users to make an 'editable' install so it
can be extended to their devices

## Sync clock option for the Lighting panel

**Priority:** low · **Scope:** `home_control/systems/hue.py` (bridge-info
sub-mode, `HueController`)

### Problem

The bridge-info view (`b` from the Lighting panel) checks the bridge clock
against the local one and flags drift over `CLOCK_DRIFT_WARN` (60s) by
suffixing the "UTC time" and "Local time" rows with " (out of sync?)" in
`fault` red. That is the right severity — a drifted clock silently misfires
any schedule the bridge runs — but the panel only reports it. There is no way
to act on it without leaving the app for the Hue app or the bridge's web UI.

### Proposed direction

A hotkey in the bridge-info sub-mode (the sub-mode has no key of its own yet)
that pushes the correct time to the bridge, with the usual command treatment:
run off the main thread, confirm via `set_status`, re-poll the config after.

Needs API research first — do not assume the shape:

- The v1 CLIP API exposes the clock under `PUT /api/<user>/config`, and the
  drift check already reads `UTC` back from `GET .../config`. Whether that
  field is writable (versus read-only and NTP-managed) has **not** been
  verified against the real bridge — check before designing the UI around it.
- Bridges normally keep time over NTP themselves, so persistent drift may mean
  the bridge cannot reach an NTP server rather than that its clock needs a
  nudge. If so, a one-shot "set the time" would silently drift again and the
  honest fix is to surface *why* (no internet / blocked NTP) instead. The
  bridge config's `internetservices` block already reports internet
  reachability and is displayed two rows below the drift warning.
- If the field turns out to be read-only, the fallback is to drop the action
  and instead make the warning explain itself in the `?` help.

### Done when

- The drift warning is actionable, or is documented as un-actionable with the
  reason shown in-panel.
- Whatever the resolution, the behaviour is covered by a mock-mode test — the
  fixture bridge clock is fixed at `2026-07-21`, so the drift path is always
  live under `HOME_CONTROL_MOCK=1`.
