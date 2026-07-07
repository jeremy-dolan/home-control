"""The shell: stacked-panel layout, focus handling, global keys, main loop."""

from __future__ import annotations

import curses
import logging
import os
import sys
import textwrap
from pathlib import Path

from . import layout
from .poller import Poller
from .systems import System, build_systems
from .ui import Line, Region, Seg, attr, draw_box, hint, hint_row, init_colors, seg_len
from .voice import VoiceController

# Cap drawn width so boxes don't sprawl on very wide terminals.
MAX_WIDTH = 100

# Push-to-talk key: spacebar, handled globally before any panel. Panels use
# ENTER for their main action instead, so nothing competes for space.
VOICE_KEY = ord(" ")

# Fixed geometry for the Voice box: sized to fit the input-mode prompt, then
# held constant across every mode so the box doesn't resize as the dialogue
# progresses from listening -> thinking -> result.
VOICE_CONTENT_W = 44
VOICE_BODY_LINES = 8

# Static sample phrases shown in the box so a new user sees the kind of
# natural phrasing that works, without deriving a live tool listing.
VOICE_EXAMPLES = [
    "Turn the music up 3",
    "Set the living room lights for reading",
    "Turn off all the ACs and lights",
]
GLOBAL_TOOLBAR = "TAB change system    SPACE voice command    ? help    q quit"

LOG_PATH = Path(
    os.environ.get("HOME_CONTROL_LOG", Path.home() / ".cache" / "home-control" / "tui.log")
)


