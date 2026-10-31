"""The shell: stacked-panel layout, focus handling, global keys, main loop."""

from __future__ import annotations

import curses
import io
import logging
import os
import sys
import textwrap
import threading
from pathlib import Path
from typing import TextIO

from . import config, layout
from .poller import Poller
from .systems import System, build_systems
from .ui import Line, Region, Seg, attr, backdrop, draw_box, hint, hint_row, init_colors, seg_len
from .voice import VoiceController

# Cap drawn width so boxes don't sprawl on very wide terminals.
MAX_WIDTH = 100

# Push-to-talk key: spacebar, handled globally ahead of panel hotkeys — except
# while the focused panel reports captures_text() (text-entry state), when
# SPACE falls through to it as a typed character.
VOICE_KEY = ord(" ")

# Fixed geometry for the Voice box, held constant across every mode so the box
# doesn't resize as the dialogue progresses from listening -> thinking ->
# result. The width is the longest example plus its two-space indent and
# quotes; body lines are clipped to it in `_render_voice`, so a longer phrase
# than the width allows loses its tail silently.
VOICE_CONTENT_W = 44
VOICE_BODY_LINES = 8

# Static sample phrases shown in the box so a new user sees the kind of
# natural phrasing that works, without deriving a live tool listing.
VOICE_EXAMPLES = [
    "Turn down the lights and start the movie",
    "It's too warm in the bedroom",
    "Move the music to the kitchen",
]
GLOBAL_TOOLBAR = "TAB change system    SPACE voice command    ? help    q quit"

# Help popup: one fixed width for every system (clamped to leave 5 columns of
# margin each side on narrow terminals) so the boxes read consistently.
HELP_WIDTH = 70

LOG_PATH = Path(
    os.environ.get("HOME_CONTROL_LOG", Path.home() / ".cache" / "home-control" / "tui.log")
)


class _LazyLog(io.TextIOBase):
    """Append-only text stream that creates its file (0600) on first write.

    A run with nothing to report leaves no file behind. If the file can't be
    opened, writes are dropped: this stream stands in for stderr, so there is
    nowhere safe to report the failure.
    """

    def __init__(self, path: Path):
        self._path = path
        self._file: TextIO | None = None
        self._failed = False
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def _open(self) -> TextIO | None:
        with self._lock:
            if self._file is None and not self._failed:
                try:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                    fd = os.open(self._path, flags, config.PRIVATE_FILE_MODE)
                    self._file = os.fdopen(fd, "a", encoding="utf-8", errors="backslashreplace")
                except OSError:
                    self._failed = True
            return self._file

    def write(self, s: str) -> int:
        f = self._file or self._open()
        if f is not None:
            try:
                f.write(s)
                f.flush()
            except OSError:
                pass
        return len(s)

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None
        super().close()


