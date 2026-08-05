"""Headless tests for the Midea AC panel (no curses init, no real network/asyncio)."""

import curses

import pytest

from home_control import ui
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
    # Represents a real unit we've read at least once, so contacted=True and
    # caps_known=True by default; pass contacted=False to model a
    # never-reached placeholder, or caps_known=False for one whose
    # capabilities reply hasn't landed yet.
    base: dict[str, object] = dict(id=1, ip="192.168.1.50", name="LR", online=True,
                                   contacted=True, caps_known=True)
    base.update(kw)
    return midea.MideaUnit(**base)  # type: ignore[arg-type]


def test_unit_badge_cooling():
    u = _unit(power=True, mode="COOL")
    assert midea.unit_badge(u) == ("● COOL", ui.BADGE_ACTIVE)


def test_unit_badge_dry_and_auto():
    # DRY is padded to 4 so it aligns with the wider labels (COOL/????).
    assert midea.unit_badge(_unit(power=True, mode="DRY")) == ("● DRY ", ui.BADGE_ACTIVE)
    assert midea.unit_badge(_unit(power=True, mode="AUTO")) == ("● AUTO", ui.BADGE_ACTIVE)


def test_unit_badge_fan_only():
    u = _unit(power=True, mode="FAN_ONLY")
    assert midea.unit_badge(u) == ("● FAN ", ui.BADGE_IDLE)


def test_unit_badge_off():
    u = _unit(power=False)
    label, state = midea.unit_badge(u)
    assert label == "● OFF "
    assert state == ui.BADGE_IDLE


def test_unit_badge_unreachable():
    u = _unit(online=False)
    # An unreachable *unit* is idle, not a fault — see unit_badge()'s docstring.
    label, state = midea.unit_badge(u)
    assert label == "● ????"
    assert state == ui.BADGE_IDLE


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
    # contacted *and* have capabilities — otherwise they'd render as bare
    # "connecting…" cards, or (missing caps) collapse to a lone header row,
    # leaving the mock TUI a weaker surface than the panel it stands in for.
    assert all(u.contacted for u in units.values())
    assert all(u.caps_known for u in units.values())


def test_mock_units_render_full_cards(mock_env):
    s = midea.MideaSystem()
    s.poll(True)
    for u in s._units():
        assert len(s._card_rows(u, is_selected=False, width=76)) == 3


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


def test_header_shows_outside_temp_with_the_other_temperatures():
    s = midea.MideaSystem()
    u = _unit(power=True, mode="COOL", indoor_temp_c=24.0, outdoor_temp_c=23.0, target_temp_c=24.0)
    text = "".join(seg.text for seg in s._card_rows(u, is_selected=False, width=76)[0])
    assert "Unit ext. 73°F" in text
    assert text.index("Unit ext.") < text.index("→")   # ahead of indoor → target


def test_header_omits_outside_temp_when_the_unit_never_reports_it():
    s = midea.MideaSystem()
    u = _unit(power=True, mode="COOL", indoor_temp_c=24.0, outdoor_temp_c=None)
    text = "".join(seg.text for seg in s._card_rows(u, is_selected=False, width=76)[0])
    assert "Unit ext." not in text and "—" not in text


def test_header_fits_with_every_optional_field_present():
    # filter alert + error code + outside temp is the widest the header gets;
    # it has to survive at the 80-column terminal the project designs for.
    s = midea.MideaSystem()
    u = _unit(power=True, mode="COOL", name="Living Room", filter_alert=True, error_code=5,
              indoor_temp_c=24.0, outdoor_temp_c=23.0, target_temp_c=24.0)
    text = "".join(seg.text for seg in s._card_rows(u, is_selected=True, width=76)[0])
    assert len(text) <= 76
    assert "filter!" in text and "err 5" in text and "Unit ext." in text


def test_card_rows_uncontacted_unit_is_single_connecting_line():
    # A never-reached placeholder must not render fabricated control rows —
    # just one dimmed header line saying it's connecting.
    s = midea.MideaSystem()
    rows = s._card_rows(_unit(online=False, contacted=False), is_selected=False, width=80)
    assert len(rows) == 1
    text = "".join(seg.text for seg in rows[0])
    assert "connecting" in text and "Mode" not in text
    assert all(seg.dim for seg in rows[0])


