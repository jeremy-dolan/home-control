"""Shared curses UI primitives: colors, styled segments, box drawing, regions.

This module is the only place that knows how to talk to curses for drawing.
Layout *math* lives in layout.py (pure, testable); device logic lives in
systems/. Keeping curses confined here keeps the rest of the app headless-testable.
"""

from __future__ import annotations

import curses
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

# Named foreground colors on the terminal's default background. Each becomes a
# curses color pair after init_colors() runs. "grey" is white + A_DIM since the
# 8-color ANSI palette has no true grey. These stay as base ANSI colors and are
# used for status text (green=playing/on, red=off, yellow=paused, etc.).
_COLOR_FG = {
    "white": curses.COLOR_WHITE,
    "green": curses.COLOR_GREEN,
    "blue": curses.COLOR_BLUE,
    "magenta": curses.COLOR_MAGENTA,
    "cyan": curses.COLOR_CYAN,
    "yellow": curses.COLOR_YELLOW,
    "red": curses.COLOR_RED,
}

# Custom accent colors given as exact RGB hex. Resolved at init to the best the
# terminal supports: exact (init_color) → nearest xterm-256 → ANSI fallback.
_HEX_COLORS = {
    "hue_blue":     "#33AAFF",  # bright daylight blue
    "roku_purple":  "#A855F7",  # bright violet — reads well on a black terminal
    "sonos_yellow": "#FFE24D",  # bright warm yellow
    "yoto_orange":  "#FF8C42",  # warm amber/orange
    "midea_teal":   "#14B8A6",  # cool teal (kept clear of the bright blue)
    "light_grey":   "#808080",  # readable mid grey (true grey, not dim white)
}
# Approximate RGB for the base ANSI colors, so accents named by ANSI (e.g. the
# Router's "green") can also be lightened.
_ANSI_RGB = {
    "white": (229, 229, 229), "green": (0, 205, 0), "blue": (0, 0, 238),
    "magenta": (205, 0, 205), "cyan": (0, 205, 205), "yellow": (205, 205, 0),
    "red": (205, 0, 0),
}
# Fallback ANSI name when the terminal can't render the hex color.
_HEX_FALLBACK = {
    "hue_blue": "blue",
    "roku_purple": "magenta",
    "sonos_yellow": "yellow",
    "yoto_orange": "yellow",
    "midea_teal": "cyan",
    "light_grey": "white",
}

# Per-system accent colors (used for borders + highlights; body text stays white).
SYSTEM_COLORS = {
    "router": "green",
    "hue": "hue_blue",
    "roku": "roku_purple",
    "sonos": "sonos_yellow",
    "yoto": "yoto_orange",
    "midea": "midea_teal",
}

_PAIRS: dict[str, int] = {}
_DIM: set[str] = set()
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
    """Allocate one color pair per named color. Call once after curses init."""
    curses.start_color()
    curses.use_default_colors()
    pair = 1
    for name, fg in _COLOR_FG.items():
        curses.init_pair(pair, fg, -1)
        _PAIRS[name] = pair
        pair += 1
    # "grey" renders as dim white.
    _PAIRS["grey"] = _PAIRS["white"]
    _DIM.add("grey")
    _PAIRS[""] = 0  # default terminal color

    # Resolve the custom hex accent colors.
    n_colors = getattr(curses, "COLORS", 0)
    try:
        can_change = curses.can_change_color()
    except curses.error:
        can_change = False
    custom_slot = 16  # leave the 16 base ANSI slots untouched
    for name, hexv in _HEX_COLORS.items():
        r, g, b = _hex_rgb(hexv)
        fg: int | None = None
        if can_change and 16 <= custom_slot < n_colors:
            try:
                curses.init_color(custom_slot, r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)
                fg = custom_slot
                custom_slot += 1
            except curses.error:
                fg = None
        if fg is None and n_colors >= 256:
            fg = _nearest_256(r, g, b)
        if fg is None:
            _PAIRS[name] = _PAIRS.get(_HEX_FALLBACK[name], 0)
            continue
        try:
            curses.init_pair(pair, fg, -1)
            _PAIRS[name] = pair
            pair += 1
        except curses.error:
            _PAIRS[name] = _PAIRS.get(_HEX_FALLBACK[name], 0)

    # Drop any dynamic RGB pairs from a previous init; they re-register lazily
    # against the freshly allocated pair numbers. Record where free slots begin.
    global _next_pair
    for nm in _dynamic_names:
        _PAIRS.pop(nm, None)
    _dynamic_names.clear()
    _next_pair = pair


