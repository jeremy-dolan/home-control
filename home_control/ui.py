"""Shared curses UI primitives: colors, styled segments, box drawing, regions.

This module is the only place that knows how to talk to curses for drawing.
Layout *math* lives in layout.py (pure, testable); device logic lives in
systems/. Keeping curses confined here keeps the rest of the app headless-testable.
"""

from __future__ import annotations

import colorsys
import curses
import textwrap
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

# ===========================================================================
# UI conventions
#
# The visual language every panel shares.
#
# Drawing. Panels draw only through this module, never raw curses; layout math
#   lives in layout.py. Each panel is a rounded box (draw_box) bordered and
#   titled in its system accent, its interior a clipped Region (an 80-column
#   terminal yields a 76-column interior). The focused panel expands to fill
#   leftover height and brightens its border; the rest stay collapsed. Design
#   for 80 columns.
#
# Colour. One PALETTE, authored as RGB hex for a 256-colour terminal — below
#   that every name falls through to the default foreground (no 8-colour
#   variant by design; layout, weight, cursors and glyphs carry the UI without
#   hue). Entries are semantic roles that mean the same in any panel — warn
#   (working, wants attention), fault (unreachable/failed), muted (a value that
#   is itself off/absent), info_teal/info_green (neutral secondary series,
#   two so one panel can carry two at once), neutral/neutral_dim
#   (hueless chrome for UI the shell owns rather than a device) — plus one base
#   accent per system in SYSTEM_COLORS. Accent is chrome only (borders, cursors,
#   hotkeys, section headers, bars); body text stays the terminal default.
#
# Accents are base shades. lighten(accent) raises HSL lightness (holding hue
#   and saturation) for the brighter form used by hotkeys, focused borders and
#   selected rows — A_BOLD can't brighten a 256-colour pair. Author accents with
#   headroom; one near the top of its lightness range makes the two shades read
#   as one.
#
# Popups. While a popup is open (help, a modal alert, the voice overlay) the
#   panels behind it are re-drawn dimmed by backdrop() — each colour keeps its
#   hue and is scaled toward the background, so a backgrounded panel still
#   reads as itself while the popup is the only thing at full strength. Panels
#   do nothing to opt in: the substitution happens in attr(), which every draw
#   already goes through. Draw a popup *outside* the backdrop() block or it
#   dims with its backdrop.
#
# Badges. Every panel leads its collapsed line and expanded header with a
#   "● LABEL" badge, coloured by badge_color(state, accent): BADGE_ACTIVE ->
#   accent + bold (doing its job), BADGE_IDLE -> muted (reachable but
#   off/stopped), BADGE_FAULT -> fault. An item going unreachable (one light,
#   one AC unit) is IDLE; FAULT is reserved for a whole panel's device being
#   unreachable. Pad the label to a fixed width so the column after it doesn't
#   shift as the state changes. Label wording tracks the kind of state: -ing for
#   active work (PLAYING, LOADING — the only two, and both ACTIVE), an adjective
#   for a resting condition (IDLE, ONLINE, ASLEEP), -ed for a state someone put
#   the device into (PAUSED, STOPPED, CONNECTED).
#
# Selection. cursor(accent, sel) — an accent "▶ " when selected, else two
#   blanks — is the guaranteed cue and owns the leading two columns of every
#   selectable row. highlight(line, accent) is optional reinforcement (bold
#   every segment, clear dim, lift accent segments to
#   lighten(accent)), used by dense scrolling lists (Hue, Sonos) and
#   deliberately not by card layouts (Midea, whose _dim already means
#   off/unreachable, so row-bold would collide). Seg(lift=False) opts a segment
#   out of the lift; the cursor uses it, so the marker stays base while the row
#   it marks brightens.
#
# dim vs muted. dim=True is secondary/supporting text (labels, hints, static
#   identity) and is cleared by selection bolding. muted is a value that is
#   itself off/absent/inactive and survives it.
#
# Primitives & glyphs. level_bar() (a "━━━◉───" slider, ◉ knob), toggle_dot()
#   (●/○), hint()/hint_row() (toolbar hints: hotkey brightened + bold, label in
#   plain accent), justify()/pad_between() (left/right-aligned rows),
#   select_row() (plain-text selectable row). ● leads a badge ("● ONLINE") and
#   trails a toggle ("Eco ●"); ◉ is only the level-bar knob. Box chrome is
#   rounded (╭╮╰╯); square corners (┌┐└┘) are reserved for content nested inside
#   a panel (Roku's input boxes) — a deliberate content-vs-chrome cue.
# ===========================================================================


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
# hue. See the "UI conventions" block at the top of this module.
PALETTE = {
    # -- Semantic roles: what a color *means*, in any panel. ----------------
    "warn":  "#E3B341",  # working, but wants attention (filter due, error code)
    "fault": "#F85149",  # unreachable, offline, failed
    "muted": "#8A8A8A",  # a value that is itself off/absent/inactive
    # Neutral secondary series — a colour for something that needs to stand
    # apart without claiming to be a device. Two of them, so a panel can carry
    # two series at once (the Router charts) and still read as one family.
    "info_teal":  "#39C5CF",
    "info_green": "#00C300",  # picked to pop: where ANSI 32 lands on a stock xterm

    # Hueless chrome for UI that belongs to no system — the voice overlay floats
    # above every panel, so an accent would imply it acts on that one device.
    # White reads as "the shell is asking", and the dim shade keeps the overlay's
    # key hints below its border in the same way an accent's base sits below
    # lighten()'s bright form.
    #
    # `neutral` is deliberately the one role with no lighten() headroom: the
    # overlay is always drawn focused, so the lift is a no-op and the border
    # stays pure white.
    "neutral":     "#FFFFFF",
    "neutral_dim": "#9E9E9E",

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
_next_pair = 1   # next free curses pair number
_next_slot = 16  # next free colour slot to redefine (base ANSI 0-15 left alone)
_can_change = False  # terminal supports init_color, so colours can be exact
_dynamic_names: set[str] = set()  # lazily-allocated RGB pairs, cleared on re-init

# xterm-256 color cube levels, for nearest-color fallback.
_CUBE = (0, 95, 135, 175, 215, 255)


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _nearest_256(r: int, g: int, b: int) -> int:
    """Map an RGB triple to the closest xterm-256 color, cube or greyscale ramp.

    The 6x6x6 cube's grey diagonal has only six steps, so a near-grey snapping
    to the cube alone quantises brutally — the backdrop's ten grey levels
    collapsed to two before the 232-255 ramp was considered here. The ramp is 24
    steps of 10, and losing to the cube whenever the cube has an exact match, so
    adding it strictly improves the fit and leaves every existing cube colour
    where it was.
    """
    def lvl(v: int) -> int:
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))

    cube = 16 + 36 * lvl(r) + 6 * lvl(g) + lvl(b)
    cube_err = sum((_CUBE[lvl(v)] - v) ** 2 for v in (r, g, b))

    k = min(range(24), key=lambda i: abs(8 + 10 * i - (r + g + b) / 3))
    grey = 8 + 10 * k
    grey_err = sum((grey - v) ** 2 for v in (r, g, b))

    return 232 + k if grey_err < cube_err else cube