def _setup_logging() -> None:
    """Send all logging to a file, never stderr.

    Libraries like phue2/soco call ``logger.exception()`` on network errors,
    which would otherwise dump tracebacks straight onto the curses screen and
    corrupt it. Routing the root logger to a file keeps the TUI clean and still
    captures errors for debugging.
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    logging.basicConfig(
        filename=str(LOG_PATH), filemode="a", level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    for name in ("phue_modern", "httpx", "httpcore", "soco", "midealocal"):
        logging.getLogger(name).setLevel(logging.ERROR)


def _wrap_message(text: str, width: int, max_lines: int) -> list[str]:
    """Wrap free-form text (e.g. an LLM reply) into display lines.

    Splits on existing newlines first (so paragraph/bullet breaks from the
    model's reply are preserved) then word-wraps each paragraph to ``width``.
    Truncates to ``max_lines`` with a trailing "..." so a long reply can never
    push the box past the screen or smear text across other rows.
    """
    lines: list[str] = []
    for para in text.split("\n"):
        lines.extend(textwrap.wrap(para, width=width) if para.strip() else [""])
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + ["..."]
    return lines or ["Done."]


class Shell:
    def __init__(self, systems: list[System]):
        self.systems = systems
        # Start focused on Lighting (Hue) if present, else the first system.
        self.focused = next((i for i, s in enumerate(systems) if s.name == "Lighting"), 0)
        self.poller = Poller(systems)
        self.show_help = False
        self.voice = VoiceController(systems)

    # -- focus ----------------------------------------------------------------
    def cycle_focus(self, delta: int) -> None:
        self.focused = (self.focused + delta) % len(self.systems)
        self.poller.set_focus(self.focused)

    # -- drawing --------------------------------------------------------------
    def render(self, stdscr: curses.window) -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        width = min(w, MAX_WIDTH)

        collapsed_heights = [s.collapsed_height for s in self.systems]
        slots = layout.compute_layout(collapsed_heights, self.focused, h)

        for slot in slots:
            system = self.systems[slot.index]
            region = draw_box(stdscr, slot.top, 0, slot.height, width,
                              system.name, system.color, focused=slot.focused)
            if region.height <= 0:
                continue
            if slot.focused:
                self._render_focused(stdscr, system, region)
            else:
                for i, line in enumerate(system.collapsed_lines(region.width)):
                    region.segs(i, line)

        self._render_status_line(stdscr, h, w)
        self._render_global_toolbar(stdscr, h, w)
        if self.show_help:
            self._render_help(stdscr, h, w)
        if self.voice.active():
            self._render_voice(stdscr, h, w)
        stdscr.refresh()

    def _render_status_line(self, stdscr: curses.window, h: int, w: int) -> None:
        msg = self.systems[self.focused].status()
        if not msg:
            return
        text = f" {self.systems[self.focused].name}: {msg} "
        col = max(0, (w - len(text)) // 2)
        try:
            stdscr.addstr(h - 2, col, text[: w - 1], attr(self.systems[self.focused].color))
        except curses.error:
            pass

    def _render_help(self, stdscr: curses.window, h: int, w: int) -> None:
        system = self.systems[self.focused]
        lines = [
            ("TAB / Shift-TAB", "switch system"),
            ("SPACE", "voice command"),
            ("?", "toggle this help"),
            ("q / ESC", "quit"),
            ("", ""),
            (f"— {system.name} —", ""),
            (system.toolbar(), ""),
            *[(note, "") for note in system.help_notes()],
        ]
        bw = min(max(len(GLOBAL_TOOLBAR) + 4, *(len(a) + len(b) + 6 for a, b in lines)), w - 4)
        bh = len(lines) + 4
        top = max(0, (h - bh) // 2)
        left = max(0, (w - bw) // 2)
        region = draw_box(stdscr, top, left, bh, bw, "Help", "white", focused=True)
        for i, (a, b) in enumerate(lines):
            if not a and not b:
                continue
            if b:
                region.text(i, 0, f"{a:<16}", "white", bold=True)
                region.text(i, 17, b)
            else:
                region.text(i, 0, a, system.color if a.startswith("—") else "")

    def _render_voice(self, stdscr: curses.window, h: int, w: int) -> None:
        mode, buffer, message, _is_error = self.voice.snapshot()
        color = "cyan"
        mid = VOICE_BODY_LINES // 2
        # Static sample phrases, shown while the dialogue is still waiting for
        # a command so the user sees what's possible without asking the model.
        hints = ["You can say things like:"] + [f'  "{ex}"' for ex in VOICE_EXAMPLES]
        if mode == "input":
            body = ["Listening...", "", *hints, "", "> " + buffer + "_"]
            dim_rows: set[int] = {0} | set(range(2, 2 + len(hints)))
            hint_line: Line = hint_row(hint("ENTER", "send", color), hint("ESC", "cancel", color), sep="    ")
        elif mode == "listening":
            body = ["Listening...", "", *hints]
            dim_rows = {0} | set(range(2, 2 + len(hints)))
            hint_line = []
        elif mode == "thinking":
            body = [""] * VOICE_BODY_LINES
            body[mid] = "Thinking..."
            dim_rows = {mid}
            hint_line = []
        else:  # result
            body = _wrap_message(message or "Done.", VOICE_CONTENT_W, VOICE_BODY_LINES)
            dim_rows = set()
            hint_line = [Seg("press any key", dim=True)]
        body = (body + [""] * VOICE_BODY_LINES)[:VOICE_BODY_LINES]

        content_w = max(len("Voice") + 4, VOICE_CONTENT_W, seg_len(hint_line))
        bw = min(content_w + 4, w - 4)
        bh = VOICE_BODY_LINES + 4  # body + blank separator + hint row + top/bottom border
        top = max(0, (h - bh) // 2)
        left = max(0, (w - bw) // 2)
        region = draw_box(stdscr, top, left, bh, bw, "Voice", color, focused=True)
        for i, line in enumerate(body):
            region.text(i, 0, line[: region.width], dim=(i in dim_rows))
        if hint_line:
            col = max(0, (region.width - seg_len(hint_line)) // 2)
            region.segs(VOICE_BODY_LINES + 1, hint_line, col)

    def _render_focused(self, stdscr: curses.window, system: System, region: Region) -> None:
        toolbar = system.toolbar()
        toolbar_line = system.toolbar_line()
        has_toolbar = bool(toolbar_line) if toolbar_line is not None else bool(toolbar)
        body_h = region.height - 1 if has_toolbar else region.height
        body = Region(stdscr, region.top, region.left, max(0, body_h), region.width)
        system.render_expanded(body)
        if has_toolbar and region.height >= 2:
            if toolbar_line is not None:
                col = max(0, (region.width - seg_len(toolbar_line)) // 2)
                region.segs(region.height - 1, toolbar_line, col)
            else:
                col = max(0, (region.width - len(toolbar)) // 2)
                region.text(region.height - 1, col, toolbar, system.color)

    def _render_global_toolbar(self, stdscr: curses.window, h: int, w: int) -> None:
        row = layout.toolbar_row(h)
        col = max(0, (w - len(GLOBAL_TOOLBAR)) // 2)
        try:
            stdscr.addstr(row, col, GLOBAL_TOOLBAR[: w - 1], attr(bold=True))
        except curses.error:
            pass

    # -- input ----------------------------------------------------------------
    def handle_key(self, key: int) -> bool:
        """Process one key. Return True to quit."""
        if self.voice.active():  # voice overlay captures all keys while open
            self.voice.feed_key(key)
            return False

        if self.show_help:  # any key dismisses help; nothing else happens
            self.show_help = False
            return False

        if key == VOICE_KEY:  # push-to-talk
            self.voice.begin()
            return False
        if key == 9:  # TAB — always shell focus, never delegated
            self.cycle_focus(1)
            return False
        if key == curses.KEY_BTAB:  # Shift-TAB
            self.cycle_focus(-1)
            return False

        # Focused system gets first crack; unconsumed keys fall to globals.
        if self.systems[self.focused].handle_key(key):
            return False

        if key == ord("?"):
            self.show_help = True
            return False
        if key in (ord("q"), ord("Q"), 27):  # q / ESC
            return True
        return False


def main_loop(stdscr: curses.window, shell: Shell) -> None:
    init_colors()
    curses.curs_set(0)
    curses.set_escdelay(25)
    stdscr.keypad(True)
    stdscr.timeout(250)  # repaint cadence for live (threaded) state updates

    shell.poller.set_focus(shell.focused)
    shell.poller.start()
    for system in shell.systems:
        system.start()

    try:
        while True:
            shell.render(stdscr)
            key = stdscr.getch()
            if key == -1 or key == curses.KEY_RESIZE:
                continue
            if shell.handle_key(key):
                break
    finally:
        shell.poller.stop()
        for system in shell.systems:
            system.stop()


def run() -> None:
    _setup_logging()
    shell = Shell(build_systems())
    # Redirect stderr to the log file for the curses session so stray tracebacks
    # or library prints can't corrupt the screen. Restored before any crash
    # traceback is shown (curses.wrapper has already restored the terminal).
    old_stderr = sys.stderr
    try:
        with open(LOG_PATH, "a") as log:
            sys.stderr = log
            try:
                curses.wrapper(main_loop, shell)
            finally:
                sys.stderr = old_stderr
    except OSError:
        curses.wrapper(main_loop, shell)


if __name__ == "__main__":
    run()