def rgb_color(r: int, g: int, b: int) -> str:
    """Return a Seg/attr color name that renders approximately ``(r, g, b)``.

    The triple is mapped to the nearest xterm-256 cube color and a curses pair is
    allocated for it on first use (cached, so repeated colors share one pair).
    Falls back to ``"white"`` when the terminal lacks 256 colors or pair slots run
    out. Must be called after init_colors() (i.e. during rendering)."""
    global _next_pair
    if getattr(curses, "COLORS", 0) < 256:
        return "white"
    idx = _nearest_256(r, g, b)
    name = f"x256:{idx}"
    if name in _PAIRS:
        return name
    if _next_pair >= getattr(curses, "COLOR_PAIRS", 256):
        return "white"
    try:
        curses.init_pair(_next_pair, idx, -1)
    except curses.error:
        return "white"
    _PAIRS[name] = _next_pair
    _dynamic_names.add(name)
    _next_pair += 1
    return name


def _color_rgb(color: str) -> tuple[int, int, int] | None:
    """RGB for a named color (custom hex accent or base ANSI), or None."""
    if color in _HEX_COLORS:
        return _hex_rgb(_HEX_COLORS[color])
    return _ANSI_RGB.get(color)


def lighten(color: str, t: float = 0.4) -> str:
    """Return a Seg/attr color name for ``color`` blended ``t`` of the way toward
    white — a brighter shade of the same hue, allocated as a true-color pair.
    Used to make a selected row's accent (e.g. the brightness bar) pop, since
    A_BOLD only brightens the base ANSI colors, not custom 256-color accents.
    Falls back to the original name when the RGB can't be resolved."""
    rgb = _color_rgb(color)
    if rgb is None:
        return color
    r, g, b = (round(c + (255 - c) * t) for c in rgb)
    return rgb_color(r, g, b)


def attr(color: str = "", *, bold: bool = False, dim: bool = False, reverse: bool = False) -> int:
    """Build a curses attribute from a color name + flags."""
    a = curses.color_pair(_PAIRS.get(color, 0))
    if bold:
        a |= curses.A_BOLD
    if dim or color in _DIM:
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


Line = list[Seg]


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


def hint(key: str, rest: str, color: str, *, paren: bool = False, key_color: str | None = None) -> Line:
    """One toolbar key hint: the hot key brightened, the label in the plain
    accent color (not dimmed/greyed — just not as bright as the key).

    ``paren=True`` wraps the key in parens inline with the label, e.g.
    ``hint("s", "cenes", color, paren=True)`` -> "(s)cenes". Otherwise the key
    stands alone before a space-separated label, e.g. ``hint("↕", "nav", color)``
    -> "↕ nav".

    ``key_color`` overrides the auto-lightened key color — e.g. to match the
    key to a section header's exact accent instead of a paler tint of it.
    """
    bright = key_color if key_color is not None else lighten(color)
    if paren:
        return [Seg("(", color), Seg(key, bright, bold=True), Seg(f"){rest}", color)]
    return [Seg(key, bright, bold=True), Seg(f" {rest}", color)]


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

    def segs(self, row: int, line: Line, col: int = 0) -> None:
        """Write a list of styled segments on a single row."""
        for seg in line:
            col = self.text(row, col, seg.text, seg.color,
                            bold=seg.bold, dim=seg.dim, reverse=seg.reverse)
            if col >= self.width:
                break

    def fill_row(self, row: int, ch: str = " ", color: str = "") -> None:
        self.text(row, 0, ch * self.width, color)


def select_row(region: Region, row: int, text: str, *, sel: bool, accent: str,
               col: int = 0) -> None:
    """Draw a selectable list row. Replaces reverse-video selection with an accent ▶
    cursor + bold text when selected (a blank cursor + normal text otherwise). ``text``
    must not include its own cursor/marker — this owns the leading two columns."""
    region.text(row, col, "▶ " if sel else "  ", accent if sel else "", bold=sel)
    region.text(row, col + 2, text, bold=sel)


def draw_box(stdscr: curses.window, top: int, left: int, height: int, width: int,
             title: str, color: str, *, focused: bool = False) -> Region:
    """Draw a rounded border with an embedded title; return the interior Region.

    The border (and title) use the system's accent color; focused boxes draw the
    border bold. The interior is cleared. Interior region is inset by 1 on all
    sides, with one extra column of padding on left/right for breathing room.
    """
    border = attr(color, bold=focused)

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
