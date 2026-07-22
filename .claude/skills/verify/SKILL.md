---
name: verify
description: Launch and drive the home-control TUI to observe a change working (build/launch/drive recipe for runtime verification).
---

# Verifying home-control changes

The surface is the curses TUI. Drive it in an isolated tmux and capture
panes; use `capture-pane -e` when the change is about color/bold/dim
attributes (dim = SGR `ESC[2m`).

## Launch (from any checkout/worktree)

Default to **80×50** — the app is designed with an 80-column aesthetic in
mind (row count is flexible, anywhere in ~45-55 is representative; it should
also degrade gracefully at other geometries, but 80 wide is the one to match
visually by default).

The venv lives in the main checkout, which is also where a worktree's
`.git` file points — derive it rather than hardcoding a path:

```bash
MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")

tmux -L hcverify new-session -d -x 80 -y 50 \
  "cd $PWD && HOME_CONTROL_MOCK=1 HOME_CONTROL_LOG=${TMPDIR:-/tmp}/verify-tui.log \
   PYTHONPATH=$PWD $MAIN/.venv/bin/python \
   -c 'from home_control.app import run; run()'"
```

Gotchas:
- The venv's `home-control` entry point is an editable install of the
  **main checkout** — from a worktree you must set `PYTHONPATH=$PWD` (cwd
  precedes the `.pth` entry) or you'll run the old code. Sanity-check with
  `python -c 'import home_control; print(home_control.__file__)'` first.
- `HOME_CONTROL_MOCK=1` mocks every system wired into the shell (Hue, Sonos,
  Roku, Router, Midea) — none hit the real LAN or need network access. Midea
  gives 3 fixture units (Living Room on/COOL, Bedroom on/FAN_ONLY+filter
  alert, Office offline); the others have their own canned fixtures per
  system. (`systems/yoto.py` has no mock branch, but is not registered in
  `app.py` either.)
- pyright in a worktree needs `ln -s "$MAIN/.venv" .venv` (pyproject sets
  `venvPath = "."`); **remove the symlink before commit** — it shows up
  untracked.

## Drive

- TAB cycles panel focus at the shell level. Grep the capture for a
  panel-specific marker to know when you've arrived (e.g. Midea expanded
  cards show `Mode Auto Cool Dry Fan`); beware false matches — Hue and
  Sonos also list rooms named "Living Room"/"Bedroom".
- `?` opens the help overlay, ESC closes it. `q` quits.
- Every panel is safe to drive under `HOME_CONTROL_MOCK=1` — writes
  short-circuit into the in-memory fixture and never reach hardware. The
  focused panel's key hints render in its own toolbar, so they're already in
  your pane capture; `handle_key()` in the system's module is authoritative
  for anything the toolbar elides.
- Panels repaint from a cached snapshot that only refreshes on poll ticks —
  `poll_interval_focused` (1s) when focused, `poll_interval_idle` (5s) when
  not, and the last-registered panel starts unfocused. When verifying *how
  fast* something appears, drive the panel at its real interval and render
  through `render_expanded`; a fast hand-rolled poll loop makes a 5s stall
  look instant (that mistake shipped twice in `2143095`'s branch).

## Cleanup

`tmux -L hcverify kill-server`