def test_card_rows_status_without_capabilities_is_header_only():
    # The status reply and the capabilities reply are separate packets. In
    # between, the supported_* lists are placeholder defaults — rendering them
    # produced the "Mode Fan / Fan Auto" collapsed card. Show the header,
    # which is entirely real, and wait for the rest.
    s = midea.MideaSystem()
    u = _unit(power=True, mode="COOL", caps_known=False, indoor_temp_c=24.0)
    rows = s._card_rows(u, is_selected=False, width=80)
    assert len(rows) == 1
    text = "".join(seg.text for seg in rows[0])
    assert "COOL" in text and "75°F" in text     # real status is shown
    assert "Mode" not in text and "Fan" not in text


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


class _FakeConnected:
    """Stands in for a freshly connected midealocal device."""

    device_id = 123
    name = "Den"
    available = True
    daemon = False
    attributes: dict = {}
    capabilities = {"cool_mode": True, "auto_mode": True, "eco": True}
    _unsupported_protocol = ["MessageQueryAppliance"]
    _message_protocol_version = 3
    refresh_interval = 30

    _attributes: dict = {}

    def open(self):
        pass

    def refresh_status(self, check_protocol=False):
        pass

    def set_refresh_interval(self, interval):
        self.refresh_interval = interval

    def build_send(self, message, query=False):
        self.sent = [*getattr(self, "sent", []), type(message).__name__]

    def process_message(self, msg):
        return {}


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


# --- setpoint encoding ------------------------------------------------------


def _wire_roundtrip(celsius: float) -> float:
    """What the unit ends up storing, per midealocal MessageGeneralSet._body
    (int() for the whole degrees, round(t*2) for the half-degree bit) and the
    matching decode."""
    byte = (int(celsius) & 0xF) | (0x10 if round(celsius * 2) % 2 != 0 else 0)
    return 16 + (byte & 0xF) + (0.5 if byte & 0x10 else 0.0)


def test_quantize_snaps_to_half_degrees():
    assert midea._quantize_c(22.7778) == 23.0
    assert midea._quantize_c(21.6667) == 21.5
    assert midea._quantize_c(22.0) == 22.0


def test_every_fahrenheit_setpoint_survives_the_wire(monkeypatch):
    # Regression: stepping 72°F -> 73°F sent 22.778°C, which encoded to a flat
    # 22.0°C — the setpoint never moved and the right arrow looked dead. Each
    # whole °F must land on its own half-degree and read back as itself.
    ctl = _pinned_controller(monkeypatch, [])
    ctl._units[1] = midea.MideaUnit(id=1, ip="10.0.0.9", name="LR", online=True,
                                    contacted=True, caps_known=True, fahrenheit=True)
    for f in range(61, 87):
        sent = ctl.target_c_for(1, f)
        stored = _wire_roundtrip(sent)
        assert stored == sent, f"{f}°F sent {sent}°C but the unit stores {stored}°C"
        assert round(midea._c_to_f(stored)) == f, f"{f}°F reads back as {midea._c_to_f(stored)}°F"


def test_celsius_setpoint_is_quantized_too(monkeypatch):
    ctl = _pinned_controller(monkeypatch, [])
    ctl._units[1] = midea.MideaUnit(id=1, ip="10.0.0.9", name="LR", online=True,
                                    contacted=True, caps_known=True, fahrenheit=False)
    assert ctl.target_c_for(1, 23) == 23.0


def test_edit_mirrors_locally_without_waiting(mock_env):
    # apply_edit runs on the main thread from handle_key: it must reflect the
    # change straight away rather than sleeping on the network.
    s = midea.MideaSystem()
    s.poll(True)
    before = sorted(s.ctl.snapshot().values(), key=lambda u: u.name.lower())
    unit = next(u for u in before if u.online and u.power)
    s.selected = [u.id for u in before if u.online].index(unit.id)
    s.handle_key(curses.KEY_RIGHT)
    after = s.ctl.snapshot()[unit.id]
    assert after.target_temp_c > unit.target_temp_c


def test_pinned_card_never_falls_back_to_discovering(monkeypatch):
    # The card is published only once a status reply lands, but the synthetic
    # placeholder is dropped as soon as the device connects. Without a
    # stand-in keyed by the real device id, the panel has zero units for that
    # window and renders "Discovering..." — after already showing the unit.
    ctl = _pinned_controller(monkeypatch, [{"ip": "10.0.0.9", "name": "Den"}],
                             {"123": dict(_CACHED_ENTRY)})

    class _Connecting(_FakeConnected):
        attributes = {"mode": 0}          # connected, no status reply yet

    monkeypatch.setattr(ctl, "_try_connect", lambda *a: _Connecting())
    monkeypatch.setattr(ctl, "_fill_in", lambda: None)
    assert ctl.snapshot(), "pinned unit must render before the first poll"
    ctl.poll(focused=True)
    units = ctl.snapshot()
    assert len(units) == 1, "a connecting unit must still occupy a card"
    u = next(iter(units.values()))
    assert u.name == "Den" and not u.contacted   # renders "connecting...", not "Discovering..."