def init_colors() -> None:
    """Allocate one curses pair per palette entry. Call once after curses init.

    On a terminal with fewer than 256 colors nothing is allocated, so every name
    falls through to the default foreground in `attr()`.
    """
    global _next_pair, _next_slot, _can_change
    curses.start_color()
    curses.use_default_colors()
    _PAIRS.clear()
    _PAIRS[""] = 0  # default terminal color
    _dynamic_names.clear()
    _backdrop_names.clear()  # receded names point at pairs about to be re-allocated
    _next_pair = 1
    _next_slot = 16  # leave the 16 base ANSI slots untouched
    _can_change = False

    n_colors = getattr(curses, "COLORS", 0)
    if n_colors < 256:
        return
    try:
        _can_change = curses.can_change_color()
    except curses.error:
        _can_change = False

    for name, hexv in PALETTE.items():
        _PAIRS[name] = _alloc(*_hex_rgb(hexv))


def _alloc(r: int, g: int, b: int) -> int:
    """Allocate a curses pair rendering ``(r, g, b)``, returning its pair number.

    Where the terminal can redefine colors, this claims the next free slot and
    sets it to the exact RGB — palette roles and dynamic colors draw from the
    same slot counter, so no two colors can ever be handed the same slot.

    Only where it *can't* does the triple snap to the nearest stock cube color,
    and there the snap is safe precisely because nothing has been redefined: an
    index means what the terminal says it means. Mixing the two — snapping to a
    stock index on a terminal where slots have been redefined — is what makes a
    dark colour come back as some unrelated palette hue at full brightness.

    Returns pair 0 (the default foreground) once slots or pairs run out.
    """
    global _next_pair, _next_slot
    if _next_pair >= getattr(curses, "COLOR_PAIRS", 256):
        return 0
    fg = _nearest_256(r, g, b)
    if _can_change and _next_slot < getattr(curses, "COLORS", 0):
        try:
            curses.init_color(_next_slot, r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)
            fg = _next_slot
            _next_slot += 1
        except curses.error:
            pass
    try:
        curses.init_pair(_next_pair, fg, -1)
    except curses.error:
        return 0
    _next_pair += 1
    return _next_pair - 1


