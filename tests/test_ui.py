"""Tests for the curses-free drawing primitives in `home_control.ui`."""

import colorsys
import curses

import pytest

from home_control import ui
from home_control.ui import (
    BADGE_ACTIVE,
    BADGE_FAULT,
    BADGE_IDLE,
    PALETTE,
    SYSTEM_COLORS,
    Seg,
    _color_rgb,
    _cube_rgb,
    _hex_rgb,
    _lighten_rgb,
    _nearest_256,
    backdrop_rgb,
    badge_color,
    cursor,
    highlight,
    hint,
)


def _text(segs):
    return "".join(s.text for s in segs)


def test_hint_plain_key_stands_alone():
    assert _text(hint("ENTER", "play/pause", "sonos")) == "ENTER play/pause"


def test_hint_paren_wraps_leading_letter():
    assert _text(hint("s", "scenes", "hue", paren=True)) == "(s)cenes"


def test_hint_paren_wraps_non_leading_letter():
    # the hot key need not lead the label — first match wins
    assert _text(hint("u", "queue", "sonos", paren=True)) == "q(u)eue"


def test_hint_paren_matches_case_insensitively_but_keeps_key_case():
    assert _text(hint("U", "queue", "sonos", paren=True)) == "q(U)eue"
    assert _text(hint("S", "stop", "sonos", paren=True)) == "(S)top"


def test_hint_paren_falls_back_to_prefix_when_key_absent():
    assert _text(hint("F5", "refresh", "router", paren=True)) == "(F5) refresh"


def test_hint_paren_brightens_only_the_key():
    segs = hint("u", "queue", "sonos", paren=True)
    bold = [s for s in segs if s.bold]
    assert len(bold) == 1 and bold[0].text == "u"


# --- colour model ----------------------------------------------------------


def test_badge_color_maps_states_to_roles():
    # Active badges carry the panel's own accent; idle and fault are shared roles
    # so the same situation reads the same colour in every panel.
    assert badge_color(BADGE_ACTIVE, "hue_blue") == "hue_blue"
    assert badge_color(BADGE_IDLE, "hue_blue") == "muted"
    assert badge_color(BADGE_FAULT, "hue_blue") == "fault"


def test_badge_color_treats_unknown_states_as_faults():
    assert badge_color("nonsense", "hue_blue") == "fault"


def test_every_system_accent_is_a_palette_entry():
    # A typo'd accent would silently render as the default foreground.
    assert set(SYSTEM_COLORS.values()) <= set(PALETTE)


def test_palette_colors_are_hex_triples():
    for name, value in PALETTE.items():
        assert len(value) == 7 and value.startswith("#"), name
        int(value[1:], 16)


def test_cursor_owns_two_columns_either_way():
    # The selected/unselected cursor must be the same width or rows would shift.
    assert len(cursor("hue_blue", True).text) == len(cursor("hue_blue", False).text) == 2
    assert cursor("hue_blue", True).color == "hue_blue"
    assert cursor("hue_blue", False).color == ""


# --- accent headroom -------------------------------------------------------
# An accent authored too light leaves lighten() no room: base and lifted shades
# quantise to the same 256-cube index and read as one colour. That is the exact
# regression Router/Sonos/Yoto have each hit, so guard it for every accent.
# Distinctness is the objective floor; how *far* apart is a visual judgement.


def test_every_accent_lightens_to_a_distinct_256_index():
    for role in SYSTEM_COLORS.values():
        base_rgb = _hex_rgb(PALETTE[role])
        lit_rgb = _lighten_rgb(role)
        assert lit_rgb is not None, role
        base_idx = _nearest_256(*base_rgb)
        lit_idx = _nearest_256(*lit_rgb)
        assert base_idx != lit_idx, f"{role} has no lighten() headroom (base==lifted at index {base_idx})"


def test_lighten_rgb_is_none_outside_the_palette():
    assert _lighten_rgb("not_a_color") is None


