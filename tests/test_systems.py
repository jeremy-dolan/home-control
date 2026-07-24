"""Headless tests for pure helpers across the system modules (no curses init)."""

import threading
from types import SimpleNamespace

from home_control import ui
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
    # full → filled run + ◉ knob at the head, in the given accent colour
    full = hue.brightness_bar(254, on=True, color="hue_blue")
    assert _bar_text(full) == "━" * (hue.BAR_WIDTH - 1) + "◉"
    assert full[0].color == "hue_blue"
    # mid → constant width, exactly one knob
    mid = hue.brightness_bar(127, on=True, color="hue_blue")
    assert len(_bar_text(mid)) == hue.BAR_WIDTH and _bar_text(mid).count("◉") == 1


def test_hue_clock_sync_pushes_host_time(monkeypatch):
    # The mock config's fixed 2026-07-21 clock is always well past
    # CLOCK_DRIFT_WARN from "now", so the drift path is live under mock.
    monkeypatch.setenv("HOME_CONTROL_MOCK", "1")
    sysm = hue.HueSystem()
    sysm._open_sysinfo()

    def row(label):
        return next((t, s) for t, s in sysm.sysinfo_lines if label in t)

    local_text, local_style = row("Local time")
    utc_text, utc_style = row("UTC time")
    # Drifted: both rows share the red alert — one names the fault, one the fix.
    assert "(out of sync?)" in local_text and local_style == "alert"
    assert "'s' to sync local host time" in utc_text and utc_style == "alert"
    # Local time is shown above UTC time.
    order = [t for t, _ in sysm.sysinfo_lines]
    assert order.index(local_text) < order.index(utc_text)

    assert sysm.handle_key(ord("s")) is True
    assert "synced" in sysm.status().lower()

    # Re-polled config reflects the push: drift and its affordances are gone.
    local_after, local_style_after = row("Local time")
    utc_after, _ = row("UTC time")
    assert "(out of sync?)" not in local_after and local_style_after == ""
    assert "push time" not in utc_after


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
    # badge() returns a shared badge *state*; ui.badge_color maps it to a colour.
    assert sonos.badge("PLAYING") == ("▶ PLAYING", ui.BADGE_ACTIVE)
    assert sonos.badge("TRANSITIONING") == ("⟳ LOADING", ui.BADGE_ACTIVE)
    assert sonos.badge("PAUSED_PLAYBACK") == ("⏸ PAUSED", ui.BADGE_IDLE)
    assert sonos.badge("STOPPED") == ("■ STOPPED", ui.BADGE_IDLE)
    assert sonos.badge("NONSENSE") == ("■ STOPPED", ui.BADGE_IDLE)


def test_sonos_trunc():
    assert sonos.trunc("hello", 10) == "hello"       # fits, unchanged
    assert sonos.trunc("hello", 5) == "hello"         # exact fit, no marker
    assert sonos.trunc("hello world", 8) == "hello..."
    assert len(sonos.trunc("hello world", 8)) == 8
    assert sonos.trunc("hello world", 3) == "..."     # too narrow for text + marker
    assert sonos.trunc("hello world", 0) == ""
    assert sonos.trunc("hello world", -1) == ""


def test_discover_resolves_order_without_holding_lock(monkeypatch):
    """Regression: _apply_order resolves d.player_name, a live SoCo property that
    fires a SOAP request. It must run OUTSIDE self._lock — else a speaker that's
    reachable by SSDP but not on its control port (laptop on WiFi) blocks the main
    thread's snapshot() for the whole request timeout, freezing the TUI at idle."""
    import soco

    ctl = sonos.SonosController()
    ctl.mock = False
    ctl._pinned = []  # force the discovery path regardless of the ambient config
    monkeypatch.setattr(soco, "discover", lambda timeout=2: ["devA", "devB"])
    monkeypatch.setattr(ctl, "_poll_all", lambda: None)

    lock_free: dict[str, bool] = {}

    def fake_apply_order(devices: list) -> list:
        # From another thread, is the RLock takeable while _apply_order runs? It is
        # only if _discover isn't holding it across this (networked) call.
        got: list[bool] = []

        def probe() -> None:
            acquired = ctl._lock.acquire(blocking=False)
            got.append(acquired)
            if acquired:
                ctl._lock.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        lock_free["value"] = got[0]
        return list(devices)

    monkeypatch.setattr(ctl, "_apply_order", fake_apply_order)

    assert ctl._discover() is True
    assert ctl.discovered is True
    assert lock_free["value"] is True  # lock was NOT held during player_name resolution


def test_parse_speakers():
    parse = sonos._parse_speakers
    assert parse([]) == []
    assert parse(None) == []  # non-list → empty
    # ip-only, and name override; config order is preserved.
    assert parse([{"ip": "192.168.1.60"},
                  {"ip": "192.168.1.61", "name": "Kitchen"}]) == [
        ("192.168.1.60", None), ("192.168.1.61", "Kitchen")]
    # entries without an ip (or non-dict) are dropped; ip is stripped.
    assert parse([{"name": "no ip"}, "junk", {"ip": " 10.0.0.5 "}]) == [("10.0.0.5", None)]


def _zone(name, state="PLAYING", vol=30, grouped=False, title="", artist=""):
    track = sonos.TrackInfo(title=title, artist=artist) if title else None
    return sonos.ZoneState(name=name, transport_state=state, volume=vol, grouped=grouped, track=track)


