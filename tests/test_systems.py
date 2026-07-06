"""Headless tests for pure helpers across the system modules (no curses init)."""

from types import SimpleNamespace

from home_control.systems import hue, roku, sonos

# --- Hue ---------------------------------------------------------------------


def test_next_bri_steps_and_bounds():
    assert hue.next_bri(0, 1) == hue.BRI_STOPS[0]      # up from off → first stop
    assert hue.next_bri(254, 1) == 254                 # already max
    assert hue.next_bri(1, -1) is None                 # below min → signal "off"
    assert hue.next_bri(254, -1) == hue.BRI_STOPS[-2]  # down from max


def _bar_text(segs):
    return "".join(s.text for s in segs)


def test_brightness_bar_shape():
    # off → just the dim empty track (no brackets, no knob)
    off = hue.brightness_bar(0, on=False)
    assert _bar_text(off) == "─" * hue.BAR_WIDTH
    assert all(s.dim for s in off)
    # full → filled run + ● knob at the head, in the given accent colour
    full = hue.brightness_bar(254, on=True, color="hue_blue")
    assert _bar_text(full) == "━" * (hue.BAR_WIDTH - 1) + "●"
    assert full[0].color == "hue_blue"
    # mid → constant width, exactly one knob
    mid = hue.brightness_bar(127, on=True, color="hue_blue")
    assert len(_bar_text(mid)) == hue.BAR_WIDTH and _bar_text(mid).count("●") == 1


# --- Sonos -------------------------------------------------------------------


def test_balance_pct_roundtrip():
    assert sonos._pct_to_balance(0) == (100, 100)
    assert sonos._pct_to_balance(100) == (0, 100)
    assert sonos._pct_to_balance(-100) == (100, 0)


def test_balance_to_pct_reads_tuple():
    assert sonos._balance_to_pct(SimpleNamespace(balance=(100, 100))) == 0
    assert sonos._balance_to_pct(SimpleNamespace(balance=(0, 100))) == 100
    assert sonos._balance_to_pct(SimpleNamespace(balance=(100, 0))) == -100
    assert sonos._balance_to_pct(SimpleNamespace(balance="bad")) == 0  # robust to junk


def test_int_slider_has_dot_and_centre_tick():
    # Value off-centre so the zero tick and the value dot are distinct cells.
    s = sonos._int_slider(5, -10, 10, width=15)
    assert s.startswith("[") and s.endswith("]") and len(s) == 17
    assert "●" in s and "│" in s  # value dot + zero tick when range straddles 0
    assert "│" not in sonos._int_slider(50, 0, 100)  # no tick for non-straddling range


def test_sonos_badge():
    assert sonos.badge("PLAYING") == ("▶ PLAYING", "green")
    assert sonos.badge("PAUSED_PLAYBACK")[0].startswith("⏸")
    assert sonos.badge("STOPPED") == ("■ STOPPED", "grey")


# --- Roku --------------------------------------------------------------------


def test_fmt_ms():
    assert roku._fmt_ms("83000 ms") == "1:23"
    assert roku._fmt_ms("") == ""
    assert roku._fmt_ms("garbage") == ""
    assert roku._fmt_ms("0 ms") == "0:00"


def test_roku_badge():
    assert roku.badge("play")[0].startswith("▶")
    assert roku.badge("pause")[0].startswith("⏸")
    assert roku.badge("close") == ("■ IDLE", "")


def test_clamp_scroll_keeps_cursor_visible():
    # cursor below window → scroll to show it at the bottom
    assert sonos._clamp_scroll(cursor=10, scroll=0, visible=5) == 6
    # cursor above window → scroll up to it
    assert sonos._clamp_scroll(cursor=2, scroll=5, visible=5) == 2
    # already visible → unchanged
    assert sonos._clamp_scroll(cursor=3, scroll=2, visible=5) == 2