def rgb_color(r: int, g: int, b: int) -> str:
    """Return a Seg/attr color name that renders ``(r, g, b)``.

    Exact where the terminal can redefine colors, nearest-stock-cube where it
    can't. The pair is allocated on first use and cached under the RGB triple,
    so repeated colors share one pair. Falls back to the default foreground when
    the terminal lacks 256 colors or slots/pairs run out. Must be called after
    init_colors() (i.e. during rendering).
    """
    if getattr(curses, "COLORS", 0) < 256:
        return ""
    name = f"rgb:{r},{g},{b}"
    if name in _PAIRS:
        return name
    pair = _alloc(r, g, b)
    if pair == 0:
        return ""
    _PAIRS[name] = pair
    _dynamic_names.add(name)
    return name


# --- backdrop dimming ------------------------------------------------------
# While a popup is open the panels behind it are re-drawn dimmed, so the popup
# reads as the only live thing on screen. Each colour keeps its hue and is
# scaled toward the terminal background, so a backgrounded panel still reads as
# itself — Hue blue stays blue, just quiet.
#
# Where the terminal can redefine colour slots the dimmed shade is exact. Where
# it can't, it snaps to the 6x6x6 cube, which is coarsest exactly where these
# colours land; hue drifts by up to ~35 degrees on a pale accent, a tint rather
# than a change of colour. (Searching all 240 stock colours by weighted
# distance instead of rounding each channel gives bit-identical results — the
# coarseness is the cube's, not the rounding's.)
#
# Not A_DIM: dimming an already-dim cell is a no-op, so an attribute-level pass
# would flatten the panels' own dim-vs-normal hierarchy, and A_DIM|A_BOLD is
# contradictory enough that terminals disagree about which wins.
# How far toward the background a backdrop colour is pulled. Scaling RGB (not
# HSL lightness, which lighten() inverts) is what makes the drop uniform: a
# lightness cut barely moves a bright yellow, whose luminance lives in R+G
# sitting near max, leaving sonos_yellow the loudest thing behind the popup.
BACKDROP_SCALE = 0.55

# Body text draws in the terminal's default foreground (colour name ""), which
# we can't read back, so dimming assumes a light-grey default. Guessing low
# would leave body text — most of the screen — barely dimmed at all.
_DEFAULT_FG = (200, 200, 200)

_backdrop = False
_backdrop_names: dict[str, str] = {}  # colour name -> dimmed name, per init


@contextmanager
def backdrop(active: bool = True) -> Generator[None]:
    """Draw everything inside the block dimmed behind a popup.

    Wraps the panel pass in `Shell.render`; the popup itself is drawn outside
    the block, at full strength. A no-op when `active` is False, so the caller
    can wrap unconditionally.
    """
    global _backdrop
    prev = _backdrop
    _backdrop = active
    try:
        yield
    finally:
        _backdrop = prev


