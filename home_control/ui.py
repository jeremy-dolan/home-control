"""Shared curses UI primitives: colors, styled segments, box drawing, regions.

This module is the only place that knows how to talk to curses for drawing.
Layout *math* lives in layout.py (pure, testable); device logic lives in
systems/. Keeping curses confined here keeps the rest of the app headless-testable.
"""

from __future__ import annotations

import colorsys
import curses
import textwrap
from dataclasses import dataclass


def wrap(text: str, width: int, max_lines: int | None = None) -> list[str]:
    """Word-wrap `text` to `width` columns, returning a list of lines.

    The single wrapping primitive shared by `Region.text_wrapped()` (which draws
    the lines) and callers that build styled `Seg`/`Line` rows themselves. Long
    unbroken tokens (URLs, `HTTPConnectionPool(...)` blobs) are hard-broken so a
    message is never silently truncated off the right edge. Never returns empty:
    a blank/whitespace `text` yields `[text]`. Caps to `max_lines` when given.
    """
    lines = textwrap.wrap(text, width=max(1, width),
                          break_long_words=True, break_on_hyphens=False) or [text]
    return lines[:max_lines] if max_lines is not None else lines


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

# The app targets a 256-color terminal. Every palette entry is authored as RGB
# hex and resolved at init to an exact color (init_color, where the terminal
# allows redefining slots) or the nearest xterm-256 cube color. Below 256 colors
# the palette is left unallocated and every name renders in the terminal's
# default foreground: there is deliberately no hand-tuned 8-color variant, since
# layout, bold/dim weight, cursors and badge glyphs already carry the UI without
# hue. See "UI conventions" in ARCHITECTURE.md.
PALETTE = {
    # -- Semantic roles: what a color *means*, in any panel. ----------------
    "warn":  "#E3B341",  # working, but wants attention (filter due, error code)
    "fault": "#F85149",  # unreachable, offline, failed
    "muted": "#8A8A8A",  # a value that is itself off/absent/inactive
    "info":  "#39C5CF",  # neutral secondary series (voice chrome, upload chart)

    # -- System accents: which panel this is. -------------------------------
    # Each is the panel's *base* shade; lighten() derives the brighter one used
    # for hotkeys and selected rows, so a base near the top of its hue's range
    # leaves no room for that and makes the two read as one colour.
    "router_green": "#19A450",  # deep emerald — lighten()s to #58E690
    "hue_blue":     "#33AAFF",  # bright daylight blue
    "roku_purple":  "#A855F7",  # bright violet — reads well on a black terminal
    "sonos_yellow": "#FFE24D",  # bright warm yellow
    "yoto_orange":  "#F2820B",  # true orange — redder bases read burnt, paler ones lighten() to peach
    "midea_teal":   "#14B8A6",  # cool teal (kept clear of the bright blue)
}

# Per-system accent colors (borders, cursors, hotkeys, section headers, bars;
# body text stays the terminal default).
SYSTEM_COLORS = {
    "router": "router_green",
    "hue": "hue_blue",
    "roku": "roku_purple",
    "sonos": "sonos_yellow",
    "yoto": "yoto_orange",
    "midea": "midea_teal",
}

_PAIRS: dict[str, int] = {}
_next_pair = 1  # next free curses pair slot; set at the end of init_colors()
_dynamic_names: set[str] = set()  # lazily-allocated RGB pairs, cleared on re-init

# xterm-256 color cube levels, for nearest-color fallback.
_CUBE = (0, 95, 135, 175, 215, 255)


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _nearest_256(r: int, g: int, b: int) -> int:
    """Map an RGB triple to the closest xterm-256 color-cube index."""
    def lvl(v: int) -> int:
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))
    return 16 + 36 * lvl(r) + 6 * lvl(g) + lvl(b)