def test_poll_cadence_speeds_up_until_the_whole_picture_lands(monkeypatch):
    # Midea renders last in the panel order, so it starts unfocused at a 5s
    # cadence. A unit's picture arrives in three waves — status, capabilities,
    # then the follow-up burst — and each is invisible until a poll tick reads
    # it. Gating on status alone left the Mode/Fan rows blank for a full tick
    # after the header appeared.
    ctl = _pinned_controller(monkeypatch, [{"ip": "10.0.0.9", "name": "Den"}],
                             {"123": dict(_CACHED_ENTRY)})
    s = midea.MideaSystem()
    s.ctl = ctl
    assert s.poll_interval_idle == 5.0          # nothing connected yet

    class _Connecting(_FakeConnected):
        attributes = {"mode": 0}                # connected, no status reply yet
        capabilities: dict = {}

    dev = _Connecting()
    monkeypatch.setattr(ctl, "_try_connect", lambda *a: dev)
    ctl.poll(focused=False)
    assert ctl.settling and s.poll_interval_idle == midea.SETTLE_POLL_INTERVAL

    dev.attributes = {"mode": 2}                # wave 1: status
    assert ctl.settling, "capabilities still outstanding"
    assert s.poll_interval_idle == midea.SETTLE_POLL_INTERVAL

    ctl.poll(focused=False)                     # _fill_in fires now status is in
    dev.capabilities = {"cool_mode": True}      # wave 2: capabilities
    assert ctl.settling, "follow-up burst still has FILL_GRACE to land"

    ctl._filled[123] -= midea.FILL_GRACE        # wave 3: grace elapsed
    assert not ctl.settling
    assert s.poll_interval_idle == 5.0
    assert s.poll_interval_focused == 1.0


def test_settle_window_caps_the_fast_cadence(monkeypatch):
    # A unit that never answers must not hold the fast cadence forever.
    ctl = _pinned_controller(monkeypatch, [{"ip": "10.0.0.9", "name": "Den"}],
                             {"123": dict(_CACHED_ENTRY)})

    class _Silent(_FakeConnected):
        attributes = {"mode": 0}

    monkeypatch.setattr(ctl, "_try_connect", lambda *a: _Silent())
    ctl.poll(focused=False)
    assert ctl.settling
    ctl._settle_deadline -= midea.SETTLE_WINDOW + 1     # pretend the window elapsed
    assert not ctl.settling


def test_library_refresh_is_disabled_at_connect(monkeypatch):
    # midealocal's own 30s refresh sends all eight queries in build_query();
    # only MessageQuery's reply carries anything that changes. _query_status
    # takes the recurring job over, so the device thread's timer is switched
    # off — zero, because _check_refresh gates on `0 < interval`.
    ctl = _pinned_controller(monkeypatch, [{"ip": "10.0.0.9", "name": "Den"}],
                             {"123": dict(_CACHED_ENTRY)})
    dev = _FakeConnected()
    monkeypatch.setattr(ctl, "_try_connect", lambda *a: dev)
    ctl._discover_all()
    assert dev.refresh_interval == 0


def test_status_query_respects_its_own_cadence(monkeypatch):
    ctl = _pinned_controller(monkeypatch, [{"ip": "10.0.0.9", "name": "Den"}],
                             {"123": dict(_CACHED_ENTRY)})
    dev = _FakeConnected()
    monkeypatch.setattr(ctl, "_try_connect", lambda *a: dev)
    ctl._discover_all()

    dev.sent = []
    ctl._query_status(focused=True)
    assert dev.sent == []                      # connect just asked; too soon to repeat
    ctl._last_query[123] -= midea.STATUS_QUERY_INTERVAL_FOCUSED + 1
    ctl._query_status(focused=True)
    assert dev.sent == ["MessageQuery"]        # capabilities already known: status only


