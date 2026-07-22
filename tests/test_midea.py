"""Headless tests for the Midea AC panel (no curses init, no real network/asyncio)."""

import curses

import pytest

from home_control.systems import midea

# --- temperature conversion ----------------------------------------------------


def test_c_to_f_and_back():
    assert round(midea._c_to_f(24.0)) == 75
    assert round(midea._f_to_c(75.0)) == 24


def test_fmt_temp():
    assert midea._fmt_temp(None, True) == "—"
    assert midea._fmt_temp(24.0, True) == "75°F"
    assert midea._fmt_temp(24.0, False) == "24°C"


# --- badge/state logic -----------------------------------------------------------


def _unit(**kw: object) -> midea.MideaUnit:
    # Represents a real unit we've read at least once, so contacted=True by
    # default; pass contacted=False to model a never-reached placeholder.
    base: dict[str, object] = dict(id=1, ip="192.168.1.50", name="LR", online=True, contacted=True)
    base.update(kw)
    return midea.MideaUnit(**base)  # type: ignore[arg-type]


def test_unit_badge_cooling():
    u = _unit(power=True, mode="COOL")
    assert midea.unit_badge(u) == ("● COOL", "midea_teal")


def test_unit_badge_dry_and_auto():
    # DRY is padded to 4 so it aligns with the wider labels (COOL/????).
    assert midea.unit_badge(_unit(power=True, mode="DRY")) == ("● DRY ", "midea_teal")
    assert midea.unit_badge(_unit(power=True, mode="AUTO")) == ("● AUTO", "midea_teal")


def test_unit_badge_fan_only():
    u = _unit(power=True, mode="FAN_ONLY")
    assert midea.unit_badge(u) == ("● FAN ", "light_grey")


def test_unit_badge_off():
    u = _unit(power=False)
    label, color = midea.unit_badge(u)
    assert label == "● OFF "
    assert color == "light_grey"


def test_unit_badge_unreachable():
    u = _unit(online=False)
    label, color = midea.unit_badge(u)
    assert label == "● ????"
    assert color == "light_grey"


def test_unit_badge_labels_share_width():
    # Every badge must be the same display width so the name column that
    # follows it doesn't jitter as a unit changes mode/power/reachability.
    widths = {
        len(midea.unit_badge(u)[0])
        for u in (
            _unit(online=False),
            _unit(power=False),
            _unit(power=True, mode="FAN_ONLY"),
            _unit(power=True, mode="COOL"),
            _unit(power=True, mode="DRY"),
            _unit(power=True, mode="AUTO"),
        )
    }
    assert len(widths) == 1


# --- EditableField ---------------------------------------------------------------


def test_editable_field_defaults():
    f = midea.EditableField("power", "power_state", True, field_type="bool")
    assert f.field_type == "bool"
    assert f.value is True


# --- token cache -------------------------------------------------------------------


def test_token_cache_roundtrip(tmp_path):
    path = tmp_path / "midea_tokens.json"
    midea._save_token_cache({"1": {"token": "aa", "key": "bb"}}, path)
    assert midea._load_token_cache(path) == {"1": {"token": "aa", "key": "bb"}}


def test_token_cache_missing_file_is_empty(tmp_path):
    assert midea._load_token_cache(tmp_path / "missing.json") == {}


# --- mock controller/panel ---------------------------------------------------------


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("HOME_CONTROL_MOCK", "1")


def test_mock_fixture_shape(mock_env):
    ctl = midea.MideaController()
    ctl.poll(True)
    units = ctl.snapshot()
    assert len(units) == 3
    assert any(not u.online for u in units.values())
    assert any(u.mode == "FAN_ONLY" for u in units.values())
    # Every mock unit stands in for a device we've read, so all must be
    # contacted — otherwise they'd render as bare "connecting…" cards.
    assert all(u.contacted for u in units.values())


def test_system_collapsed_lines_one_row_per_unit(mock_env):
    s = midea.MideaSystem()
    s.poll(True)
    assert s.collapsed_height == 3
    lines = s.collapsed_lines(80)
    assert len(lines) == 3


def test_card_rows_powered_off_unit_fully_dimmed():
    s = midea.MideaSystem()
    rows = s._card_rows(_unit(power=False), is_selected=False, width=80)
    assert len(rows) == 3
    assert all(seg.dim for row in rows for seg in row)


def test_card_rows_powered_off_selected_keeps_cursor_accent():
    s = midea.MideaSystem()
    rows = s._card_rows(_unit(power=False), is_selected=True, width=80)
    cursor = rows[0][0]
    assert cursor.text == "▶ "
    assert cursor.color == s.color and cursor.bold and not cursor.dim
    assert all(seg.dim for seg in rows[0][1:])
    assert all(seg.dim for row in rows[1:] for seg in row)


def test_card_rows_powered_on_unit_not_dimmed():
    s = midea.MideaSystem()
    rows = s._card_rows(_unit(power=True, mode="COOL"), is_selected=False, width=80)
    assert any(not seg.dim for seg in rows[0])


