# Demo GIF sources

`assets/demo.gif` (embedded in the top-level README) is built from hand-made
80×50 ANSI frames, not a live recording, to keep private household details
out of it.
Each `demo-N/` keeps the sources for one iteration plus the GIF it produced;
`assets/demo.gif` is a copy of the newest one.

- `demo-1/`: the original two frames (router, lighting).
- `demo-2/`: router ×3 (chart ticking), lighting, lighting + voice, Roku,
  Roku + voice. `make-gif.sh` holds the frame order and timing.

## Making a GIF

1. In iTerm at 80×50, `source set-iterm-title` (sets the window title the GIF
   shows), then `cat` a frame.
2. Screenshot the window with its shadow (⌘⇧4, Space, click) and name it as
   `make-gif.sh` lists (`ss1.png` …). The PNGs are git-ignored: regenerable,
   and ~750 KB each.
3. `./make-gif.sh` (needs ImageMagick 7) crops the shadow, builds a shared
   palette and writes `demo.gif`; copy it over `assets/demo.gif`.

## tools/

Generate frames from others so ticks and overlays match the app exactly. Each
build script takes an iteration folder, reads its inputs there and writes its
outputs there; outputs are deterministic, so re-running overwrites them
identically.

| Script | Reads | Writes |
|---|---|---|
| `build.py` | `2-router.ans`, `4a-lighting.ans` | `3-router.ans` (chart one tick on), `4b-lighting-and-voice.ans` |
| `build_roku.py` | `1-router.ans`, `4a-lighting.ans`, `4b-lighting-and-voice.ans`, `roku-mock-capture.ans` | `5a-roku.ans`, `5b-roku-voice.ans` |

Run with the repo venv: `.venv/bin/python assets/demo/tools/build.py assets/demo/demo-2`.

- `ansgrid.py`: parses a `.ans` into a grid of `(char, Style)` cells and
  serializes it back. Edit frames on the grid, never on raw escape codes.
- `backdrop.py`: the app's colour maths on cells, imported from `home_control.ui`:
  `recede()` is the popup backdrop dim (×`BACKDROP_SCALE`), `lighten()` the
  hotkey/focused-border lift. `TERM_ANSI` holds basic-ANSI colours as the
  maintainer's terminal theme renders them (sampled from a screenshot).
- `png.py`: dependency-free PNG decoder, for sampling screenshot pixels.
- `demo-2/roku-mock-capture.ans`: `tmux capture-pane -e` of the app in
  `HOME_CONTROL_MOCK=1` with Roku focused (see the `verify` skill), unedited.
  `build_roku.py` keeps only its expanded Roku panel; every other row of
  `5a-roku.ans` comes from the hand-made frames. tmux snaps colours to the
  256-cube, so `build_roku.py` maps them back to exact palette hex.

Overlay geometry (Voice box size, examples, colours) comes from `app.py`
constants, so a change to the real overlay flows through on the next build.
Row/column offsets for panels and the router chart are hardcoded to demo-2's
layout: a demo-3 would copy and adjust the build scripts, not reuse them as-is.