def init_colors() -> None:
    """Allocate one curses pair per palette entry. Call once after curses init.

    On a terminal with fewer than 256 colors nothing is allocated, so every name
    falls through to the default foreground in `attr()`.
    """
    global _next_pair
    curses.start_color()
    curses.use_default_colors()
    _PAIRS.clear()
    _PAIRS[""] = 0  # default terminal color
    _dynamic_names.clear()
    _next_pair = 1

    n_colors = getattr(curses, "COLORS", 0)
    if n_colors < 256:
        return
    try:
        can_change = curses.can_change_color()
    except curses.error:
        can_change = False

    pair = 1
    custom_slot = 16  # leave the 16 base ANSI slots untouched
    for name, hexv in PALETTE.items():
        r, g, b = _hex_rgb(hexv)
        fg = _nearest_256(r, g, b)
        if can_change and 16 <= custom_slot < n_colors:
            try:
                curses.init_color(custom_slot, r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)
                fg = custom_slot
                custom_slot += 1
            except curses.error:
                pass
        try:
            curses.init_pair(pair, fg, -1)
        except curses.error:
            continue  # out of pair slots: this name renders in the default color
        _PAIRS[name] = pair
        pair += 1

    # Dynamic RGB pairs (rgb_color) re-register lazily above the fixed palette.
    _next_pair = pair


def rgb_color(r: int, g: int, b: int) -> str:
    """Return a Seg/attr color name that renders approximately ``(r, g, b)``.

    The triple is mapped to the nearest xterm-256 cube color and a curses pair is
    allocated for it on first use (cached, so repeated colors share one pair).
    Falls back to the default foreground when the terminal lacks 256 colors or
    pair slots run out. Must be called after init_colors() (i.e. during
    rendering)."""
    global _next_pair
    if getattr(curses, "COLORS", 0) < 256:
        return ""
    idx = _nearest_256(r, g, b)
    name = f"x256:{idx}"
    if name in _PAIRS:
        return name
    if _next_pair >= getattr(curses, "COLOR_PAIRS", 256):
        return ""
    try:
        curses.init_pair(_next_pair, idx, -1)
    except curses.error:
        return ""
    _PAIRS[name] = _next_pair
    _dynamic_names.add(name)
    _next_pair += 1
    return name


def lighten(color: str, t: float = 0.4) -> str:
    """Return a Seg/attr color name for ``color`` raised ``t`` of the way toward
    full lightness — a brighter shade of the same hue, allocated as its own pair.
    Used to make hotkeys and a selected row's accent (e.g. the brightness bar)
    pop, since A_BOLD does not brighten a 256-color pair the way it does the base
    ANSI colors. Falls back to the original name for anything outside the palette.

    The lift happens in HSL, holding hue and saturation. Blending toward white
    instead would desaturate: for an already-saturated accent that yields a paler
    colour rather than a brighter one, leaving the two shades hard to tell apart.
    """
    if color not in PALETTE:
        return color
    r, g, b = (c / 255 for c in _hex_rgb(PALETTE[color]))
    h, lum, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, lum + (1 - lum) * t, s)
    return rgb_color(round(r * 255), round(g * 255), round(b * 255))


# Status-badge states. Every panel leads its collapsed line (and its expanded
# header) with a `● ONLINE` / `▶ PLAYING` / `● COOL` badge; these are the three
# states such a badge can be in, so the same situation reads the same color in
# every panel.
BADGE_ACTIVE = "active"  # doing its job: online, playing, conditioning
BADGE_IDLE = "idle"      # reachable but not doing anything: off, stopped, paused
BADGE_FAULT = "fault"    # unreachable or failed


def badge_color(state: str, accent: str) -> str:
    """Color for a status badge in `state` on a panel whose accent is `accent`.

    Active badges carry the system accent (the panel's identity is loudest when
    the device is doing something), idle ones go `muted`, faults go `fault`.
    """
    return {BADGE_ACTIVE: accent, BADGE_IDLE: "muted"}.get(state, "fault")


def attr(color: str = "", *, bold: bool = False, dim: bool = False, reverse: bool = False) -> int:
    """Build a curses attribute from a color name + flags."""
    a = curses.color_pair(_PAIRS.get(color, 0))
    if bold:
        a |= curses.A_BOLD
    if dim:
        a |= curses.A_DIM
    if reverse:
        a |= curses.A_REVERSE
    return a


# ---------------------------------------------------------------------------
# Styled text segments
# ---------------------------------------------------------------------------


@dataclass
class Seg:
    """A run of text with a style. A "line" is a list[Seg]."""

    text: str
    color: str = ""
    bold: bool = False
    dim: bool = False
    reverse: bool = False
    # False pins this run to its own colour when the row is selected, exempting
    # it from highlight()'s accent lift. The ▶ cursor uses it: it is already the
    # thing marking the row, so brightening it too says nothing extra.
    lift: bool = True


Line = list[Seg]