def test_status_query_retries_capabilities_until_they_land(monkeypatch):
    # Without the 30s refresh there is no other retry: a dropped B5 would
    # never be re-asked and the card would render from dataclass defaults for
    # the life of the connection.
    ctl = _pinned_controller(monkeypatch, [{"ip": "10.0.0.9", "name": "Den"}],
                             {"123": dict(_CACHED_ENTRY)})

    class _NoCaps(_FakeConnected):
        capabilities: dict = {}

    dev = _NoCaps()
    monkeypatch.setattr(ctl, "_try_connect", lambda *a: dev)
    ctl._discover_all()

    dev.sent = []
    ctl._last_query[123] -= midea.STATUS_QUERY_INTERVAL_IDLE + 1
    ctl._query_status(focused=False)
    assert dev.sent == ["MessageQuery", "MessageCapabilitiesQuery"]

    dev.capabilities = {"cool_mode": True}     # they land
    dev.sent = []
    ctl._last_query[123] -= midea.STATUS_QUERY_INTERVAL_IDLE + 1
    ctl._query_status(focused=False)
    assert dev.sent == ["MessageQuery"]        # and are never asked for again


def test_device_info_reports_attributes_and_capabilities(monkeypatch):
    ctl = _pinned_controller(monkeypatch, [{"ip": "10.0.0.9", "name": "Den"}],
                             {"123": dict(_CACHED_ENTRY)})

    class _Live(_FakeConnected):
        attributes = {"mode": 2, "power": True, "indoor_humidity": None}

    dev = _Live()
    monkeypatch.setattr(ctl, "_try_connect", lambda *a: dev)
    ctl._discover_all()
    ctl._refresh_snapshot()

    assert ctl.device_info() is None           # closed until asked for
    ctl.request_device_info(123)
    info = ctl.device_info()
    assert info is not None
    title, lines = info
    assert "device info" in title
    body = "\n".join(lines)
    assert "indoor_humidity" in body and "—" in body   # None renders, not hidden
    assert "eco" in body and "cool_mode" in body       # capability flags listed
    ctl.close_device_info()
    assert ctl.device_info() is None
# --- A0 push padding (see _STATUS_ONLY_ATTRS) --------------------------------

# Real frames off a unit whose display was physically off and staying off, and
# whose filter had just been reset. The status reply reports byte 14 as 0x70
# ("display off"); the push pads the same byte with 0x00, which reads as
# "display on". Captured 2026-08-02, ~/midea-samples/.
_STATUS_FRAME = bytes.fromhex(
    "aa23ac00000000000303c00148667f7f00300010046369007000000000000000000388b3"
)
_PUSH_FRAME = bytes.fromhex(
    "aa22ac00000000000305a01940660000003000900007000000000000000000001e588e"
)


def _replay_device(patched: bool):
    from midealocal.const import ProtocolVersion
    from midealocal.devices.ac import MideaACDevice

    dev = MideaACDevice(
        name="replay", device_id=1, ip_address="0.0.0.0", port=6444,
        token="", key="", device_protocol=ProtocolVersion.V3,
        model="", subtype=0, customize="",
    )
    if patched:
        midea._ignore_push_padding(dev)
    return dev


def test_push_frame_clobbers_display_without_the_shim():
    # Documents the upstream behaviour the shim exists for: if this ever stops
    # failing, midealocal fixed its A0 offsets and the shim can go.
    dev = _replay_device(patched=False)
    dev.process_message(_STATUS_FRAME)
    assert dev.attributes["screen_display"] is False
    dev.process_message(_PUSH_FRAME)
    assert dev.attributes["screen_display"] is True, "push overwrote the status reply"


def test_push_frame_leaves_status_only_attrs_alone():
    dev = _replay_device(patched=True)
    dev.process_message(_STATUS_FRAME)
    assert dev.attributes["screen_display"] is False
    dev.process_message(_PUSH_FRAME)
    assert dev.attributes["screen_display"] is False
    assert dev.attributes["full_dust"] is False


def test_status_reply_still_owns_status_only_attrs():
    # The shim must not latch: a stale value has to be correctable by the very
    # next status reply, or a filter reset would never clear the badge.
    dev = _replay_device(patched=True)
    dev._attributes["screen_display"] = True
    dev._attributes["full_dust"] = True
    dev.process_message(_STATUS_FRAME)
    assert dev.attributes["screen_display"] is False
    assert dev.attributes["full_dust"] is False


def test_push_frame_still_carries_the_fields_it_shares():
    # Bytes 0-9 do agree between the two bodies, so a push must keep updating
    # power/mode/setpoint — the shim is narrow, not a blanket ignore.
    dev = _replay_device(patched=True)
    dev.process_message(_PUSH_FRAME)
    assert dev.attributes["power"] is True
    assert dev.attributes["mode"] == 2
    assert dev.attributes["target_temperature"] == 24.0


def test_shim_reports_only_the_keys_it_kept():
    dev = _replay_device(patched=True)
    dev.process_message(_STATUS_FRAME)
    status = dev.process_message(_PUSH_FRAME)
    assert not set(status) & set(midea._STATUS_ONLY_ATTRS)
