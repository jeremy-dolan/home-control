"""Midea AC panel (midea-local, the extracted core of the Home Assistant
midea_ac_lan integration).

Split into:
  * MideaController — thin sync wrapper. Unlike every other backend this app
    used to bridge to (msmart-ng was fully asyncio), midea-local's device
    objects are plain ``threading.Thread`` subclasses: each connected unit
    runs its own persistent background thread doing heartbeats/refreshes and
    parsing pushed state updates, and ``dev.attributes`` is a live-updated
    dict you can read with zero network I/O. Only the one-time cloud login
    (V3 token/key pairing) is ``async def`` — done via a single blocking
    ``asyncio.run(...)`` call, no dedicated event-loop thread needed.
  * MideaSystem — the panel: every unit (online or not) is always fully
    expanded as a 3-line card. ↕ picks which *online* unit hotkeys act on;
    p/m/f/s/e/t/d directly toggle/cycle that unit's fields; ←→ nudges its
    target temperature. No Hue-style drill-in dialog — there are only ever a
    handful of AC units, so everything fits on screen at once.

Set HOME_CONTROL_MOCK=1 to render 3 fixture units with no network at all.

V3 devices need a per-device token+key, fetched from Midea's cloud on first
pairing (then cached locally, see TOKEN_CACHE_PATH, so the cloud is only
touched once per device). Midea's app ecosystem has several incompatible
cloud backends behind the same physical protocol — "NetHome Plus" (the
default; msmart-ng's old built-in shared demo account lived here) and
"SmartHome"/MSmartHome (what Midea is migrating users to) are the two
relevant ones; set ``[midea] cloud = "smarthome"`` in config to use the
latter. This library's SmartHomeCloud implementation is the reason for the
switch away from msmart-ng: msmart-ng's SmartHome ``get_token`` call has been
broken since early 2025 (upstream issue #201, never fixed) because it omits
an ``applianceCodes`` field the SmartHome endpoint silently requires;
midea-local's cloud.py includes it and has been verified live against a real
account/unit.
"""

from __future__ import annotations

import asyncio
import curses
import dataclasses
import json
import os
import textwrap
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from aiohttp import ClientSession
from midealocal.cloud import get_midea_cloud
from midealocal.const import DeviceType
from midealocal.devices import device_selector
from midealocal.devices.ac import MideaACDevice
from midealocal.discover import discover as midea_discover

from .. import config
from ..ui import Line, Region, Seg, hint, hint_row, justify
from .base import System, VoiceAction

# Minimum gap between discovery attempts. A failure here can be a cloud-side
# auth rate limit — retrying every poll tick (as fast as 1s when focused)
# would hammer that endpoint, unlike Roku's cheap local-only SSDP retry.
DISCOVERY_RETRY_INTERVAL = 60
# Fixed settle delay after sending a command before re-reading device state
# for the mirrored cache. midea-local's set_attribute()/set_target_temperature()
# are fire-and-forget at the protocol layer — the actual attribute update
# lands asynchronously once the unit's persistent background thread parses
# its ack, which is sub-second on a LAN.
EDIT_SETTLE_DELAY = 0.3

TOKEN_CACHE_PATH = Path(
    os.environ.get("HOME_CONTROL_MIDEA_CACHE")
    or (Path.home() / ".cache" / "home-control" / "midea_tokens.json")
)

_CLOUD_NAMES = {"nethome_plus": "NetHome Plus", "smarthome": "SmartHome", "meiju": "美的美居"}
_MODE_TO_INT = {"AUTO": 1, "COOL": 2, "DRY": 3, "HEAT": 4, "FAN_ONLY": 5}
_INT_TO_MODE = {v: k for k, v in _MODE_TO_INT.items()}
_FAN_TO_INT = {"SILENT": 20, "LOW": 40, "MEDIUM": 60, "HIGH": 80, "MAX": 100, "AUTO": 102}
_INT_TO_FAN = {v: k for k, v in _FAN_TO_INT.items()}
_SWING_TO_BOOLS = {"OFF": (False, False), "VERTICAL": (True, False), "HORIZONTAL": (False, True), "BOTH": (True, True)}
_BOOLS_TO_SWING = {v: k for k, v in _SWING_TO_BOOLS.items()}