def level_bar(value: float, maximum: float, color: str = "", width: int = 20, *,
              empty: bool = False) -> Line:
    """A ━━━━◉──── level bar (no brackets): the filled run with a ◉ knob at its
    head in `color`, the remaining track dim.

    `empty` draws just the dim track, for a control that is off — the row's own
    ON/OFF badge carries that state, so the bar doesn't need to repeat it. The
    knob is ◉ rather than ● so it stays distinct from the ● status dot that
    leads almost every row.
    """
    if empty:
        return [Seg("─" * width, dim=True)]
    f = max(1, min(width, round(value / maximum * width)))
    return [Seg("━" * (f - 1) + "◉", color), Seg("─" * (width - f), dim=True)]


def pad_between(left: str, right: str, width: int) -> str:
    """Left + right justified within width (right pushed to the far edge)."""
    gap = width - len(left) - len(right)
    if gap < 1:
        gap = 1
    return left + " " * gap + right


def seg_len(line: Line) -> int:
    return sum(len(s.text) for s in line)


def justify(left: Line, right: Line, width: int, *, reverse: bool = False) -> Line:
    """Combine left + right styled runs with a space pad so right hugs the edge.

    If `reverse`, every segment (including the pad) is drawn reversed — used to
    highlight a selected row across its full width.
    """
    gap = max(1, width - seg_len(left) - seg_len(right))
    out: Line = [*left, Seg(" " * gap), *right]
    if reverse:
        for s in out:
            s.reverse = True
    return out


def hint(key: str, label: str, color: str, *, paren: bool = False, key_color: str | None = None) -> Line:
    """One toolbar key hint: the hot key brightened, the label in the plain
    accent color (not dimmed/greyed — just not as bright as the key).

    ``paren=True`` parenthesizes the key inside the whole label, at the first
    case-insensitive match — the letter need not lead:
    ``hint("s", "scenes", color, paren=True)`` -> "(s)cenes",
    ``hint("u", "queue", color, paren=True)`` -> "q(u)eue". The key's own case
    wins over the label's, so ``hint("U", "queue", ...)`` -> "q(U)eue". A key
    that doesn't occur in the label is parenthesized in front: "(F5) refresh".

    Without `paren` the key stands alone before a space-separated label, e.g.
    ``hint("↕", "nav", color)`` -> "↕ nav".

    ``key_color`` overrides the auto-lightened key color — e.g. to match the
    key to a section header's exact accent instead of a paler tint of it.
    """
    bright = key_color if key_color is not None else lighten(color)
    if paren:
        i = label.lower().find(key.lower())
        if i < 0:
            return [Seg("(", color), Seg(key, bright, bold=True), Seg(f") {label}", color)]
        head, tail = label[:i], label[i + len(key):]
        out: Line = [Seg(head, color)] if head else []
        return [*out, Seg("(", color), Seg(key, bright, bold=True), Seg(f"){tail}", color)]
    return [Seg(key, bright, bold=True), Seg(f" {label}", color)]


def hint_row(*hints: Line, sep: str = "   ") -> Line:
    """Join toolbar hints (from `hint`) with a plain separator into one Line."""
    out: Line = []
    for i, h in enumerate(hints):
        if i:
            out.append(Seg(sep))
        out.extend(h)
    return out


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

# Rounded box-drawing characters matching the design mockups.
_TL, _TR, _BL, _BR, _H, _V = "╭", "╮", "╰", "╯", "─", "│"