def _cube_rgb(idx: int) -> tuple[int, int, int] | None:
    """RGB for an xterm-256 index — the inverse of `_nearest_256` over the
    6x6x6 cube, plus the 232-255 greyscale ramp. Returns None for the 16 base
    ANSI slots, whose actual colours are the terminal's business, not ours."""
    if 16 <= idx <= 231:
        i = idx - 16
        return _CUBE[i // 36], _CUBE[(i // 6) % 6], _CUBE[i % 6]
    if 232 <= idx <= 255:
        v = 8 + 10 * (idx - 232)
        return v, v, v
    return None


def _color_rgb(name: str) -> tuple[int, int, int] | None:
    """RGB behind a Seg/attr colour name — a PALETTE role, an `rgb:` name from
    rgb_color/lighten, or "" for the assumed default foreground."""
    if not name:
        return _DEFAULT_FG
    if name in PALETTE:
        return _hex_rgb(PALETTE[name])
    if name.startswith("rgb:"):
        try:
            r, g, b = (int(v) for v in name[4:].split(","))
        except ValueError:
            return None
        return r, g, b
    return None


def backdrop_rgb(color: str, k: float = BACKDROP_SCALE) -> tuple[int, int, int] | None:
    """The dimmed form of `color` behind a popup, or None for a name with no
    resolvable colour.

    Curses-free, so the invariants are unit-testable: every colour the app can
    draw dims into the band, and luminance order is preserved so the backdrop
    keeps its own bright-to-dim ranking.

    Covers lighten()'s dynamic `rgb:` names as well as palette roles — hotkeys,
    focused borders and selected rows all draw in those, and they are the
    brightest cells on screen, so missing them would leave the backdrop's
    loudest text lit.
    """
    rgb = _color_rgb(color)
    if rgb is None:
        return None
    return tuple(round(c * k) for c in rgb)  # type: ignore[return-value]


def _dimmed(color: str) -> str:
    """Cached `backdrop_rgb` -> allocated colour name, for the per-cell hot path."""
    if color in _backdrop_names:
        return _backdrop_names[color]
    rgb = backdrop_rgb(color)
    name = color if rgb is None else rgb_color(*rgb)
    _backdrop_names[color] = name
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
    rgb = _lighten_rgb(color, t)
    if rgb is None:
        return color
    return rgb_color(*rgb)


def _lighten_rgb(color: str, t: float = 0.4) -> tuple[int, int, int] | None:
    """The pure HSL lift behind `lighten()`: the RGB triple for a palette
    `color` raised `t` toward full lightness, or None when `color` isn't in the
    palette. Curses-free, so the accent-headroom invariant is unit-testable —
    an accent authored with no headroom quantises to the same 256-cube index as
    its base and the two shades render as one."""
    if color not in PALETTE:
        return None
    r, g, b = (c / 255 for c in _hex_rgb(PALETTE[color]))
    h, lum, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, lum + (1 - lum) * t, s)
    return round(r * 255), round(g * 255), round(b * 255)


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


def attr(color: str = "", *, bold: bool = False, dim: bool = False) -> int:
    """Build a curses attribute from a color name + flags.

    Inside a `backdrop()` block the colour is swapped for its receded shade.
    Every draw in the app routes through here, so that one substitution covers
    the whole screen without any panel knowing a popup is open. `dim` is left
    applied on top, keeping each panel's own dim-vs-normal split legible while
    the whole field recedes.
    """
    if _backdrop:
        color = _dimmed(color)
    a = curses.color_pair(_PAIRS.get(color, 0))
    if bold:
        a |= curses.A_BOLD
    if dim:
        a |= curses.A_DIM
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


def justify(left: Line, right: Line, width: int) -> Line:
    """Combine left + right styled runs with a space pad so right hugs the edge."""
    gap = max(1, width - seg_len(left) - seg_len(right))
    return [*left, Seg(" " * gap), *right]


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
             dim: bool = False) -> int:
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
                               attr(color, bold=bold, dim=dim))
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
            col = self.text(row, col, seg.text, seg.color, bold=seg.bold, dim=seg.dim)
            if col >= self.width:
                break

    def fill_row(self, row: int, ch: str = " ", color: str = "") -> None:
        self.text(row, 0, ch * self.width, color)


def cursor(accent: str, sel: bool) -> Seg:
    """The leading two columns of a selectable row: an accent ▶ when selected, two
    blanks otherwise. The cursor plus bolding the row marks a selection (see the
    "UI conventions" block at the top of this module), so every list builds its
    rows starting with this Seg."""
    return Seg("▶ ", accent, bold=True, lift=False) if sel else Seg("  ")


def toggle_dot(on: bool) -> str:
    """The app's boolean indicator: filled ● when on, hollow ○ when off.

    Trails its label ("Eco ●"), which is what keeps it clear of the ● status dot
    that *leads* a badge ("● ONLINE") — that, and the hollow counterpart, which
    no status dot has.
    """
    return "●" if on else "○"


def highlight(line: Line, accent: str) -> Line:
    """Mark a whole row as selected: bold every segment, clear dim so the bold
    reads, and lift every segment already carrying ``accent`` to ``lighten(accent)``.

    That last step is what keeps a selected row coherent. A_BOLD can't brighten a
    256-color pair, so a row whose bar was explicitly lightened but whose accent
    text was only bolded ends up half-highlighted — the slider moves, the ``● ON``
    beside it doesn't. Callers therefore build rows with the *base* accent
    throughout and let this do the lifting. Segments marked ``lift=False`` — the
    ▶ cursor — keep their own colour.

    Mutates and returns the *same* ``Seg`` objects, so pass a freshly built row
    each frame: applied twice (or to a cached/shared ``Line``) it compounds —
    the second pass sees dim already cleared and the accent already lifted, so
    an un-selected row that reused those segments would stay bright. Panels
    rebuild their rows every render, which is what keeps this safe.
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
