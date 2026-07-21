"""Tests for the curses-free drawing primitives in `home_control.ui`."""

from home_control.ui import hint


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