# --- backdrop dimming ------------------------------------------------------
# Behind a popup every colour keeps its hue and is scaled toward the terminal
# background.
#
# The regression these guard: colours used to snap to the nearest *stock* cube
# index while init_colors() had redefined the low slots to palette hues, so a
# dimmed colour came back as an unrelated palette colour at full brightness —
# hue_blue landed on roku_purple's slot, router_green on its own. That is fixed
# in the allocator (one slot pool, exact colours where the terminal allows), so
# the guard against it lives with _alloc; what is left to check here is that
# dimming itself behaves. tmux reports can_change_color() false, so the
# redefinition never happens there and no tmux capture could have caught the
# original bug. These run headless and can.


def _drawable_colors():
    """Every colour name the app can hand to attr(): palette roles, lighten()'s
    dynamic names, and "" for the terminal default."""
    names = ["", *PALETTE]
    for role in PALETTE:
        lit = _lighten_rgb(role)
        if lit is not None:
            names.append("rgb:{},{},{}".format(*lit))
    return names


def _luma(rgb):
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_every_drawable_colour_dims():
    # Nothing may sit at full strength behind a popup, and nothing may go to
    # pure black — an invisible backdrop is as wrong as an undimmed one.
    for name in _drawable_colors():
        rgb = backdrop_rgb(name)
        assert rgb is not None, name
        src = _color_rgb(name)
        assert _luma(rgb) < _luma(src), f"{name} does not dim"
        assert max(rgb) > 0 or max(src) == 0, f"{name} dims to black"


def test_dimming_preserves_hue():
    # The point of dimming over greyscale: a backgrounded panel still reads as
    # itself. Exact here, since this is the maths before any quantisation.
    for role in PALETTE:
        src = _hex_rgb(PALETTE[role])
        dim = backdrop_rgb(role)
        assert dim is not None, role
        if max(src) - min(src) < 8:
            continue  # already neutral, no hue to keep
        assert colorsys.rgb_to_hls(*[c / 255 for c in src])[0] == pytest.approx(
            colorsys.rgb_to_hls(*[c / 255 for c in dim])[0], abs=0.01), role


def test_dimming_preserves_luminance_order():
    # The backdrop keeps its own ranking: a lit accent stays above body text,
    # which stays above dim labels. Scaling every channel by one factor makes
    # this exact, which an HSL lightness cut would not.
    rows = sorted((_luma(_color_rgb(n)), _luma(backdrop_rgb(n))) for n in _drawable_colors())
    for (_, a), (_, b) in zip(rows, rows[1:]):
        assert a <= b, f"dimming inverted the backdrop ordering ({a} > {b})"


def test_dimmed_hue_drift_through_quantisation_stays_a_tint():
    # On a terminal that cannot redefine slots the dimmed colour snaps to the
    # cube, which is coarsest exactly where these land. Drift is expected; a
    # change of colour is not. 45 degrees is a tint, "blue came back purple"
    # was more than twice that.
    for role in PALETTE:
        dim = backdrop_rgb(role)
        assert dim is not None, role
        quantised = _cube_rgb(_nearest_256(*dim))
        assert quantised is not None
        h1, _, s1 = colorsys.rgb_to_hls(*[c / 255 for c in dim])
        h2, _, s2 = colorsys.rgb_to_hls(*[c / 255 for c in quantised])
        if s1 < 0.08 or s2 < 0.08:
            continue  # near-grey, hue is not meaningful
        drift = abs(h1 - h2) * 360 % 360
        assert min(drift, 360 - drift) <= 45, f"{role} drifts {drift:.0f} degrees"


def test_backdrop_leaves_unresolvable_names_alone():
    assert backdrop_rgb("not_a_color") is None
    assert backdrop_rgb("rgb:nonsense") is None


def test_cube_rgb_inverts_nearest_256():
    # _cube_rgb underpins the quantisation check above; if it disagreed with
    # _nearest_256 that test would be checking the wrong colours.
    for idx in (16, 75, 145, 188, 231):
        rgb = _cube_rgb(idx)
        assert rgb is not None
        assert _nearest_256(*rgb) == idx