def test_card_rows_uncontacted_unit_is_single_connecting_line():
    # A never-reached placeholder must not render fabricated control rows —
    # just one dimmed header line saying it's connecting.
    s = midea.MideaSystem()
    rows = s._card_rows(_unit(online=False, contacted=False), is_selected=False, width=80)
    assert len(rows) == 1
    text = "".join(seg.text for seg in rows[0])
    assert "connecting" in text and "Mode" not in text
    assert all(seg.dim for seg in rows[0])


def test_card_rows_contacted_but_offline_shows_last_known_card():
    # Once contacted, an offline unit keeps its full (dimmed) card and reads
    # "unreachable", since the values are real last-known state.
    s = midea.MideaSystem()
    rows = s._card_rows(_unit(online=False, contacted=True, power=True), is_selected=False, width=80)
    assert len(rows) == 3
    assert "unreachable" in "".join(seg.text for seg in rows[0])


def test_system_collapsed_lines_before_discovery():
    # No mock env: no units yet, single "Discovering..." line, not a crash.
    s = midea.MideaSystem()
    lines = s.collapsed_lines(80)
    assert len(lines) == 1


# --- pinned units: instant placeholders + cached-metadata connect ------------------


def _pinned_controller(monkeypatch, units, token_cache=None):
    monkeypatch.setattr(
        midea.config, "section", lambda name: {"units": units} if name == "midea" else {}
    )
    monkeypatch.setattr(midea.config, "get", lambda *a: a[2] if len(a) > 2 else None)
    monkeypatch.setattr(midea, "_load_token_cache", lambda *a: token_cache or {})
    monkeypatch.setattr(midea, "_save_token_cache", lambda *a: None)
    return midea.MideaController()


_CACHED_ENTRY = {
    "token": "aa", "key": "bb",
    "ip": "10.0.0.9", "port": 6444, "type": 172, "protocol": 2, "model": "m1",
}


def test_pinned_units_render_placeholders_before_first_poll(monkeypatch):
    ctl = _pinned_controller(monkeypatch, [{"ip": "10.0.0.9", "name": "Den"}])
    units = ctl.snapshot()
    assert len(units) == 1
    u = next(iter(units.values()))
    assert u.name == "Den" and u.ip == "10.0.0.9" and not u.online


def test_placeholder_unit_has_no_phantom_on_toggles():
    # A unit whose live state we haven't read yet must default every toggle
    # to off — otherwise its brief placeholder card renders e.g. "Displ ●"
    # before the real (usually off) value arrives. Regression: display_on
    # used to default True.
    u = midea.MideaUnit(id=-1000, ip="10.0.0.9", name="Den", online=False)
    assert not u.display_on and not u.power and not u.eco and not u.turbo


def test_pinned_discover_skips_probe_with_cached_metadata(monkeypatch):
    ctl = _pinned_controller(
        monkeypatch, [{"ip": "10.0.0.9", "name": "Den"}], {"123": dict(_CACHED_ENTRY)}
    )
    monkeypatch.setattr(
        midea, "midea_discover", lambda **kw: pytest.fail("discovery must not run")
    )
    raw = ctl._discover_raw()
    assert raw == {123: {
        "device_id": 123, "type": 172, "ip_address": "10.0.0.9", "port": 6444,
        "protocol": 2, "model": "m1", "_name": "Den", "_cached": True,
    }}


def test_pinned_discover_probes_when_metadata_missing(monkeypatch):
    # Token-only cache entry (pre-metadata format): must fall back to the probe.
    ctl = _pinned_controller(
        monkeypatch, [{"ip": "10.0.0.9"}], {"123": {"token": "aa", "key": "bb"}}
    )
    probed: list[str] = []
    monkeypatch.setattr(
        midea, "midea_discover", lambda **kw: probed.append(kw["ip_address"]) or {}
    )
    assert ctl._discover_raw() == {}
    assert probed == ["10.0.0.9"]


def test_stale_cached_metadata_dropped_on_connect_failure(monkeypatch):
    ctl = _pinned_controller(
        monkeypatch, [{"ip": "10.0.0.9", "name": "Den"}], {"123": dict(_CACHED_ENTRY)}
    )
    monkeypatch.setattr(ctl, "_try_connect", lambda *a: None)
    ctl._discover_all()
    # Metadata gone (next pass re-probes the IP), token/key kept.
    assert ctl._token_cache["123"] == {"token": "aa", "key": "bb"}
    # The init-seeded synthetic placeholder was replaced by the real-id one.
    assert set(ctl.snapshot()) == {123}


def test_hotkey_cycles_mode(mock_env):
    s = midea.MideaSystem()
    s.poll(True)
    online = s._online_units()
    u = online[s.selected]
    before = u.mode
    assert s.handle_key(ord("m")) is True
    after = s.ctl.snapshot()[u.id].mode
    assert after != before
    assert after in u.supported_modes


def test_hotkey_toggles_eco(mock_env):
    s = midea.MideaSystem()
    s.poll(True)
    u = s._online_units()[s.selected]
    before = u.eco
    assert s.handle_key(ord("e")) is True
    assert s.ctl.snapshot()[u.id].eco != before