class Region:
    """A bounded drawing surface in absolute screen coordinates.

    All writes are clipped to the region and swallow curses errors at the
    screen edge, so callers never have to bounds-check.
    """

    def __init__(self, stdscr: curses.window, top: int, left: int, height: int, width: int):
        self.stdscr = stdscr
        self.top = top
        self.left = left
        self.height = height
        self.width = width

    def text(self, row: int, col: int, s: str, color: str = "", *, bold: bool = False,
             dim: bool = False, reverse: bool = False) -> int:
        """Write a string at (row, col) within the region. Returns next free col."""
        if not (0 <= row < self.height) or col >= self.width:
            return col
        if col < 0:
            s = s[-col:]
            col = 0
        avail = self.width - col
        if avail <= 0:
            return col
        s = s[:avail]
        try:
            self.stdscr.addstr(self.top + row, self.left + col, s,
                               attr(color, bold=bold, dim=dim, reverse=reverse))
        except curses.error:
            pass
        return col + len(s)

    def text_wrapped(self, row: int, col: int, s: str, color: str = "", *, bold: bool = False,
                     dim: bool = False, max_rows: int | None = None) -> int:
        """Word-wrap `s` to the region width and draw it starting at (row, col).

        A drawing wrapper around `wrap()`: long unbroken tokens (URLs,
        `HTTPConnectionPool(...)` blobs) are hard-broken so nothing is silently
        truncated off the right edge — the whole message is readable, which
        single-line `text()` can't guarantee. Stops at the region bottom (or
        after `max_rows` lines). Returns the next free row.
        """
        avail = self.width - col
        if avail <= 0 or row >= self.height:
            return row
        room = self.height - row
        limit = room if max_rows is None else min(room, max_rows)
        for line in wrap(s, avail, limit):
            self.text(row, col, line, color, bold=bold, dim=dim)
            row += 1
        return row

    def segs(self, row: int, line: Line, col: int = 0) -> None:
        """Write a list of styled segments on a single row."""
        for seg in line:
            col = self.text(row, col, seg.text, seg.color,
                            bold=seg.bold, dim=seg.dim, reverse=seg.reverse)
            if col >= self.width:
                break

    def fill_row(self, row: int, ch: str = " ", color: str = "") -> None:
        self.text(row, 0, ch * self.width, color)


def cursor(accent: str, sel: bool) -> Seg:
    """The leading two columns of a selectable row: an accent ▶ when selected, two
    blanks otherwise. The app never uses reverse video to mark a selection — the
    cursor plus bolding the row does that job (see "UI conventions" in
    ARCHITECTURE.md), so every list builds its rows starting with this Seg."""
    return Seg("▶ ", accent, bold=True, lift=False) if sel else Seg("  ")


def highlight(line: Line, accent: str) -> Line:
    """Mark a whole row as selected: bold every segment, clear dim so the bold
    reads, and lift every segment already carrying ``accent`` to ``lighten(accent)``.

    That last step is what keeps a selected row coherent. A_BOLD can't brighten a
    256-color pair, so a row whose bar was explicitly lightened but whose accent
    text was only bolded ends up half-highlighted — the slider moves, the ``● ON``
    beside it doesn't. Callers therefore build rows with the *base* accent
    throughout and let this do the lifting. Segments marked ``lift=False`` — the
    ▶ cursor — keep their own colour. Mutates and returns the same list.
    """
    bright = lighten(accent)
    for s in line:
        s.bold = True
        s.dim = False
        if s.lift and s.color == accent:
            s.color = bright
    return line


def select_row(region: Region, row: int, text: str, *, sel: bool, accent: str,
               col: int = 0) -> None:
    """Draw a plain-text selectable list row: `cursor()` + the text, bolded when
    selected. ``text`` must not include its own marker — this owns the leading two
    columns. Lists that need styled runs build them from `cursor()` directly."""
    c = cursor(accent, sel)
    region.text(row, col, c.text, c.color, bold=c.bold)
    region.text(row, col + 2, text, bold=sel)


def draw_box(stdscr: curses.window, top: int, left: int, height: int, width: int,
             title: str, color: str, *, focused: bool = False) -> Region:
    """Draw a rounded border with an embedded title; return the interior Region.

    The border (and title) use the system's accent color, lightened and bolded
    while focused — bold alone can't brighten a 256-color pair, and on box-drawing
    glyphs its extra weight barely reads. The interior is cleared. Interior region
    is inset by 1 on all sides, with one extra column of padding on left/right for
    breathing room.
    """
    border = attr(lighten(color) if focused else color, bold=focused)

    # Top border with title:  ╭─── Title ──────────────╮
    label = f"{_TL}{_H * 3} {title} "
    top_line = label + _H * max(0, width - len(label) - 1) + _TR
    bot_line = _BL + _H * max(0, width - 2) + _BR

    def put(y: int, x: int, s: str, a: int) -> None:
        try:
            stdscr.addstr(y, x, s, a)
        except curses.error:
            pass

    put(top, left, top_line[:width], border)
    put(top + height - 1, left, bot_line[:width], border)
    for r in range(1, height - 1):
        put(top + r, left, _V, border)
        put(top + r, left + width - 1, _V, border)
        put(top + r, left + 1, " " * (width - 2), 0)

    # Interior region: inset borders + 1 col padding each side.
    return Region(stdscr, top + 1, left + 2, max(0, height - 2), max(0, width - 4))
