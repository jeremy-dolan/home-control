---
name: add-device
description: Add a new device integration to home-control, or repoint an existing panel at different hardware — library research, the Controller/Panel implementation, config, registration, mocks/tests, and docs.
---

# Adding support for a new device

Work in a worktree. Read alongside `ARCHITECTURE.md` — this recipe assumes
the `System` contract described there.

1. **Research the library first — don't just grab the first hit.** Find the
   best-fit Python library for *local-LAN* control of the target hardware; a
   cloud/account dependency required for regular use is disqualifying (see
   Design constraints in `ARCHITECTURE.md`). Weigh: active maintenance and
   recent releases, PyPI availability, a permissive license, type hints, and
   confirmed support for the user's specific model/firmware. Check PyPI and
   the project's open issues, and web-search for alternatives and known
   pitfalls before committing to one. Prefer stdlib + one focused library
   over a heavy framework. If a `standalone-apps/` prototype already drives
   this device, mine it for a known-good approach. Surface the shortlist and
   your pick (with the trade-off) to the user before building on it.

2. **Read two existing panels as templates.** `systems/hue.py` and
   `systems/sonos.py` are the reference implementations of the `System`
   contract and the app's UI conventions — match them rather than inventing.

3. **Implement the `System`** in `home_control/systems/<device>.py` as a
   Controller + Panel sharing a cached snapshot, following the System contract
   in `ARCHITECTURE.md`. Beyond what that section specifies:
   - *Controller*: use short network timeouts, so an unreachable device can't
     freeze the UI; gate command methods on being connected; and never print to
     stdout/stderr (the file-only logger exists for this reason — a stray print
     corrupts the curses screen).
   - *Panel*: the full surface is `collapsed_lines(width)`,
     `render_expanded(region)`, `handle_key(key)`, `help_notes()`,
     `toolbar_line()`, plus `captures_text()` if it has a text-entry mode.
   - Add `voice_actions()` / `voice_context()` if voice control fits — read the
     `claude-api` skill before writing the Anthropic call.

4. **Follow the UI conventions** the reference panels use: a colored status
   badge + summary when collapsed; the shared `ui.py` primitives for selection
   (cursor + bold, never reverse video), level bars, and color; and a distinct
   system accent color. Draw only through `ui.py`, never raw curses.

5. **Wire config**: add a `[<device>]` section read via `config.py`,
   auto-detecting when unset. Mind the template byte-identity rule under Config
   in `ARCHITECTURE.md`.

6. **Register it**: add `<Device>System` to `_DEFAULT_ORDER` in
   `home_control/systems/__init__.py`.

7. **Mock + test**: honor `HOME_CONTROL_MOCK=1` with canned data so the panel
   runs without hardware (see Testing in `ARCHITECTURE.md` for the pattern),
   and add tests under `tests/`.

8. **Document**: add a blurb to `README.md`'s `## Device support` and the
   panel's `help_notes()` — keep them in sync.

9. **Verify**: `pytest`, `ruff check .`, `pyright`; then drive the real UI with
   the `verify` skill to confirm it renders.
