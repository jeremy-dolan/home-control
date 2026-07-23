"""Tests for the curses-free drawing primitives in `home_control.ui`."""

from home_control.ui import (
    BADGE_ACTIVE,
    BADGE_FAULT,
    BADGE_IDLE,
    PALETTE,
    SYSTEM_COLORS,
    Seg,
    _hex_rgb,
    _lighten_rgb,
    _nearest_256,
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
