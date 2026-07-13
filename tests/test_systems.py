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


def test_sonos_trunc():
    assert sonos.trunc("hello", 10) == "hello"       # fits, unchanged
    assert sonos.trunc("hello", 5) == "hello"         # exact fit, no marker
    assert sonos.trunc("hello world", 8) == "hello..."
    assert len(sonos.trunc("hello world", 8)) == 8
    assert sonos.trunc("hello world", 3) == "..."     # too narrow for text + marker
    assert sonos.trunc("hello world", 0) == ""
    assert sonos.trunc("hello world", -1) == ""


# --- Roku --------------------------------------------------------------------


def test_fmt_ms():
    assert roku._fmt_ms("83000 ms") == "1:23"
    assert roku._fmt_ms("") == ""
    assert roku._fmt_ms("garbage") == ""
    assert roku._fmt_ms("0 ms") == "0:00"


def test_roku_badge():
    assert roku.badge("play") == ("▶ PLAYING", False)   # bright accent
    assert roku.badge("pause") == ("⏸ PAUSED", True)    # dimmed accent
    assert roku.badge("close") == ("■ IDLE", False)


def _mock_roku(monkeypatch):
    monkeypatch.setenv("HOME_CONTROL_MOCK", "1")
    system = roku.RokuSystem()
    system.poll(True)  # load fixture device/apps
    return system


def test_digit_apps_skip_lettered_shortcuts(monkeypatch):
    rk = _mock_roku(monkeypatch)
    digits = rk.digit_apps()
    # Mock fixture order, minus the five lettered shortcut apps.
    assert [(d, name) for d, _, name in digits] == [
        ("1", "Hulu"), ("2", "Disney+"), ("3", "Spotify"),
    ]


def test_send_text_url_encodes_lit_keypress(monkeypatch):
    ctl = roku.RokuController(ip="192.0.2.1")
    ctl.mock = False
    posts: list[str] = []
    monkeypatch.setattr(ctl, "_post", lambda path: (posts.append(path), True)[1])
    ctl.send_text("a")
    ctl.send_text(" ")
    ctl.send_text("&")
    assert posts == ["keypress/Lit_a", "keypress/Lit_%20", "keypress/Lit_%26"]


def test_keyboard_mode_forwards_and_swallows_keys(monkeypatch):
    import curses

    rk = _mock_roku(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(rk.ctl, "send_text", lambda ch: sent.append(ch))
    monkeypatch.setattr(rk.ctl, "key", lambda name: sent.append(f"<{name}>"))

    assert rk.handle_key(ord("\\")) and rk.mode == "keyboard"
    for ch in "hi":
        assert rk.handle_key(ord(ch))
    assert rk.typed == "hi" and sent == ["h", "i"]
    assert rk.handle_key(curses.KEY_BACKSPACE)
    assert rk.typed == "h" and sent[-1] == "<Backspace>"
    # 'q' must not fall through to shell globals (quit) — it's literal text now.
    assert rk.handle_key(ord("q")) and sent[-1] == "q"
    assert rk.handle_key(curses.KEY_UP) and sent[-1] == "<Up>"  # arrows still navigate
    assert rk.handle_key(27) and rk.mode == "remote"            # ESC exits, sends nothing
    assert sent[-1] == "<Up>"


def test_search_mode_buffers_locally_and_sends_once(monkeypatch):
    import curses

    rk = _mock_roku(monkeypatch)
    searches: list[str] = []
    sent: list[str] = []
    monkeypatch.setattr(rk.ctl, "search", lambda kw: searches.append(kw))
    monkeypatch.setattr(rk.ctl, "key", lambda name: sent.append(name))
    monkeypatch.setattr(rk.ctl, "send_text", lambda ch: sent.append(ch))

    rk.typed = "stale"
    assert rk.handle_key(ord("/")) and rk.mode == "search" and rk.typed == ""
    for ch in "the matrix":
        assert rk.handle_key(ord(ch))
    assert rk.handle_key(curses.KEY_BACKSPACE) and rk.typed == "the matri"
    assert sent == [] and searches == []      # nothing hits the device while typing
    assert rk.handle_key(ord("\n"))           # ⏎ fires exactly one search/browse
    assert searches == ["the matri"] and rk.mode == "remote"

    # ESC cancels without sending anything.
    rk.handle_key(ord("/"))
    rk.handle_key(ord("x"))
    assert rk.handle_key(27) and rk.mode == "remote" and searches == ["the matri"]

    # ⏎ on an empty/whitespace buffer sends nothing and stays in search mode.
    rk.handle_key(ord("/"))
    rk.handle_key(ord(" "))
    assert rk.handle_key(ord("\n"))
    assert rk.mode == "search" and searches == ["the matri"]


def test_captures_text_only_in_typing_modes(monkeypatch):
    rk = _mock_roku(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(rk.ctl, "send_text", lambda ch: sent.append(ch))
    assert not rk.captures_text()          # remote mode: SPACE stays push-to-talk
    rk.handle_key(ord("a"))
    assert not rk.captures_text()          # apps mode too
    rk.handle_key(27)
    rk.handle_key(ord("\\"))
    assert rk.captures_text()              # keyboard mode: SPACE is a character
    rk.handle_key(ord(" "))
    assert sent == [" "] and rk.typed == " "
    rk.handle_key(27)
    rk.handle_key(ord("/"))
    assert rk.captures_text()              # search mode: SPACE is a character
    rk.handle_key(ord(" "))
    assert rk.typed == " " and sent == [" "]  # buffered locally, not sent


def test_controller_search_url_encodes(monkeypatch):
    ctl = roku.RokuController(ip="192.0.2.1")
    ctl.mock = False
    posts: list[str] = []
    monkeypatch.setattr(ctl, "_post", lambda path: (posts.append(path), True)[1])
    ctl.search("purple pineapple & co")
    assert posts == ["search/browse?keyword=purple%20pineapple%20%26%20co"]


def test_clamp_scroll_keeps_cursor_visible():
    # cursor below window → scroll to show it at the bottom
    assert sonos._clamp_scroll(cursor=10, scroll=0, visible=5) == 6
    # cursor above window → scroll up to it
    assert sonos._clamp_scroll(cursor=2, scroll=5, visible=5) == 2
    # already visible → unchanged
    assert sonos._clamp_scroll(cursor=3, scroll=2, visible=5) == 2
