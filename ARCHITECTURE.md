# Architecture

How home-control is built — reference for human contributors and coding agents
alike.

## What this is

A curses TUI that unifies control of home devices (Philips Hue, Roku, Sonos, a
Verizon Fios router, Yoto, Midea AC) plus a voice-control mode that sends
transcripts to the Claude API for NLU (tool-calling, not a rules grammar).

## Commands

```bash
source .venv/bin/activate      # venv already set up with project + dev deps
pip install -e ".[dev]"        # ruff/pyright/pytest live in the `dev` extra, not the base install
home-control                   # run the TUI (entry point: home_control.app:run)
pytest                         # run the test suite
pytest tests/test_midea.py     # run a single test file
pytest tests/test_midea.py -k some_test_name
ruff check .                    # lint (line-length 120, extend-select I, UP)
pyright                         # type check (basic mode)
```

## The System contract

Each device integration is a `System` (`home_control/systems/base.py`) — two
halves sharing state:

- **Controller** — discovery, polling, commands. No curses. `poll()` runs on a
  background thread per system (`home_control/poller.py`); it must be
  thread-safe: mutate the cached snapshot, never touch the screen.
- **Panel** — `collapsed_lines()` / `render_expanded()` draw cached state;
  `handle_key()` runs only while focused, on the main thread.

The `Shell` (`home_control/app.py`) reaches devices only through the `System`
contract (focus, toolbar, help wiring); shared layout lives in `layout.py`,
box-drawing and color in `ui.py`. TAB/Shift-TAB changes focus at the shell
level, never delegated to a panel. Other keys: the focused panel's `handle_key`
gets first crack; unconsumed keys fall through to shell globals (`?` help,
`q`/ESC quit, SPACE voice). Exception: while the focused panel reports
`captures_text()` (text-entry, e.g. Roku's keyboard/search), the shell suspends
SPACE so it reaches the panel as a typed character.

Voice control layers on top: each `System` exposes `voice_actions()` →
`VoiceAction`s (name/description/handler/JSON-schema params); `voice/router.py`
turns these into Claude tool schemas, and a matched tool call dispatches to the
handler off the main thread, returning a short result string to the voice
overlay. `voice/controller.py` drives the push-to-talk state machine (listening
→ thinking → result); `voice/recognizer.py` is the pluggable STT layer.

Systems are registered in display order (top to bottom) in `_DEFAULT_ORDER` in
`home_control/systems/__init__.py`; `build_systems()` instantiates them.

Logging: the root logger writes to a file (`$HOME_CONTROL_LOG`, default
`~/.cache/home-control/tui.log`), never stderr — library `logger.exception()`
calls (phue2/soco/midealocal on network errors) would corrupt the curses
screen.

## Directory map

- `home_control/` — the package (tracked): `app.py` (shell/main loop),
  `systems/` (one file per device integration), `voice/`, `config.py`,
  `layout.py`, `poller.py`, `ui.py` (curses drawing primitives, colors).
- `tests/` — pytest suite (tracked).
- `dev/`, `standalone-apps/`, `plugins/` — prototypes, network probes, and the
  original standalone single-device scripts this was built from.
  **Intentionally gitignored** (see `.gitignore`) — not shipped, don't add to
  them expecting them to end up in the repo.
- `README.ideas` — brainstorming notes, also gitignored.

## Config

Real config lives outside the repo at `~/.config/home-control/config.toml`
(override `$HOME_CONTROL_CONFIG`), auto-created from a template on first run.
That template is a Python string literal (`_DEFAULT_TEMPLATE` in
`home_control/config.py`) and must stay byte-identical to `config.example.toml`.

**Docs sync**: per-device implementation/config blurbs are mirrored in two
places — `README.md`'s `## Device support` and each system's `help_notes()`
(the in-app `?` help).

## Testing

- `pytest` (testpaths = `tests/` per `pyproject.toml`). No network calls in
  the suite.
- Systems that need device fixtures check `HOME_CONTROL_MOCK=1` (set via
  `monkeypatch.setenv`) and load canned data instead of polling real
  hardware — see `_mock_systems()` in `tests/test_voice.py` for the pattern.

## Design constraints

- Built for a 80-column and 45-to-60-row terminal. Design panels to use the
  space, don't minimize for 24 rows.
- No cloud/account requirement except for one-time authentications. For regular
  use and actual control, everything must be local-LAN.
