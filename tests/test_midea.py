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
    base: dict[str, object] = dict(id=1, ip="192.168.1.50", name="LR", online=True)
    base.update(kw)
    return midea.MideaUnit(**base)  # type: ignore[arg-type]


def test_unit_badge_cooling():
    u = _unit(power=True, mode="COOL")
    assert midea.unit_badge(u) == ("● COOL", "midea_teal")


def test_unit_badge_dry_and_auto():
    assert midea.unit_badge(_unit(power=True, mode="DRY")) == ("● DRY", "midea_teal")
    assert midea.unit_badge(_unit(power=True, mode="AUTO")) == ("● AUTO", "midea_teal")


def test_unit_badge_fan_only():
    u = _unit(power=True, mode="FAN_ONLY")
    assert midea.unit_badge(u) == ("● FAN", "light_grey")


def test_unit_badge_off():
    u = _unit(power=False)
    label, color = midea.unit_badge(u)
    assert label == "●"
    assert color == "light_grey"


def test_unit_badge_unreachable():
    u = _unit(online=False)
    label, color = midea.unit_badge(u)
    assert label == "● ????"
    assert color == "light_grey"


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


def test_system_collapsed_lines_one_row_per_unit(mock_env):
    s = midea.MideaSystem()
    s.poll(True)
    assert s.collapsed_height == 3
    lines = s.collapsed_lines(80)
    assert len(lines) == 3


def test_system_collapsed_lines_before_discovery():
    # No mock env: no units yet, single "Discovering..." line, not a crash.
    s = midea.MideaSystem()
    lines = s.collapsed_lines(80)
    assert len(lines) == 1


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
    assert u.supported_fan_speeds == ("SILENT", "LOW", "MEDIUM", "HIGH", "MAX", "AUTO")
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
