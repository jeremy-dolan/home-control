"""Tests for the curses-free drawing primitives in `home_control.ui`."""

from home_control.ui import BADGE_ACTIVE, BADGE_FAULT, BADGE_IDLE, PALETTE, SYSTEM_COLORS, badge_color, cursor, hint


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