def _setup_logging() -> _LazyLog:
    """Send all logging to a file, never stderr; return that file's stream.

    Libraries like soco/midealocal call ``logger.exception()`` on network errors,
    which would otherwise dump tracebacks straight onto the curses screen and
    corrupt it. Routing the root logger to a file keeps the TUI clean and still
    captures errors for debugging.
    """
    config.check_private_file(LOG_PATH)
    log = _LazyLog(LOG_PATH)
    logging.basicConfig(
        stream=log, level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    for name in ("phue_modern", "httpx", "httpcore", "soco", "midealocal"):
        logging.getLogger(name).setLevel(logging.ERROR)
    return log


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
        # Scroll state for a popup taller than the screen. pending_popup()
        # builds a fresh Popup every frame, so the offset belongs to the
        # (system, title) it was scrolled on and restarts when that changes.
        self._popup_ident: tuple[System, str] | None = None
        self._popup_scroll = 0
        self._popup_page = 1  # body rows the last render had room for

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

        popup_sys = self._popup_system()
        if popup_sys is None:
            self._popup_ident = None
        overlaid = self.show_help or popup_sys is not None or self.voice.active()

        focused_slot = slots[0]
        with backdrop(overlaid):
            for slot in slots:
                system = self.systems[slot.index]
                region = draw_box(stdscr, slot.top, 0, slot.height, width,
                                  system.name, system.color, focused=slot.focused)
                if slot.focused:
                    focused_slot = slot
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
            self._render_help(stdscr, h, w, focused_slot)
        if popup_sys is not None:
            self._render_popup(stdscr, h, w, popup_sys)
        if self.voice.active():
            self._render_voice(stdscr, h, w)
        stdscr.refresh()

    def _popup_system(self) -> System | None:
        """First system with a pending modal alert (checked for every system,
        not just the focused one), or None."""
        for system in self.systems:
            if system.pending_popup() is not None:
                return system
        return None

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

    def _render_help(self, stdscr: curses.window, h: int, w: int, focused_slot: layout.Slot) -> None:
        # Fixed width (clamped for narrow terminals) so every system's help
        # reads the same; notes are paragraphs, word-wrapped here.
        system = self.systems[self.focused]
        bw = min(HELP_WIDTH, w - 10)
        inner = bw - 4
        rows: list[str] = []
        for note in system.help_notes():
            if rows:
                rows.append("")
            rows.extend(textwrap.wrap(note, inner))
        if not rows:
            rows = ["This panel has no controls of its own."]
        rows.insert(0, "")  # blank line buffer below the title border
        footer = "press any key to close"
        bh = len(rows) + 4  # borders + blank separator + footer
        # Center on the focused panel's box, not the whole screen, but clamp
        # so a tall help box never runs off-screen for a short focused panel.
        panel_mid = focused_slot.top + focused_slot.height // 2
        top = max(0, min(h - bh, panel_mid - bh // 2))
        left = max(0, (w - bw) // 2)
        region = draw_box(stdscr, top, left, bh, bw, f"{system.name} help",
                          system.color, focused=True)
        for i, text in enumerate(rows):
            region.text(i, 0, text)
        region.text(len(rows) + 1, max(0, (inner - len(footer)) // 2), footer, dim=True)

    def _render_popup(self, stdscr: curses.window, h: int, w: int, system: System) -> None:
        popup = system.pending_popup()
        if popup is None:
            return
        bw = min(HELP_WIDTH, w - 10)
        inner = bw - 4
        body = popup.lines
        # Borders, a blank line either side of the body, and the footer: five
        # rows around it. The box stops growing a row short of each screen
        # edge, and past that the body scrolls.
        bh = min(len(body) + 5, max(6, h - 2))
        page = bh - 5
        ident = (system, popup.title)
        if ident != self._popup_ident:
            self._popup_ident, self._popup_scroll = ident, 0
        # Clamped every frame, not just on a keypress: a popup can change
        # length while open (Midea's re-reads live attributes).
        self._popup_scroll = max(0, min(self._popup_scroll, len(body) - page))
        self._popup_page = page
        first = self._popup_scroll
        shown = body[first:first + page]
        if len(shown) < len(body):
            footer = f"↑↓ scroll ({first + 1}-{first + len(shown)} of {len(body)})   ENTER to close"
        else:
            footer = "press ENTER to close"
        top = max(0, (h - bh) // 2)
        left = max(0, (w - bw) // 2)
        color = popup.color or system.color
        region = draw_box(stdscr, top, left, bh, bw, popup.title, color, focused=True)
        for i, text in enumerate(shown):
            region.text(i + 1, 0, text[:inner])
        region.text(page + 2, max(0, (inner - len(footer)) // 2), footer, dim=True)

    def _scroll_popup(self, system: System, delta: int) -> None:
        popup = system.pending_popup()
        total = len(popup.lines) if popup is not None else 0
        self._popup_scroll = max(0, min(self._popup_scroll + delta, total - self._popup_page))

    def _render_voice(self, stdscr: curses.window, h: int, w: int) -> None:
        mode, buffer, message, _is_error = self.voice.snapshot()
        color = "info_teal"
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
        toolbar_line = system.toolbar_line()
        body_h = region.height - 1 if toolbar_line else region.height
        body = Region(stdscr, region.top, region.left, max(0, body_h), region.width)
        system.render_expanded(body)
        if toolbar_line and region.height >= 2:
            col = max(0, (region.width - seg_len(toolbar_line)) // 2)
            region.segs(region.height - 1, toolbar_line, col)

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

        popup_sys = self._popup_system()  # modal alert: ENTER dismisses, arrows scroll, other keys ignored
        if popup_sys is not None:
            if key in (ord("\n"), curses.KEY_ENTER):
                popup_sys.dismiss_popup()
                self._popup_ident = None
            elif key == curses.KEY_UP:
                self._scroll_popup(popup_sys, -1)
            elif key == curses.KEY_DOWN:
                self._scroll_popup(popup_sys, 1)
            elif key == curses.KEY_PPAGE:
                self._scroll_popup(popup_sys, -self._popup_page)
            elif key == curses.KEY_NPAGE:
                self._scroll_popup(popup_sys, self._popup_page)
            return False

        if self.show_help:  # any key dismisses help; nothing else happens
            self.show_help = False
            return False

        # Push-to-talk — unless the focused panel is taking text input (e.g. the
        # Roku keyboard/search modes), where SPACE must type a literal space.
        if key == VOICE_KEY and not self.systems[self.focused].captures_text():
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
    log = _setup_logging()
    shell = Shell(build_systems())
    # Redirect stderr to the log file for the curses session so stray tracebacks
    # or library prints can't corrupt the screen. Restored before any crash
    # traceback is shown (curses.wrapper has already restored the terminal).
    old_stderr = sys.stderr
    sys.stderr = log
    try:
        curses.wrapper(main_loop, shell)
    finally:
        sys.stderr = old_stderr


if __name__ == "__main__":
    run()
