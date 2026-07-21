# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A curses TUI that unifies control of home devices (Philips Hue, Roku, Sonos, a
Verizon Fios router, Yoto, Midea AC) plus a voice-control mode that sends
transcripts to the Claude API for NLU (tool-calling, not a rules grammar).

## Commands

```bash
source .venv/bin/activate      # venv already set up with project + dev deps
pip install -e ".[dev]"        # ruff/pyright/pytest live in the `dev` extra, not the base install
home-control                   # run the TUI (entry point: home_control.app:run)
pytest                          # run the test suite
pytest tests/test_midea.py     # run a single test file
pytest tests/test_midea.py -k some_test_name
ruff check .                    # lint (line-length 120, extend-select I, UP)
pyright                         # type check (basic mode)
```

## Architecture

Each device integration is a `System` (`home_control/systems/base.py`),
conceptually split into two halves that share state:

- **Controller** — discovery, polling, commands. No curses. `poll()` runs on
  a background thread per system (see `home_control/poller.py`), so it must be
  thread-safe: mutate a cached snapshot, never touch the screen from that
  thread.
- **Panel** — `collapsed_lines()` / `render_expanded()` draw cached state;
  `handle_key()` runs only while focused, on the main thread.

`home_control/app.py` (the `Shell`) never reaches into a device's internals —
only through these methods: layout (`layout.py`), box drawing and color
(`ui.py`), and per-system focus/toolbar/help wiring all go through the
`System` contract. TAB/Shift-TAB always changes focus at the shell level and
is never delegated to a panel; a focused panel's `handle_key` gets first crack
at other keys, and unconsumed keys fall through to shell globals (`?` help,
`q`/ESC quit, SPACE for voice). Exception: while the focused panel reports
`captures_text()` (a text-entry state, e.g. Roku's keyboard/search modes), the
shell suspends the SPACE binding so it reaches the panel as a typed character.

Voice control layers on top: each `System` exposes `voice_actions()` →
`VoiceAction`s (name/description/handler/JSON-schema params), which
`voice/router.py` turns into Claude tool schemas; a matched tool call
dispatches back to the system's handler off the main thread and returns a
short result string shown in the voice overlay. `voice/controller.py` drives
the push-to-talk state machine (listening → thinking → result);
`voice/recognizer.py` is the pluggable STT layer.

Logging: the root logger is redirected to a file
(`$HOME_CONTROL_LOG`, default `~/.cache/home-control/tui.log`) — never stderr,
since library `logger.exception()` calls (phue2/soco/midealocal on network
errors) would otherwise corrupt the curses screen.

## Directory map

- `home_control/` — the actual package (tracked): `app.py` (shell/main loop),
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
(overridable via `$HOME_CONTROL_CONFIG`), auto-created from a template on
first run. **Gotcha**: that template is a Python string literal
(`_DEFAULT_TEMPLATE` in `home_control/config.py`) that must be kept
byte-identical to `config.example.toml` in the repo — nothing currently tests
this, so if you edit one, edit the other.

**Docs sync**: the per-device implementation/config blurbs live in two places
— the `## Device support` section of `README.md` and each system's
`help_notes()` (the in-app `?` help). Keep them in sync: if you change how a
device is discovered/configured, update both. Nothing tests this either.

## Testing

- `pytest` (testpaths = `tests/` per `pyproject.toml`). No network calls in
  the suite.
- Systems that need device fixtures check `HOME_CONTROL_MOCK=1` (set via
  `monkeypatch.setenv`) and load canned data instead of polling real
  hardware — see `_mock_systems()` in `tests/test_voice.py` for the pattern.

## Design constraints

- Built for a ~50-row terminal — design panels to use the space, don't
  minimize for 40 rows.
- No cloud/account requirement — everything is local-LAN control.

## Agent workflow

Default to an isolated git worktree per task, not direct edits in a shared
checkout — this repo has already seen multiple agent sessions drop untracked
files (draft CLAUDE.md variants, swap files) into the same working directory,
which is exactly the collision worktrees prevent when sessions run
concurrently. Default flow: worktree → branch → commit → push → open a draft
PR for review before merging to `main`.

Escape hatch: small, single-session interactive edits (typo fixes, one-line
tweaks) can be committed directly to the current branch without the full
worktree+PR ceremony, if the user says so in the moment. When in doubt, use
the isolated workflow. Never force-push, merge, or push to `main` without the
user explicitly asking.

## Git conventions

- Repo-local `user.name`/`user.email` is set to `Jeremy Dolan
  <129558107+jeremy-dolan@users.noreply.github.com>` — GitHub's
  privacy-preserving noreply alias, not a real email. Don't override with a
  real address.
- `dev/`, `standalone-apps/`, `plugins/`, `README.ideas` are gitignored on
  purpose (see Directory map above) — don't `git add -f` them without
  checking with the user first.
- `.claude/` is partly tracked: `.claude/skills/` and `.claude/settings.json`
  are shared (they're project tooling everyone benefits from), everything
  else under it is ignored — `settings.local.json` is per-machine permission
  grants, `worktrees/` is agent scratch space. Keep skills checkout-agnostic
  (derive paths from git, don't hardcode `/home/<user>/...`) so they work
  from a worktree.

## Keeping this file current

If you change the `System` contract, directory layout, config handling, or
common commands, update the relevant section here in the same commit.