_MODE_LABEL = {"COOL": "COOL", "DRY": "DRY", "AUTO": "AUTO", "FAN_ONLY": "FAN"}
# api_key -> MideaUnit field name, for mock mode's apply_edit (see below) —
# identical for every field except target_temperature, which needs its own
# °F/°C conversion and so is handled separately.
_API_KEY_TO_UNIT_FIELD = {
    "power_state": "power", "operational_mode": "mode", "fan_speed": "fan_speed",
    "swing_mode": "swing_mode", "eco": "eco", "turbo": "turbo", "display_on": "display_on",
}
_FIELD_INDENT = "    "
_STATUS_WRAP_LINES = 4  # max wrapped lines for a "no units"/error message


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MideaUnit:
    id: int
    ip: str
    name: str
    online: bool
    power: bool = False
    mode: str = "COOL"            # AUTO | COOL | DRY | FAN_ONLY (no HEAT on this model)
    fan_speed: str = "AUTO"       # SILENT | LOW | MEDIUM | HIGH | AUTO | MAX
    swing_mode: str = "OFF"       # OFF | VERTICAL
    target_temp_c: float = 24.0
    indoor_temp_c: float | None = None
    outdoor_temp_c: float | None = None
    fahrenheit: bool = True       # this unit's own display-unit preference
    eco: bool = False
    turbo: bool = False
    display_on: bool = True
    filter_alert: bool = False
    error_code: int = 0
    min_temp_c: float = 16.0
    max_temp_c: float = 30.0
    supported_modes: tuple[str, ...] = ("AUTO", "COOL", "DRY", "FAN_ONLY")
    supported_fan_speeds: tuple[str, ...] = ("AUTO", "SILENT", "LOW", "MEDIUM", "HIGH", "MAX")
    supported_swing_modes: tuple[str, ...] = ("OFF", "VERTICAL")
    supports_eco: bool = True
    supports_turbo: bool = True
    supports_display_control: bool = True


@dataclass
class EditableField:
    """Transport for one field edit, passed to MideaController.apply_edit:
    which attribute to change, its new value, and enough type info
    (``field_type``) for the dispatch there."""
    name: str
    api_key: str
    value: Any
    field_type: str = "info"  # "bool" | "int" | "enum"


def unit_badge(u: MideaUnit) -> tuple[str, str]:
    """(badge text, color) for a unit's status dot: colored accent + mode word
    when actively conditioning, grey "FAN" for fan-only, grey "OFF" for
    powered off, "????" for unreachable. Deliberately not Router's
    red-for-offline convention — off/unreachable read as calm grey here,
    not alarming."""
    if not u.online:
        return "● ????", "light_grey"
    if not u.power:
        return "● OFF", "light_grey"
    if u.mode == "FAN_ONLY":
        return "● FAN", "light_grey"
    return f"● {_MODE_LABEL.get(u.mode, u.mode)}", "midea_teal"


def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def _f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def _fmt_temp(c: float | None, fahrenheit: bool) -> str:
    if c is None:
        return "—"
    return f"{round(_c_to_f(c)) if fahrenheit else round(c)}°{'F' if fahrenheit else 'C'}"


# ---------------------------------------------------------------------------
# Token/key cache — best-effort, never fatal (a cache miss just re-pairs).
# ---------------------------------------------------------------------------


def _load_token_cache(path: Path = TOKEN_CACHE_PATH) -> dict[str, dict[str, str]]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_token_cache(cache: dict[str, dict[str, str]], path: Path = TOKEN_CACHE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache))
    except OSError:
        pass


def _unit_from_device(dev: MideaACDevice, ip: str) -> MideaUnit:
    a = dev.attributes
    caps = dev.capabilities or {}
    modes = [
        name
        for name, present in (
            ("AUTO", caps.get("auto_mode")),
            ("COOL", caps.get("cool_mode")),
            ("DRY", caps.get("dry_mode")),
            ("HEAT", caps.get("heat_mode")),
        )
        if present
    ]
    modes.append("FAN_ONLY")  # always available; not gated by a capability flag
    if caps.get("fan_custom"):
        fan_speeds = ["AUTO", "SILENT", "LOW", "MEDIUM", "HIGH", "MAX"]
    else:
        fan_speeds = ["AUTO", *(
            name
            for name, present in (
                ("SILENT", caps.get("fan_silent")),
                ("LOW", caps.get("fan_low")),
                ("MEDIUM", caps.get("fan_medium")),
                ("HIGH", caps.get("fan_high")),
            )
            if present
        )]
    swings = ["OFF"]
    if caps.get("swing_vertical"):
        swings.append("VERTICAL")
    if caps.get("swing_horizontal"):
        swings.append("HORIZONTAL")
    if caps.get("swing_vertical") and caps.get("swing_horizontal"):
        swings.append("BOTH")
    vertical, horizontal = bool(a.get("swing_vertical")), bool(a.get("swing_horizontal"))
    return MideaUnit(
        id=dev.device_id, ip=ip, name=dev.name or f"AC {dev.device_id}",
        online=dev.available,
        power=bool(a.get("power")),
        mode=_INT_TO_MODE.get(int(a.get("mode") or 0), "COOL"),
        fan_speed=_INT_TO_FAN.get(int(a.get("fan_speed") or 0), "AUTO"),
        swing_mode=_BOOLS_TO_SWING.get((vertical, horizontal), "OFF"),
        target_temp_c=float(a.get("target_temperature") or 24.0),
        indoor_temp_c=a.get("indoor_temperature"),
        outdoor_temp_c=a.get("outdoor_temperature"),
        fahrenheit=bool(a.get("temp_fahrenheit")),
        eco=bool(a.get("eco_mode")), turbo=bool(a.get("boost_mode")), display_on=bool(a.get("screen_display")),
        filter_alert=bool(a.get("full_dust")), error_code=int(a.get("error_code") or 0),
        min_temp_c=float(a.get("min_temperature") or 16.0), max_temp_c=float(a.get("max_temperature") or 30.0),
        supported_modes=tuple(dict.fromkeys(modes)),
        supported_fan_speeds=tuple(dict.fromkeys(fan_speeds)),
        supported_swing_modes=tuple(swings),
        supports_eco=bool(caps.get("eco")),
        supports_turbo=bool(caps.get("turbo_cool") or caps.get("turbo_heat")),
        supports_display_control=bool(caps.get("display_control")),
    )


