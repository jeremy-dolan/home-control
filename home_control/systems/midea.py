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
  * MideaSystem — the panel: one collapsed line per detected unit, and an
    expanded cursor-navigable unit list with per-unit settings nested inline
    under the selected row (mirrors HueSystem's device dialog).

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
    supported_fan_speeds: tuple[str, ...] = ("SILENT", "LOW", "MEDIUM", "HIGH", "AUTO", "MAX")
    supported_swing_modes: tuple[str, ...] = ("OFF", "VERTICAL")
    supports_eco: bool = True
    supports_turbo: bool = True
    supports_display_control: bool = True


@dataclass
class EditableField:
    """One editable row in the per-unit dialog. ``field_type`` drives both how
    the value renders and how ←/→/ENTER mutate it: bool toggles, int/float
    steps by ``step`` (and accepts typed entry), enum cycles a list (held in
    ``step``)."""
    name: str
    api_key: str
    value: Any
    min_val: Any = None
    max_val: Any = None
    step: Any = None
    field_type: str = "info"  # "bool" | "int" | "float" | "enum"


def unit_badge(u: MideaUnit) -> tuple[str, str]:
    """(badge text, color) for a unit's status dot: colored accent + mode word
    when actively conditioning, grey "FAN" for fan-only, dim grey (no word)
    for off or unreachable. Deliberately not Router's red-for-offline
    convention — off/unreachable read as calm grey here, not alarming."""
    if not u.online:
        return "● unreachable", "light_grey"
    if not u.power:
        return "●", "light_grey"
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
        fan_speeds = ["SILENT", "LOW", "MEDIUM", "HIGH", "MAX", "AUTO"]
    else:
        fan_speeds = [
            name
            for name, present in (
                ("SILENT", caps.get("fan_silent")),
                ("LOW", caps.get("fan_low")),
                ("MEDIUM", caps.get("fan_medium")),
                ("HIGH", caps.get("fan_high")),
            )
            if present
        ]
        fan_speeds.append("AUTO")
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
            elif not self._last_connect_error:
                self._last_connect_error = f"{d['ip_address']} did not respond to pairing"

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

    def toggle_power(self, unit_id: int) -> None:
        u = self._units.get(unit_id)
        if u is None:
            return
        self.apply_edit(unit_id, EditableField("power", "power_state", not u.power, field_type="bool"))

    def apply_edit(self, unit_id: int, fld: EditableField) -> None:
        """Push one edited field to the live device and mirror the result into
        the cache. midea-local's setters are fire-and-forget at the protocol
        layer (the actual state update lands via the device's own background
        thread once it parses the ack) — sleep a short settle delay, then
        rebuild the cached MideaUnit from the device's *actual* current
        state, never optimistically."""
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


_CHIP_LABEL = {"FAN_ONLY": "FAN", "SILENT": "SIL", "MEDIUM": "MED", "VERTICAL": "VERT", "HORIZONTAL": "HORIZ"}
_HEADER_KEYS = ("power_state", "target_temperature")
_ROW2_KEYS = ("operational_mode", "fan_speed")
_ROW3_KEYS = ("swing_mode", "eco", "turbo", "display_on")


def _chip_label(opt: str) -> str:
    return _CHIP_LABEL.get(opt, opt)


class MideaSystem(System):
    """Every online unit is always fully expanded — a 3-line card (header
    with power/temp, a mode/fan row, a swing/eco/turbo/display row) rather
    than a Hue-style collapsed list + drill-in dialog, since there are only
    ever a handful of AC units. A single flat cursor walks every editable
    control of every unit in top-to-bottom, left-to-right order; ↕ moves it,
    ←→ adjusts the focused control in place, exactly like Hue's dialog did —
    just with no separate "open" step."""

    name = "Midea AC"
    color_key = "midea"

    def __init__(self) -> None:
        self.ctl = MideaController()
        self.cursor = 0  # flat index across every online unit's control list
        self.scroll = 0
        self._unit_fields: dict[int, list[EditableField]] = {}
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
        if self._num_buf is not None:
            return  # don't yank the field out from under an in-progress numeric entry
        for u in self._units():
            if u.online:
                self._unit_fields[u.id] = self._build_fields(u)
            else:
                self._unit_fields.pop(u.id, None)

    def _units(self) -> list[MideaUnit]:
        return sorted(self.ctl.snapshot().values(), key=lambda u: u.name.lower())

    def _flat(self) -> list[tuple[MideaUnit, int]]:
        out: list[tuple[MideaUnit, int]] = []
        for u in self._units():
            out.extend((u, i) for i in range(len(self._unit_fields.get(u.id, []))))
        return out

    def _clamp_scroll(self, total: int, focus: int, visible: int) -> None:
        if focus < self.scroll:
            self.scroll = focus
        elif focus >= self.scroll + visible:
            self.scroll = focus - visible + 1
        self.scroll = max(0, min(self.scroll, max(0, total - visible)))

    # -- rendering -----------------------------------------------------
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
        flat = self._flat()
        if flat:
            self.cursor = max(0, min(self.cursor, len(flat) - 1))
        rows, focus = self._all_rows(units, flat, region.width)
        self._clamp_scroll(len(rows), focus, region.height)
        for i in range(region.height):
            idx = self.scroll + i
            if idx >= len(rows):
                break
            region.segs(i, rows[idx])

    def _all_rows(self, units: list[MideaUnit], flat: list[tuple[MideaUnit, int]], width: int) -> tuple[list[Line], int]:
        cur_unit_id = flat[self.cursor][0].id if flat else None
        rows: list[Line] = []
        focus_row = 0
        for u in units:
            fields = self._unit_fields.get(u.id, [])
            focused_idx = flat[self.cursor][1] if u.id == cur_unit_id else -1
            card, card_focus = self._card_rows(u, fields, focused_idx, width)
            if u.id == cur_unit_id:
                focus_row = len(rows) + card_focus
            rows.extend(card)
            rows.append([])  # blank separator between cards
        if rows:
            rows.pop()
        return rows, focus_row

    def _card_rows(self, u: MideaUnit, fields: list[EditableField], focused_idx: int, width: int) -> tuple[list[Line], int]:
        by_key = {f.api_key: f for f in fields}
        focused_key = fields[focused_idx].api_key if 0 <= focused_idx < len(fields) else None
        header = self._header_row(u, by_key, focused_key, width)
        if not u.online or not fields:
            return [header], 0
        row2 = self._chip_row([by_key[k] for k in _ROW2_KEYS if k in by_key], focused_key)
        row3 = self._chip_row([by_key[k] for k in _ROW3_KEYS if k in by_key], focused_key)
        if focused_key in _HEADER_KEYS:
            focus = 0
        elif focused_key in _ROW2_KEYS:
            focus = 1
        else:
            focus = 2
        return [header, row2, row3], focus

    def _header_row(self, u: MideaUnit, by_key: dict[str, EditableField], focused_key: str | None, width: int) -> Line:
        label, color = unit_badge(u)
        left: Line = [Seg(label, color, bold=(color == "midea_teal")), Seg(f"  {u.name}")]
        power = by_key.get("power_state")
        if power is not None:
            is_f = focused_key == "power_state"
            chip = f"[{'ON' if power.value else 'OFF'}]" if is_f else ("ON" if power.value else "off")
            left.append(Seg(f"  {chip}", self.color if is_f else "", bold=is_f, dim=not (is_f or power.value)))
        if not u.online:
            return left
        right: Line = []
        if u.filter_alert:
            right.append(Seg("filter!  ", "yellow"))
        if u.error_code:
            right.append(Seg(f"err {u.error_code}  ", "yellow"))
        temp = by_key.get("target_temperature")
        if temp is not None:
            is_f = focused_key == "target_temperature"
            cur = _fmt_temp(u.indoor_temp_c, u.fahrenheit)
            unit_suffix = "F" if u.fahrenheit else "C"
            if self._num_buf is not None and is_f:
                tgt = f"[{self._num_buf}_]"
            elif is_f:
                tgt = f"[{temp.value}°{unit_suffix}]"
            else:
                tgt = f"{temp.value}°{unit_suffix}"
            right.append(Seg(f"{cur} → ", dim=True))
            right.append(Seg(tgt, self.color if is_f else "", bold=is_f))
        return justify(left, right, width) if right else left

    def _chip_row(self, fields: list[EditableField], focused_key: str | None) -> Line:
        if not fields:
            return []
        segs: Line = [Seg(_FIELD_INDENT)]
        for gi, fld in enumerate(fields):
            if gi > 0:
                segs.append(Seg("   "))
            is_f = fld.api_key == focused_key
            segs.append(Seg(f"{fld.name.title()} ", dim=True))
            if fld.field_type == "enum":
                for opt in fld.step:
                    label = _chip_label(opt)
                    text = f"[{label}]" if opt == fld.value else label
                    color = self.color if (is_f and opt == fld.value) else ""
                    segs.append(Seg(text, color, bold=(is_f and opt == fld.value), dim=(opt != fld.value)))
                    segs.append(Seg(" "))
            else:  # bool toggle
                dot = "●" if fld.value else "○"
                text = f"[{dot}]" if is_f else dot
                segs.append(Seg(text, self.color if is_f else "", bold=is_f, dim=not (is_f or fld.value)))
        return segs

    # -- fields ----------------------------------------------------------
    def _build_fields(self, u: MideaUnit) -> list[EditableField]:
        if u.fahrenheit:
            cur_t, min_t, max_t = round(_c_to_f(u.target_temp_c)), round(_c_to_f(u.min_temp_c)), round(_c_to_f(u.max_temp_c))
        else:
            cur_t, min_t, max_t = round(u.target_temp_c), round(u.min_temp_c), round(u.max_temp_c)
        fields = [
            EditableField("power", "power_state", u.power, field_type="bool"),
            EditableField("temp", "target_temperature", cur_t, min_t, max_t, 1, "int"),
            EditableField("mode", "operational_mode", u.mode, step=list(u.supported_modes), field_type="enum"),
            EditableField("fan", "fan_speed", u.fan_speed, step=list(u.supported_fan_speeds), field_type="enum"),
            EditableField("swing", "swing_mode", u.swing_mode, step=list(u.supported_swing_modes), field_type="enum"),
        ]
        if u.supports_eco:
            fields.append(EditableField("eco", "eco", u.eco, field_type="bool"))
        if u.supports_turbo:
            fields.append(EditableField("turbo", "turbo", u.turbo, field_type="bool"))
        if u.supports_display_control:
            fields.append(EditableField("display", "display_on", u.display_on, field_type="bool"))
        return fields

    # -- toolbar/help --------------------------------------------------
    def toolbar(self) -> str:
        if self._num_buf is not None:
            return "type value   ENTER set   ESC cancel"
        return "↕ nav   ←→ adjust   ENTER edit/toggle"

    def toolbar_line(self) -> Line | None:
        if self._num_buf is not None:
            return hint_row(hint("type", "value", self.color), hint("ENTER", "set", self.color),
                            hint("ESC", "cancel", self.color))
        return hint_row(
            hint("↕", "nav", self.color), hint("←→", "adjust", self.color), hint("ENTER", "edit/toggle", self.color),
        )

    def help_notes(self) -> list[str]:
        return [
            "Auto-discovers via LAN broadcast; pin IPs in [midea] units to skip it.",
            "V3 units need a one-time cloud pairing; the token is cached afterward.",
        ]

    # -- input -----------------------------------------------------------
    def handle_key(self, key: int) -> bool:
        if self._num_buf is not None:
            return self._handle_num_entry(key)
        flat = self._flat()
        if not flat:
            return False
        self.cursor = max(0, min(self.cursor, len(flat) - 1))
        if key in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(len(flat) - 1, self.cursor + 1)
        elif key in (curses.KEY_LEFT, ord("h")):
            self._step(flat, -1)
        elif key in (curses.KEY_RIGHT, ord("l")):
            self._step(flat, 1)
        elif key in (ord("\n"), curses.KEY_ENTER):
            self._enter(flat)
        else:
            return False
        return True

    def _current(self, flat: list[tuple[MideaUnit, int]]) -> tuple[MideaUnit, EditableField]:
        u, i = flat[self.cursor]
        return u, self._unit_fields[u.id][i]

    def _step(self, flat: list[tuple[MideaUnit, int]], direction: int) -> None:
        u, fld = self._current(flat)
        if fld.field_type == "bool":
            fld.value = not fld.value
        elif fld.field_type in ("int", "float"):
            nv = fld.value + direction * fld.step
            nv = min(fld.max_val, max(fld.min_val, nv))
            fld.value = round(nv, 4) if fld.field_type == "float" else nv
        elif fld.field_type == "enum":
            fld.value = self._cycle_enum(fld, direction)
        self.ctl.apply_edit(u.id, fld)

    def _enter(self, flat: list[tuple[MideaUnit, int]]) -> None:
        u, fld = self._current(flat)
        if fld.field_type == "bool":
            fld.value = not fld.value
            self.ctl.apply_edit(u.id, fld)
        elif fld.field_type in ("int", "float"):
            self._num_buf = ""
        elif fld.field_type == "enum":
            fld.value = self._cycle_enum(fld, 1)
            self.ctl.apply_edit(u.id, fld)

    def _handle_num_entry(self, key: int) -> bool:
        flat = self._flat()
        u, fld = self._current(flat)
        buf = self._num_buf or ""
        if key == 27:  # ESC — cancel
            self._num_buf = None
        elif key in (ord("\n"), curses.KEY_ENTER):
            self._num_buf = None
            if buf:
                try:
                    val: Any = float(buf) if fld.field_type == "float" else int(buf)
                    val = min(fld.max_val, max(fld.min_val, val))
                    fld.value = round(val, 4) if fld.field_type == "float" else val
                    self.ctl.apply_edit(u.id, fld)
                except ValueError:
                    pass
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self._num_buf = buf[:-1]
        elif ord("0") <= key <= ord("9"):
            self._num_buf = buf + chr(key)
        elif key == ord(".") and fld.field_type == "float" and "." not in buf:
            self._num_buf = buf + "."
        return True

    def _cycle_enum(self, fld: EditableField, direction: int) -> Any:
        options: list[str] = fld.step
        try:
            idx = options.index(fld.value)
        except ValueError:
            idx = 0
        return options[(idx + direction) % len(options)]

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