def test_hotkey_toggles_swing(mock_env):
    s = midea.MideaSystem()
    s.poll(True)
    u = s._online_units()[s.selected]
    was_off = u.swing_mode == "OFF"
    assert s.handle_key(ord("s")) is True
    after = s.ctl.snapshot()[u.id].swing_mode
    assert (after == "OFF") != was_off


def test_updown_selects_only_online_units(mock_env):
    s = midea.MideaSystem()
    s.poll(True)
    online_ids = {u.id for u in s._online_units()}
    assert len(online_ids) >= 2  # Living Room + Bedroom are both online in the mock fixture
    seen = set()
    s.selected = 0
    for _ in range(len(online_ids)):
        seen.add(s._online_units()[s.selected].id)
        s.handle_key(curses.KEY_DOWN)
    assert seen == online_ids  # never lands on the offline Office unit


def test_digit_key_starts_numeric_temp_entry_and_commits(mock_env):
    s = midea.MideaSystem()
    s.poll(True)
    u = s._online_units()[s.selected]
    assert s.handle_key(ord("7")) is True  # any digit starts entry, no ENTER needed first
    assert s._num_buf == "7"
    assert s.handle_key(ord("0")) is True
    assert s._num_buf == "70"
    assert s.handle_key(ord("\n")) is True  # commit
    assert s._num_buf is None
    after = s.ctl.snapshot()[u.id]
    expected = 70 if after.fahrenheit else round(midea._f_to_c(70))
    assert round(midea._c_to_f(after.target_temp_c) if after.fahrenheit else after.target_temp_c) == expected


# --- backend mapping tables (midea-local int<->string encodings) -----------------


class _FakeDevice:
    """Duck-types the subset of MideaACDevice that _unit_from_device reads."""

    def __init__(self, attributes: dict, capabilities: dict, device_id=1, name="LR", available=True):
        self.attributes = attributes
        self.capabilities = capabilities
        self.device_id = device_id
        self.name = name
        self.available = available


def test_unit_from_device_maps_mode_fan_swing():
    dev = _FakeDevice(
        attributes={
            "power": True, "mode": 2, "fan_speed": 60,
            "swing_vertical": True, "swing_horizontal": False,
            "target_temperature": 24.5, "temp_fahrenheit": True,
            "min_temperature": 16.0, "max_temperature": 30.0,
        },
        capabilities={
            "cool_mode": True, "dry_mode": True, "auto_mode": True, "heat_mode": False,
            "swing_vertical": True, "swing_horizontal": False,
            "fan_custom": True, "eco": True, "turbo_cool": True, "display_control": True,
        },
    )
    u = midea._unit_from_device(dev, "192.168.1.50")
    assert u.mode == "COOL"
    assert u.fan_speed == "MEDIUM"
    assert u.swing_mode == "VERTICAL"
    assert u.supported_modes == ("AUTO", "COOL", "DRY", "FAN_ONLY")
    assert u.supported_fan_speeds == ("AUTO", "SILENT", "LOW", "MEDIUM", "HIGH", "MAX")
    assert u.supported_swing_modes == ("OFF", "VERTICAL")
    assert u.supports_eco is True
    assert u.supports_turbo is True
    assert u.supports_display_control is True


def test_unit_from_device_offline_and_off():
    dev = _FakeDevice(
        attributes={"power": False, "mode": 0, "fan_speed": 0},
        capabilities={},
        available=False,
    )
    u = midea._unit_from_device(dev, "192.168.1.52")
    assert u.online is False
    assert u.power is False
    assert u.swing_mode == "OFF"


class _FakeConnected:
    """Stands in for a freshly connected midealocal device."""

    device_id = 123
    name = "Den"
    available = True
    daemon = False
    attributes: dict = {}
    capabilities = {"cool_mode": True, "auto_mode": True, "eco": True}
    _unsupported_protocol = ["MessageQueryAppliance"]

    def open(self):
        pass


def test_protocol_probe_result_is_never_cached(monkeypatch):
    # midealocal learns _unsupported_protocol by timing out a query, so it
    # holds false negatives whenever a unit was merely slow or busy. Caching
    # it made one bad pass permanent — every card collapsed to Fan/Auto once
    # the capabilities query landed in that list. Only positive discovery
    # metadata may be persisted.
    ctl = _pinned_controller(monkeypatch, [{"ip": "10.0.0.9", "name": "Den"}],
                             {"123": dict(_CACHED_ENTRY)})
    monkeypatch.setattr(ctl, "_try_connect", lambda *a: _FakeConnected())
    ctl._discover_all()
    assert "unsupported" not in ctl._token_cache["123"]
    assert ctl._token_cache["123"]["port"] == 6444  # metadata still cached


def test_cached_metadata_carries_no_probe_seed(monkeypatch):
    ctl = _pinned_controller(
        monkeypatch,
        [{"ip": "10.0.0.9", "name": "Den"}],
        {"123": {**_CACHED_ENTRY, "unsupported": ["MessageCapabilitiesQuery"]}},
    )
    assert "_unsupported" not in ctl._discover_raw()[123]