# ---------------------------------------------------------------------------
# Controller (no curses)
# ---------------------------------------------------------------------------


class MideaController:
    def __init__(self) -> None:
        self._pinned: list[dict[str, str]] = config.section("midea").get("units") or []
        self._account: str | None = config.get("midea", "account")
        self._password: str | None = config.get("midea", "password")
        self._cloud_kind = (config.get("midea", "cloud", "nethome_plus") or "nethome_plus").lower()
        self._lock = threading.Lock()
        self._units: dict[int, MideaUnit] = {}
        self._devices: dict[int, MideaACDevice] = {}
        self._ips: dict[int, str] = {}
        self._token_cache = _load_token_cache()
        self.error = ""
        self.mock = os.environ.get("HOME_CONTROL_MOCK") == "1"
        self._attempted = False           # has a real discovery pass ever run
        self._last_attempt_t = 0.0
        self._last_seen_count = 0         # raw devices seen in the last pass
        self._last_connect_error = ""     # most recent per-device connect/auth failure

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        pass

    def stop(self) -> None:
        for dev in list(self._devices.values()):
            try:
                dev.close()
            except Exception:
                pass

    # -- cloud pairing (V3 token/key fetch) -------------------------------
    def _cloud_name(self) -> str:
        return _CLOUD_NAMES.get(self._cloud_kind, "NetHome Plus")

    async def _fetch_keys(self, device_ids: list[int]) -> dict[int, dict[int, dict[str, str]]]:
        async with ClientSession() as session:
            cloud = get_midea_cloud(self._cloud_name(), session, self._account or "", self._password or "")
            if not await cloud.login():
                raise RuntimeError("Failed to login to cloud")
            return {did: await cloud.get_cloud_keys(did) for did in device_ids}

    # -- discovery/connect (blocking; runs on the Poller's own thread) ----
    def _discover_raw(self) -> dict[int, dict[str, Any]]:
        if self._pinned:
            found: dict[int, dict[str, Any]] = {}
            for p in self._pinned:
                try:
                    # discover()'s type hint says list[...] | None, but a bare
                    # IP string is the documented/working usage (verified live) —
                    # a list would be double-wrapped and break sendto().
                    one = midea_discover(discover_type=[DeviceType.AC], ip_address=cast(Any, p["ip"]))
                except Exception:
                    one = {}
                for did, d in one.items():
                    found[did] = {**d, "_name": p.get("name") or ""}
            return found
        try:
            return midea_discover(discover_type=[DeviceType.AC])
        except Exception:
            return {}

    def _try_connect(self, raw: dict[str, Any], token: str, key: str) -> MideaACDevice | None:
        dev = cast(
            "MideaACDevice | None",
            device_selector(
                name=raw.get("_name") or "",
                device_id=raw["device_id"], device_type=raw["type"], ip_address=raw["ip_address"],
                port=raw["port"], token=token, key=key, device_protocol=raw["protocol"],
                model=raw.get("model", ""), subtype=0, customize="",
            ),
        )
        if dev is None:
            return None
        if dev.connect(check_protocol=True):
            return dev
        return None

    def _discover_all(self) -> None:
        """Run a discovery pass, gated by DISCOVERY_RETRY_INTERVAL — a failure
        here is often a cloud-side rate limit, so this must not be retried on
        every poll tick like Roku's cheap local SSDP retry."""
        now = time.time()
        if self._attempted and now - self._last_attempt_t < DISCOVERY_RETRY_INTERVAL:
            return
        self._last_attempt_t = now
        self._attempted = True

        raw = self._discover_raw()
        self._last_seen_count = len(raw)
        self._last_connect_error = ""

        new_ids = [did for did in raw if did not in self._devices]
        new_v3_ids = [
            did for did in new_ids
            if raw[did]["protocol"] == 3 and str(did) not in self._token_cache
        ]
        keys_by_device: dict[int, dict[int, dict[str, str]]] = {}
        if new_v3_ids and self._account and self._password:
            try:
                keys_by_device = asyncio.run(self._fetch_keys(new_v3_ids))
            except Exception as e:
                self._last_connect_error = str(e) or type(e).__name__

        connected_any = False
        responded_ips = {d["ip_address"] for d in raw.values()}
        for did in new_ids:
            d = raw[did]
            dev: MideaACDevice | None = None
            if d["protocol"] != 3:
                dev = self._try_connect(d, "", "")
            else:
                cached = self._token_cache.get(str(did))
                if cached:
                    dev = self._try_connect(d, cached["token"], cached["key"])
                    if dev is None:
                        self._token_cache.pop(str(did), None)
                if dev is None:
                    for method_keys in keys_by_device.get(did, {}).values():
                        dev = self._try_connect(d, method_keys["token"], method_keys["key"])
                        if dev is not None:
                            self._token_cache[str(did)] = method_keys
                            _save_token_cache(self._token_cache)
                            break
            if dev is not None:
                dev.daemon = True
                dev.open()
                self._devices[did] = dev
                self._ips[did] = d["ip_address"]
                connected_any = True
                # Drop any stale synthetic placeholder this IP had before we
                # ever got a real device_id for it (see the pinned loop below).
                for stale_id in [uid for uid, u in self._units.items() if uid < 0 and u.ip == d["ip_address"]]:
                    del self._units[stale_id]
            else:
                # Responded to the UDP broadcast (so we have a real device_id)
                # but pairing failed — still show a dimmed placeholder card
                # rather than vanishing entirely.
                self._units[did] = MideaUnit(id=did, ip=d["ip_address"], name=d.get("_name") or "", online=False)
                if not self._last_connect_error:
                    self._last_connect_error = f"{d['ip_address']} did not respond to pairing"

        # Pinned entries that gave no UDP reply at all this pass — no real
        # device_id to key off, so use a stable synthetic one (position in
        # the config list) as long as we've never actually connected them.
        # Only pinned entries get this treatment: a broadcast-only unit we've
        # genuinely never heard from has nothing to remember it by.
        for i, p in enumerate(self._pinned):
            ip = p["ip"]
            if ip in responded_ips or any(u.ip == ip for u in self._units.values()):
                continue
            placeholder_id = -(1000 + i)
            self._units[placeholder_id] = MideaUnit(id=placeholder_id, ip=ip, name=p.get("name") or ip, online=False)

        if connected_any:
            with self._lock:
                for did, dev in self._devices.items():
                    self._units[did] = _unit_from_device(dev, self._ips.get(did, ""))
            self.error = ""
        elif not self._units:
            if self._last_seen_count == 0:
                self.error = "No Midea units responded on the LAN"
            else:
                self.error = f"Found {self._last_seen_count} unit(s) but couldn't pair: {self._last_connect_error or 'unknown error'}"

    def _refresh_snapshot(self) -> None:
        """No network I/O — each connected device's own persistent background
        thread keeps ``dev.attributes``/``dev.available`` live-updated, so
        this just re-derives MideaUnit snapshots from current in-memory
        state."""
        if not self._devices:
            return
        with self._lock:
            for did, dev in self._devices.items():
                self._units[did] = _unit_from_device(dev, self._ips.get(did, ""))

    def poll(self, focused: bool) -> None:
        if self.mock:
            self._load_mock()
            return
        self._discover_all()
        self._refresh_snapshot()

    # -- reads/commands (main thread) -------------------------------------
    def snapshot(self) -> dict[int, MideaUnit]:
        with self._lock:
            return dict(self._units)

    def apply_edit(self, unit_id: int, fld: EditableField) -> None:
        """Push one edited field to the live device and mirror the result into
        the cache. midea-local's setters are fire-and-forget at the protocol
        layer (the actual state update lands via the device's own background
        thread once it parses the ack) — sleep a short settle delay, then
        rebuild the cached MideaUnit from the device's *actual* current
        state, never optimistically."""
        if self.mock:
            self._apply_edit_mock(unit_id, fld)
            return
        dev = self._devices.get(unit_id)
        if dev is None:
            return
        try:
            if fld.api_key == "operational_mode":
                dev.set_attribute("mode", _MODE_TO_INT[fld.value])
            elif fld.api_key == "fan_speed":
                dev.set_attribute("fan_speed", _FAN_TO_INT[fld.value])
            elif fld.api_key == "swing_mode":
                vertical, horizontal = _SWING_TO_BOOLS[fld.value]
                dev.set_swing(vertical, horizontal)
            elif fld.api_key == "target_temperature":
                value = _f_to_c(fld.value) if dev.attributes.get("temp_fahrenheit") else float(fld.value)
                dev.set_target_temperature(value, None)
            elif fld.api_key == "eco":
                dev.set_attribute("eco_mode", fld.value)
            elif fld.api_key == "turbo":
                dev.set_attribute("boost_mode", fld.value)
            elif fld.api_key == "display_on":
                dev.set_attribute("screen_display", fld.value)
            else:  # "power_state"
                dev.set_attribute("power", fld.value)
        except Exception as e:
            self.error = f"Command to unit {unit_id} failed: {type(e).__name__}"
            return
        time.sleep(EDIT_SETTLE_DELAY)
        with self._lock:
            self._units[unit_id] = _unit_from_device(dev, self._ips.get(unit_id, ""))

    def _apply_edit_mock(self, unit_id: int, fld: EditableField) -> None:
        """Mock mode has no real device to push a command to — mutate the
        fixture directly instead, so hotkeys are actually interactive when
        developing/demoing against HOME_CONTROL_MOCK=1."""
        prev = self._units.get(unit_id)
        if prev is None:
            return
        if fld.api_key == "target_temperature":
            value = _f_to_c(fld.value) if prev.fahrenheit else float(fld.value)
            with self._lock:
                self._units[unit_id] = dataclasses.replace(prev, target_temp_c=value)
            return
        field_name = _API_KEY_TO_UNIT_FIELD.get(fld.api_key)
        if field_name is None:
            return
        with self._lock:
            self._units[unit_id] = dataclasses.replace(prev, **{field_name: fld.value})

    # -- mock fixtures -----------------------------------------------------
    def _load_mock(self) -> None:
        if self._units:
            return
        with self._lock:
            self._units = {
                151732604866906: MideaUnit(
                    id=151732604866906, ip="192.168.1.50", name="Living Room", online=True,
                    power=True, mode="COOL", fan_speed="MEDIUM", swing_mode="OFF",
                    target_temp_c=24.0, indoor_temp_c=24.0, outdoor_temp_c=23.5, fahrenheit=True,
                ),
                151732604866907: MideaUnit(
                    id=151732604866907, ip="192.168.1.51", name="Bedroom", online=True,
                    power=True, mode="FAN_ONLY", fan_speed="LOW", swing_mode="VERTICAL",
                    target_temp_c=22.0, indoor_temp_c=26.0, outdoor_temp_c=23.5, fahrenheit=True,
                    eco=True, display_on=False, filter_alert=True,
                ),
                151732604866908: MideaUnit(
                    id=151732604866908, ip="192.168.1.52", name="Office", online=False,
                    power=False, fahrenheit=True,
                ),
            }


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