def test_fully_grouped():
    assert sonos._fully_grouped([_zone("A", grouped=True), _zone("B", grouped=True)])
    assert not sonos._fully_grouped([_zone("A", grouped=True), _zone("B", grouped=False)])
    assert not sonos._fully_grouped([_zone("A", grouped=True)])  # single speaker isn't "grouped"
    assert not sonos._fully_grouped([])


def test_collapsed_height_dynamic():
    s = sonos.SonosSystem()
    s.ctl._pinned = []  # ignore any ambient config so the no-pins case is deterministic
    # No state yet: 1 row while discovering, one row per pinned speaker otherwise.
    assert s.collapsed_height == 1
    s.ctl._pinned = sonos._parse_speakers([{"ip": "1.1.1.1"}, {"ip": "1.1.1.2"}])
    assert s.collapsed_height == 2
    # Ungrouped → one row per speaker; fully grouped → the 2-line summary.
    s.ctl.zones = [_zone("A"), _zone("B"), _zone("C")]
    assert s.collapsed_height == 3
    s.ctl.zones = [_zone("A", grouped=True), _zone("B", grouped=True)]
    assert s.collapsed_height == 2


def _row_text(line):
    return "".join(seg.text for seg in line)


def test_independent_row_layout_and_dimming():
    s = sonos.SonosSystem()
    s._name_w = len("Living Room")
    playing = s._independent_row(
        _zone("Living Room", "PLAYING", 35, title="Shake It Off", artist="Taylor Swift"), width=70)
    text = _row_text(playing)
    # Badge padded to the widest label, name padded to the column, song + right vol present.
    assert text.startswith("▶ PLAYING".ljust(sonos.BADGE_W) + "  Living Room  ")
    assert "Shake It Off ─ Taylor Swift" in text
    assert text.rstrip().endswith("vol 35")
    # Playing → song + volume are NOT dimmed.
    assert not any(seg.dim for seg in playing if "Shake" in seg.text or seg.text == "vol 35")
    # Paused → song + volume dim; the badge still carries its own colour.
    paused = s._independent_row(_zone("Kitchen", "PAUSED_PLAYBACK", 20, title="Damaged Goods"), width=70)
    dimmed = {seg.text: seg.dim for seg in paused}
    assert dimmed["vol 20"] is True
    assert any(seg.dim and "Damaged Goods" in seg.text for seg in paused)


def test_pending_popup_lists_unpinned():
    s = sonos.SonosSystem()
    assert s.pending_popup() is None
    s.ctl._new_devices = ["Bedroom", "Office"]
    popup = s.pending_popup()
    assert popup is not None
    body = "\n".join(popup.lines)
    assert "Bedroom" in body and "Office" in body
    # Dismissal acknowledges (clears) the alert.
    s.dismiss_popup()
    assert s.pending_popup() is None


# --- Roku --------------------------------------------------------------------


def test_fmt_ms():
    assert roku._fmt_ms("83000 ms") == "1:23"
    assert roku._fmt_ms("") == ""
    assert roku._fmt_ms("garbage") == ""
    assert roku._fmt_ms("0 ms") == "0:00"


def test_roku_badge():
    assert roku.badge("play") == ("▶ PLAYING", ui.BADGE_ACTIVE)
    assert roku.badge("pause") == ("⏸ PAUSED", ui.BADGE_IDLE)
    assert roku.badge("close") == ("■ IDLE", ui.BADGE_IDLE)


def _mock_roku(monkeypatch):
    monkeypatch.setenv("HOME_CONTROL_MOCK", "1")
    system = roku.RokuSystem()
    system.poll(True)  # load fixture device/apps
    return system


def test_digit_apps_skip_lettered_shortcuts(monkeypatch):
    rk = _mock_roku(monkeypatch)
    digits = rk.digit_apps()
    # Mock fixture order, minus the lettered shortcut apps.
    assert [(d, name) for d, _, name in digits] == [
        ("1", "HBO Max"), ("2", "Spotify"),
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


def test_controller_search_navigates_types_and_focuses_results(monkeypatch):
    ctl = roku.RokuController(ip="192.0.2.1")
    ctl.mock = False
    posts: list[str] = []
    monkeypatch.setattr(ctl, "_post", lambda path: (posts.append(path), True)[1])
    monkeypatch.setattr(roku.time, "sleep", lambda s: None)
    ctl._search_sync("a &b")
    # Keypress-driven: home rail → Search → type → focus results.
    # (ECP search/browse only reaches the app store on modern Roku OS.)
    assert posts == [
        "keypress/Home", "keypress/Left",
        *["keypress/Up"] * roku.SEARCH_RAIL_UPS,
        *["keypress/Down"] * roku.SEARCH_RAIL_DOWNS,
        "keypress/Select",
        "keypress/Lit_a", "keypress/Lit_%20", "keypress/Lit_%26", "keypress/Lit_b",
        *["keypress/Right"] * roku.SEARCH_RESULT_RIGHTS,
    ]


def test_clamp_scroll_keeps_cursor_visible():
    # cursor below window → scroll to show it at the bottom
    assert sonos._clamp_scroll(cursor=10, scroll=0, visible=5) == 6
    # cursor above window → scroll up to it
    assert sonos._clamp_scroll(cursor=2, scroll=5, visible=5) == 2
    # already visible → unchanged
    assert sonos._clamp_scroll(cursor=3, scroll=2, visible=5) == 2