def test_lighten_rgb_raises_lightness_without_desaturating_to_white():
    # The lift brightens; it must not wash a saturated accent out to grey/white.
    r, g, b = _lighten_rgb("hue_blue")
    assert (r, g, b) != (255, 255, 255)
    assert max(r, g, b) - min(r, g, b) > 20  # still visibly chromatic


# --- highlight() -----------------------------------------------------------
# The selection reinforcement: bold every segment, clear dim so the bold reads,
# lift accent segments — but leave lift=False segments (the cursor) at their
# base colour so the marker itself doesn't brighten with the row.


def test_highlight_bolds_every_segment_and_clears_dim():
    line = [Seg("a", dim=True), Seg("b", "hue_blue"), Seg("c")]
    highlight(line, "hue_blue")
    assert all(s.bold for s in line)
    assert all(not s.dim for s in line)


def test_highlight_leaves_lift_false_segments_at_their_base_colour():
    cur = cursor("hue_blue", True)  # lift=False, colour == accent
    assert cur.lift is False and cur.color == "hue_blue"
    highlight([cur], "hue_blue")
    assert cur.color == "hue_blue"  # the ▶ marker must not brighten with its row


def test_highlight_only_touches_segments_carrying_the_accent():
    plain = Seg("body")  # default colour, not the accent
    highlight([plain], "hue_blue")
    assert plain.color == ""  # body text stays the terminal default


# --- colour slot allocation ------------------------------------------------
# The regression that broke the first backdrop attempt: init_colors() redefined
# colour slots from 16 up while rgb_color() snapped to the nearest *stock* cube
# index, so on a terminal supporting init_color a dark colour named a slot the
# palette had already overwritten and rendered as that palette hue at full
# brightness. One allocator now serves both. tmux reports can_change_color()
# false, so no tmux capture can exercise this path — these fakes can.


class _FakeCurses:
    """Just enough curses to drive init_colors()/rgb_color() headless."""

    COLORS = 256
    COLOR_PAIRS = 256
    error = curses.error

    def __init__(self, can_change):
        self._can_change = can_change
        self.redefined: dict[int, tuple] = {}   # slot -> rgb
        self.pairs: dict[int, int] = {}         # pair number -> foreground slot

    def start_color(self): pass
    def use_default_colors(self): pass
    def can_change_color(self): return self._can_change
    def init_color(self, slot, r, g, b): self.redefined[slot] = (r, g, b)
    def init_pair(self, pair, fg, bg): self.pairs[pair] = fg
    def color_pair(self, n): return n


def _allocate_everything():
    """Every colour the app asks for: palette roles at init, then the dynamic
    lighten() and backdrop shades derived from them."""
    ui.init_colors()
    for role in ui.PALETTE:
        ui.lighten(role)
        ui._dimmed(role)
    ui._dimmed("")


def test_no_two_colours_are_given_the_same_slot(monkeypatch):
    fake = _FakeCurses(can_change=True)
    monkeypatch.setattr(ui, "curses", fake)
    _allocate_everything()
    used = list(fake.pairs.values())
    assert used, "nothing was allocated"
    assert len(set(used)) == len(used), "two colours share one slot"


def test_every_colour_is_exact_where_the_terminal_allows(monkeypatch):
    # If a pair points at a slot this run did not redefine, it is showing
    # whatever that index happened to mean — which is how a dimmed blue came
    # back as Roku's purple.
    fake = _FakeCurses(can_change=True)
    monkeypatch.setattr(ui, "curses", fake)
    _allocate_everything()
    stray = {fg for fg in fake.pairs.values() if fg not in fake.redefined}
    assert not stray, f"pairs pointing at slots we never defined: {sorted(stray)}"


def test_nothing_is_redefined_when_the_terminal_cannot(monkeypatch):
    # The snap to stock indices is only safe because nothing has been
    # overwritten. If this ever redefines a slot, the snap becomes a lie.
    fake = _FakeCurses(can_change=False)
    monkeypatch.setattr(ui, "curses", fake)
    _allocate_everything()
    assert fake.redefined == {}
    assert all(16 <= fg <= 255 for fg in fake.pairs.values())