_CHIP_LABEL = {"FAN_ONLY": "Fan", "MEDIUM": "Med"}


def _chip_label(opt: str) -> str:
    return _CHIP_LABEL.get(opt, opt.title())


class MideaSystem(System):
    """Every unit (online or not) is always fully expanded as a 3-line card:
    a header (badge/name/temp) and two control rows. Only the *current*
    value of each field is shown — not every option — so hotkeys can act
    directly on a field without a separate "select it first" step: ↕ (or
    j/k) picks which *online* unit p/m/f/s/e/t/d act on, ←→ nudges its
    target temperature, and each hotkey's letter is always the field
    label's first letter, colored as a standing mnemonic. Offline units
    still render their full (last-known or default) card, entirely dimmed —
    just not selectable, since there's nothing live to command."""

    name = "Midea AC"
    color_key = "midea"

    def __init__(self) -> None:
        self.ctl = MideaController()
        self.selected = 0  # index into _online_units()
        self.scroll = 0
        self._num_buf: str | None = None

    @property
    def collapsed_height(self) -> int:
        n = len(self.ctl.snapshot())
        if n == 0:
            return _STATUS_WRAP_LINES  # room for a wrapped "no units"/error message
        return max(1, min(n, 6))

    def start(self) -> None:
        self.ctl.start()

    def stop(self) -> None:
        self.ctl.stop()

    def poll(self, focused: bool) -> None:
        self.ctl.poll(focused)

    def _units(self) -> list[MideaUnit]:
        return sorted(self.ctl.snapshot().values(), key=lambda u: u.name.lower())

    def _online_units(self) -> list[MideaUnit]:
        return [u for u in self._units() if u.online]

    def _clamp_scroll(self, total: int, focus: int, visible: int) -> None:
        if focus < self.scroll:
            self.scroll = focus
        elif focus >= self.scroll + visible:
            self.scroll = focus - visible + 1
        self.scroll = max(0, min(self.scroll, max(0, total - visible)))

    # -- rendering (small/unfocused box) --------------------------------
    def collapsed_lines(self, width: int) -> list[Line]:
        units = self._units()
        if not units:
            msg = self.ctl.error or "Discovering..."
            wrapped = textwrap.wrap(msg, max(10, width)) or [msg]
            return [[Seg(line, dim=True)] for line in wrapped[:_STATUS_WRAP_LINES]]
        return [self._unit_row(u, width) for u in units]

    def _unit_row(self, u: MideaUnit, width: int) -> Line:
        label, color = unit_badge(u)
        left = [Seg(label, color, bold=(color == "midea_teal")), Seg(f"  {u.name}")]
        if not u.online or not u.power:
            return left
        cur = _fmt_temp(u.indoor_temp_c, u.fahrenheit)
        tgt = _fmt_temp(u.target_temp_c, u.fahrenheit)
        right = [Seg(f"{cur} → {tgt}   {u.fan_speed.replace('_', ' ').title()}", dim=True)]
        return justify(left, right, width)

    # -- rendering (expanded/focused box) --------------------------------
    def render_expanded(self, region: Region) -> None:
        units = self._units()
        if not units:
            msg = self.ctl.error or "Discovering units..."
            wrapped = textwrap.wrap(msg, max(10, region.width)) or [msg]
            for i, line in enumerate(wrapped):
                if i >= region.height:
                    break
                region.text(i, 0, line, dim=True)
            return
        online = self._online_units()
        selected_id = None
        if online:
            self.selected = max(0, min(self.selected, len(online) - 1))
            selected_id = online[self.selected].id
        rows: list[Line] = []
        focus_row = 0
        for u in units:
            is_selected = u.id == selected_id
            card = self._card_rows(u, is_selected, region.width)
            if is_selected:
                focus_row = len(rows)
            rows.extend(card)
            rows.append([])  # blank separator between cards
        if rows:
            rows.pop()
        self._clamp_scroll(len(rows), focus_row, region.height)
        for i in range(region.height):
            idx = self.scroll + i
            if idx >= len(rows):
                break
            region.segs(i, rows[idx])

    def _card_rows(self, u: MideaUnit, is_selected: bool, width: int) -> list[Line]:
        header = self._header_row(u, is_selected, width)
        row_a = self._row_a(u, width)
        row_b = self._row_b(u, width)
        if not u.online:
            return [self._dim(header), self._dim(row_a), self._dim(row_b)]
        return [header, row_a, row_b]

    @staticmethod
    def _dim(line: Line) -> Line:
        return [Seg(s.text, "", dim=True) for s in line]

    def _header_row(self, u: MideaUnit, is_selected: bool, width: int) -> Line:
        label, color = unit_badge(u)
        cursor = Seg("▶ ", self.color, bold=True) if is_selected else Seg("  ")
        if not u.online:
            return [cursor, Seg(label, color), Seg(f"  {u.name} (unreachable)")]
        left: Line = [cursor, Seg(label, color, bold=(color == "midea_teal")), Seg(f"  {u.name}")]
        right: Line = []
        if u.filter_alert:
            right.append(Seg("filter!  ", "yellow"))
        if u.error_code:
            right.append(Seg(f"err {u.error_code}  ", "yellow"))
        if self._num_buf is not None and is_selected:
            tgt_text = f"{self._num_buf}_"
        else:
            tgt_text = _fmt_temp(u.target_temp_c, u.fahrenheit)
        right.append(Seg(f"{_fmt_temp(u.indoor_temp_c, u.fahrenheit)} → ", dim=True))
        right.append(Seg(tgt_text, self.color if is_selected else "", bold=is_selected))
        return justify(left, right, width)

    def _row_a(self, u: MideaUnit, width: int) -> Line:
        """Mode (all options, current one highlighted) on the left; Eco/
        Display toggles right-justified to match the header's temp. Power
        isn't repeated here — the badge (● OFF/COOL/...) and ENTER already
        cover it."""
        left: Line = [Seg(_FIELD_INDENT)]
        left.extend(self._enum_chip("Mode", list(u.supported_modes), u.mode))
        right: Line = []
        if u.supports_eco:
            right.extend(self._toggle_chip("Eco", u.eco))
        if u.supports_display_control:
            if right:
                right.append(Seg("   "))
            right.extend(self._toggle_chip("Displ", u.display_on))
        return justify(left, right, width) if right else left

    def _row_b(self, u: MideaUnit, width: int) -> Line:
        """Fan speed (all options, current one highlighted) on the left;
        Swing/Turbo toggles right-justified to match. One extra space of
        indent versus _row_a: "Fan" is one letter shorter than "Mode", so
        this keeps the two rows' option lists starting in the same column."""
        left: Line = [Seg(_FIELD_INDENT + " ")]
        left.extend(self._enum_chip("Fan", list(u.supported_fan_speeds), u.fan_speed))
        right: Line = list(self._toggle_chip("Swing", u.swing_mode != "OFF"))
        if u.supports_turbo:
            right.append(Seg("   "))
            right.extend(self._toggle_chip("Turbo", u.turbo))
        return justify(left, right, width)

    def _enum_chip(self, label: str, options: list[str], current: str) -> Line:
        """``label``'s first letter is always the accent-colored mnemonic;
        every option is listed, with the current one highlighted — not
        bracketed, so the row's width never jitters as it changes."""
        segs: Line = [Seg(label[0], self.color, bold=True), Seg(f"{label[1:]} ", dim=True)]
        for i, opt in enumerate(options):
            if i > 0:
                segs.append(Seg(" "))
            disp = _chip_label(opt)
            segs.append(Seg(disp, self.color if opt == current else "", bold=(opt == current), dim=(opt != current)))
        return segs

    def _toggle_chip(self, label: str, value: bool) -> Line:
        """Mnemonic first letter always colored; the whole chip goes bold/
        accent when ``value`` is True (the "enabled" state stands out on its
        own, independent of which unit is currently selected)."""
        dot = "●" if value else "○"
        if value:
            return [Seg(f"{label} {dot}", self.color, bold=True)]
        return [Seg(label[0], self.color, bold=True), Seg(f"{label[1:]} {dot}", dim=True)]

    # -- toolbar/help --------------------------------------------------
    def toolbar(self) -> str:
        if self._num_buf is not None:
            return "type temp   ENTER set   ESC cancel"
        return "↕ select device   ←→ temp   ENTER power"

    def toolbar_line(self) -> Line | None:
        if self._num_buf is not None:
            return hint_row(hint("type", "temp", self.color), hint("ENTER", "set", self.color),
                            hint("ESC", "cancel", self.color))
        return hint_row(
            hint("↕", "select device", self.color), hint("←→", "temp", self.color),
            hint("ENTER", "power", self.color),
        )

    def help_notes(self) -> list[str]:
        return [
            "Hotkeys act on the selected unit; each field's colored letter is",
            "its key: p power, m mode, f fan, s swing, e eco, t turbo, d display.",
            "Auto-discovers via LAN broadcast; pin IPs in [midea] units to skip it.",
            "V3 units need a one-time cloud pairing; the token is cached afterward.",
        ]

    # -- input -----------------------------------------------------------
    def handle_key(self, key: int) -> bool:
        online = self._online_units()
        if self._num_buf is not None:
            return self._handle_num_entry(key, online)
        if key in (curses.KEY_UP, ord("k")):
            self.selected = max(0, self.selected - 1)
            return True
        if key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(max(0, len(online) - 1), self.selected + 1)
            return True
        if not online:
            return False
        self.selected = max(0, min(self.selected, len(online) - 1))
        u = online[self.selected]
        if key in (curses.KEY_LEFT, ord("h")):
            self._step_temp(u, -1)
        elif key in (curses.KEY_RIGHT, ord("l")):
            self._step_temp(u, 1)
        elif key in (ord("\n"), curses.KEY_ENTER, ord("p")):
            self._toggle(u, "power_state", u.power)
        elif key == ord("m"):
            self._cycle(u, "operational_mode", list(u.supported_modes), u.mode)
        elif key == ord("f"):
            self._cycle(u, "fan_speed", list(u.supported_fan_speeds), u.fan_speed)
        elif key == ord("s"):
            self._toggle_swing(u)
        elif key == ord("e") and u.supports_eco:
            self._toggle(u, "eco", u.eco)
        elif key == ord("t") and u.supports_turbo:
            self._toggle(u, "turbo", u.turbo)
        elif key == ord("d") and u.supports_display_control:
            self._toggle(u, "display_on", u.display_on)
        elif ord("0") <= key <= ord("9"):
            self._num_buf = chr(key)
        else:
            return False
        return True

    def _toggle(self, u: MideaUnit, api_key: str, current: bool) -> None:
        self.ctl.apply_edit(u.id, EditableField(api_key, api_key, not current, field_type="bool"))

    def _cycle(self, u: MideaUnit, api_key: str, options: list[str], current: str) -> None:
        if not options:
            return
        idx = options.index(current) if current in options else 0
        new_val = options[(idx + 1) % len(options)]
        self.ctl.apply_edit(u.id, EditableField(api_key, api_key, new_val, field_type="enum"))

    def _toggle_swing(self, u: MideaUnit) -> None:
        others = [o for o in u.supported_swing_modes if o != "OFF"]
        new_val = "OFF" if u.swing_mode != "OFF" else (others[0] if others else "OFF")
        self.ctl.apply_edit(u.id, EditableField("swing_mode", "swing_mode", new_val, field_type="enum"))

    def _step_temp(self, u: MideaUnit, direction: int) -> None:
        if u.fahrenheit:
            cur, lo, hi = round(_c_to_f(u.target_temp_c)), round(_c_to_f(u.min_temp_c)), round(_c_to_f(u.max_temp_c))
        else:
            cur, lo, hi = round(u.target_temp_c), round(u.min_temp_c), round(u.max_temp_c)
        new_val = min(hi, max(lo, cur + direction))
        self.ctl.apply_edit(u.id, EditableField("target_temperature", "target_temperature", new_val, field_type="int"))

    def _handle_num_entry(self, key: int, online: list[MideaUnit]) -> bool:
        if not online:
            self._num_buf = None
            return False
        self.selected = max(0, min(self.selected, len(online) - 1))
        u = online[self.selected]
        buf = self._num_buf or ""
        if key == 27:  # ESC — cancel
            self._num_buf = None
        elif key in (ord("\n"), curses.KEY_ENTER):
            self._num_buf = None
            if buf:
                try:
                    val = int(buf)
                    if u.fahrenheit:
                        lo, hi = round(_c_to_f(u.min_temp_c)), round(_c_to_f(u.max_temp_c))
                    else:
                        lo, hi = round(u.min_temp_c), round(u.max_temp_c)
                    val = min(hi, max(lo, val))
                    self.ctl.apply_edit(
                        u.id, EditableField("target_temperature", "target_temperature", val, field_type="int")
                    )
                except ValueError:
                    pass
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self._num_buf = buf[:-1]
        elif ord("0") <= key <= ord("9"):
            self._num_buf = buf + chr(key)
        return True

    # -- voice control -----------------------------------------------------
    def voice_actions(self) -> list[VoiceAction]:
        return [
            VoiceAction(
                name="ac_power",
                description="Turn a Midea AC unit on or off by name.",
                parameters={
                    "unit": {"type": "string", "description": "Unit name, e.g. 'Living Room'."},
                    "on": {"type": "boolean", "description": "true to turn on, false to turn off."},
                },
                required=["unit", "on"],
                handler=self._voice_power,
            ),
            VoiceAction(
                name="ac_temperature",
                description="Set a Midea AC unit's target temperature (in its own displayed scale).",
                parameters={
                    "unit": {"type": "string", "description": "Unit name."},
                    "degrees": {"type": "integer", "description": "Target temperature."},
                },
                required=["unit", "degrees"],
                handler=self._voice_temperature,
            ),
        ]

    def voice_context(self) -> str:
        names = [u.name for u in self._units()]
        return f"Midea AC units: {', '.join(names)}" if names else ""

    def _find_unit(self, name: str) -> MideaUnit | None:
        name = name.lower()
        units = self._units()
        exact = next((u for u in units if u.name.lower() == name), None)
        if exact:
            return exact
        return next((u for u in units if name in u.name.lower()), None)

    def _voice_power(self, args: dict[str, Any]) -> str:
        u = self._find_unit(args.get("unit", ""))
        if u is None:
            return f"No AC unit found matching '{args.get('unit', '')}'"
        on = bool(args.get("on"))
        self.ctl.apply_edit(u.id, EditableField("power", "power_state", on, field_type="bool"))
        return f"Turned {u.name} {'on' if on else 'off'}"

    def _voice_temperature(self, args: dict[str, Any]) -> str:
        u = self._find_unit(args.get("unit", ""))
        if u is None:
            return f"No AC unit found matching '{args.get('unit', '')}'"
        degrees = args.get("degrees")
        if degrees is None:
            return "No temperature given"
        # apply_edit expects the value in the unit's own display scale and
        # converts to Celsius internally — do not pre-convert here.
        self.ctl.apply_edit(
            u.id, EditableField("target temp", "target_temperature", float(degrees), field_type="int")
        )
        return f"Set {u.name} to {degrees}°{'F' if u.fahrenheit else 'C'}"
