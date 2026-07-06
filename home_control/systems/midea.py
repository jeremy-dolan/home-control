"""Midea AC panel (msmart-ng, LAN protocol V2/V3).

Split into:
  * MideaController — owns a dedicated background asyncio event-loop thread,
    since msmart-ng's entire device/discovery API is ``async def`` while every
    other controller in this app is synchronous. ``poll()``/``apply_edit()``
    submit coroutines onto that loop via
    ``asyncio.run_coroutine_threadsafe(...).result(timeout=...)``.
  * MideaSystem — the panel: one collapsed line per detected unit, and an
    expanded cursor-navigable unit list with per-unit settings nested inline
    under the selected row (mirrors HueSystem's device dialog).

Set HOME_CONTROL_MOCK=1 to render 3 fixture units with no network/asyncio
loop at all (mock mode never starts the event-loop thread).

V3 devices need a per-device token+key, normally fetched from Midea's cloud
on first pairing. msmart-ng bakes in a shared demo NetHome Plus account for
this, so no user Midea account is required; the resulting token+key are
cached locally (see TOKEN_CACHE_PATH) so the cloud is only touched once per
device, not on every app start.

msmart-ng's own ``Discover.connect()`` is hardcoded to pair via the "NetHome
Plus" app's cloud (``NetHomePlusCloud``) — it has no option to use a
different Midea-family app's backend. Users whose account instead lives in
the "SmartHome"/MSmartHome app's cloud (a different backend entirely: own
base URL, own app ID, own login crypto — same physical protocol, different
account database) need ``[midea] cloud = "smarthome"`` in config, which
routes pairing through ``_authenticate_smarthome`` (a from-scratch
replication of ``Discover._authenticate_device``'s logic against
``SmartHomeCloud`` instead) rather than ``Discover.connect``.
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

from msmart.cloud import CloudError, SmartHomeCloud
from msmart.device import AirConditioner
from msmart.discover import Discover
from msmart.lan import AuthenticationError, Security

from .. import config
from ..ui import Line, Region, Seg, hint, hint_row, justify
from .base import System, VoiceAction

DISCOVERY_WAIT = 5      # seconds msmart-ng itself waits for broadcast replies
DISCOVERY_TIMEOUT = 15  # bridge timeout wrapping a whole discovery pass
CONNECT_TIMEOUT = 8     # bridge timeout wrapping a refresh() of already-known units
EDIT_TIMEOUT = 5        # bridge timeout for a single dialog field edit (render thread)
# Minimum gap between discovery attempts once we have zero connected units. A
# failure here is often a cloud-side auth rate limit (msmart-ng's shared demo
# NetHome Plus account) — retrying every poll tick (as fast as 1s when
# focused) would hammer that endpoint and prolong the lockout, unlike Roku's
# cheap local-only SSDP retry-every-poll convention.
DISCOVERY_RETRY_INTERVAL = 60

TOKEN_CACHE_PATH = Path(
    os.environ.get("HOME_CONTROL_MIDEA_CACHE")
    or (Path.home() / ".cache" / "home-control" / "midea_tokens.json")
)

_MODE_LABEL = {"COOL": "COOL", "DRY": "DRY", "AUTO": "AUTO", "FAN_ONLY": "FAN"}
_FIELD_INDENT = "    "
_FIELD_NAME_WIDTH = 12
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
    the value renders and how ←/→/ENTER mutate it: bool/action toggle,
    int/float step by ``step`` (and accept typed entry), enum cycles a list
    (held in ``step``). "action" behaves exactly like "bool" here — the only
    difference (display_on has no setter, needs `toggle_display()` instead of
    setattr+apply) lives in MideaController.apply_edit, not in this class."""
    name: str
    api_key: str
    value: Any
    min_val: Any = None
    max_val: Any = None
    step: Any = None
    field_type: str = "info"  # "bool" | "int" | "float" | "enum" | "action"


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


def _enum_name(v: Any) -> str:
    return v.name if hasattr(v, "name") else str(v)


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


def _unit_from_device(dev: AirConditioner, name_override: str = "") -> MideaUnit:
    return MideaUnit(
        id=dev.id, ip=dev.ip, name=name_override or dev.name or f"AC {dev.id}",
        online=dev.online,
        power=bool(dev.power_state),
        mode=_enum_name(dev.operational_mode) if dev.operational_mode is not None else "COOL",
        fan_speed=_enum_name(dev.fan_speed) if dev.fan_speed is not None else "AUTO",
        swing_mode=_enum_name(dev.swing_mode) if dev.swing_mode is not None else "OFF",
        target_temp_c=dev.target_temperature if dev.target_temperature is not None else 24.0,
        indoor_temp_c=dev.indoor_temperature,
        outdoor_temp_c=dev.outdoor_temperature,
        fahrenheit=bool(dev.fahrenheit),
        eco=bool(dev.eco), turbo=bool(dev.turbo), display_on=bool(dev.display_on),
        filter_alert=bool(dev.filter_alert), error_code=dev.error_code or 0,
        min_temp_c=dev.min_target_temperature, max_temp_c=dev.max_target_temperature,
        supported_modes=tuple(_enum_name(m) for m in dev.supported_operation_modes),
        supported_fan_speeds=tuple(_enum_name(s) for s in dev.supported_fan_speeds),
        supported_swing_modes=tuple(_enum_name(s) for s in dev.supported_swing_modes),
        supports_eco=dev.supports_eco, supports_turbo=dev.supports_turbo,
        supports_display_control=dev.supports_display_control,
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
        self._smarthome_cloud: SmartHomeCloud | None = None
        self._lock = threading.Lock()
        self._units: dict[int, MideaUnit] = {}
        self._devices: dict[int, AirConditioner] = {}
        self._token_cache = _load_token_cache()
        self.error = ""
        self.mock = os.environ.get("HOME_CONTROL_MOCK") == "1"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._attempted = False           # has a real discovery pass ever run
        self._last_attempt_t = 0.0
        self._last_seen_count = 0         # raw devices seen in the last pass, before connect
        self._last_connect_error = ""     # most recent per-device connect/auth failure

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self.mock:
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

    def stop(self) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread:
            self._loop_thread.join(timeout=2)

    # -- asyncio bridge --------------------------------------------------
    def _run_coro(self, coro: Any, timeout: float) -> Any | None:
        """Submit a coroutine to the controller's own event loop and block
        (bounded) for the result. A timeout does NOT cancel the coroutine —
        msmart's own LAN.send already retries internally, and the next poll's
        refresh() reconciles whatever actually landed, matching the rest of
        this app's "poll reconciles" philosophy."""
        if self._loop is None:
            return None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except Exception:
            return None

    # -- cloud pairing (SmartHome app backend; NetHome Plus goes through
    # Discover.connect instead, see module docstring) --------------------
    async def _get_smarthome_cloud(self) -> SmartHomeCloud:
        if self._smarthome_cloud is None:
            cloud = SmartHomeCloud(account=self._account, password=self._password)
            await cloud.login()
            self._smarthome_cloud = cloud
        return self._smarthome_cloud

    async def _authenticate_smarthome(self, dev: AirConditioner) -> bool:
        try:
            cloud = await self._get_smarthome_cloud()
        except CloudError as e:
            self._last_connect_error = str(e)
            return False
        for endian in ("little", "big"):
            udpid = Security.udpid(dev.id.to_bytes(6, endian)).hex()
            try:
                token, key = await cloud.get_token(udpid)
            except CloudError as e:
                self._last_connect_error = str(e)
                continue
            try:
                await dev.authenticate(token, key)
                return True
            except AuthenticationError:
                continue
        return False

    # -- discovery/connect (run as coroutines on the loop thread) --------
    async def _finish_connect(self, dev: AirConditioner, name_override: str = "") -> MideaUnit | None:
        ok = False
        if dev.version == 3:
            cached = self._token_cache.get(str(dev.id))
            if cached:
                try:
                    await dev.authenticate(cached["token"], cached["key"])
                    await dev.refresh()
                    ok = dev.online
                except Exception:
                    ok = False
        if not ok:
            try:
                if dev.version == 3 and self._cloud_kind == "smarthome":
                    ok = await self._authenticate_smarthome(dev)
                    if ok:
                        await dev.refresh()
                        ok = dev.online
                else:
                    ok = await Discover.connect(dev)
            except Exception as e:
                ok = False
                self._last_connect_error = str(e) or type(e).__name__
            if ok and dev.version == 3 and dev.token and dev.key:
                self._token_cache[str(dev.id)] = {"token": dev.token, "key": dev.key}
                _save_token_cache(self._token_cache)
        if not ok:
            if not self._last_connect_error:
                self._last_connect_error = f"{dev.ip} did not respond to pairing"
            return None
        if dev.id not in self._devices:
            try:
                await dev.get_capabilities()
            except Exception:
                pass
        self._devices[dev.id] = dev
        return _unit_from_device(dev, name_override)

    async def _connect_broadcast(self) -> list[MideaUnit]:
        devices = cast(list[AirConditioner], await Discover.discover(
            auto_connect=False, timeout=DISCOVERY_WAIT, account=self._account, password=self._password,
        ))
        self._last_seen_count = len(devices)
        results = await asyncio.gather(*(self._finish_connect(d) for d in devices))
        return [u for u in results if u is not None]

    async def _connect_pinned(self) -> list[MideaUnit]:
        async def one(ip: str, name: str) -> MideaUnit | None:
            try:
                dev = cast(
                    "AirConditioner | None",
                    await Discover.discover_single(
                        ip, auto_connect=False, account=self._account, password=self._password,
                    ),
                )
            except Exception:
                return None
            if dev is None:
                return None
            self._last_seen_count += 1
            return await self._finish_connect(dev, name)

        self._last_seen_count = 0
        results = await asyncio.gather(*(one(p["ip"], p.get("name", "")) for p in self._pinned))
        return [u for u in results if u is not None]

    def _discover_all(self) -> None:
        """Run a discovery pass, gated by DISCOVERY_RETRY_INTERVAL once we have
        zero connected units — a failure here is often the cloud-side rate
        limit, so this must not be retried on every poll tick like Roku's
        cheap local SSDP retry."""
        now = time.time()
        if self._attempted and now - self._last_attempt_t < DISCOVERY_RETRY_INTERVAL:
            return
        self._last_attempt_t = now
        self._attempted = True
        self._last_seen_count = 0
        self._last_connect_error = ""
        if self._pinned:
            results = self._run_coro(self._connect_pinned(), DISCOVERY_TIMEOUT)
        else:
            results = self._run_coro(self._connect_broadcast(), DISCOVERY_TIMEOUT)
        if results:
            with self._lock:
                for u in results:
                    self._units[u.id] = u
            self.error = ""
        elif not self._units:
            if self._last_seen_count == 0:
                self.error = "No Midea units responded on the LAN"
            else:
                self.error = f"Found {self._last_seen_count} unit(s) but couldn't pair: {self._last_connect_error}"

    def _refresh_all(self) -> None:
        async def _refresh_one(uid: int, dev: AirConditioner) -> MideaUnit | None:
            try:
                await dev.refresh()
                prev = self._units.get(uid)
                return _unit_from_device(dev, prev.name if prev else "")
            except Exception:
                prev = self._units.get(uid)
                if prev is None:
                    return None
                return dataclasses.replace(prev, online=False)

        async def _refresh_all_coro() -> list[MideaUnit]:
            results = await asyncio.gather(
                *(_refresh_one(uid, dev) for uid, dev in list(self._devices.items()))
            )
            return [u for u in results if u is not None]

        results = self._run_coro(_refresh_all_coro(), CONNECT_TIMEOUT)
        if results:
            with self._lock:
                for u in results:
                    self._units[u.id] = u

    def poll(self, focused: bool) -> None:
        if self.mock:
            self._load_mock()
            return
        # Always due for a look — internally cooldown-gated once we have zero
        # connected units, and cheap (no network call) once fully connected.
        # This keeps retrying a transient failure (e.g. a cloud rate limit)
        # instead of getting stuck on the first attempt's error forever, and
        # still picks up units that appear on the LAN later.
        self._discover_all()
        if self._devices:
            self._refresh_all()

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
        the cache. Runs from the render thread (dialog key handler) — bounded
        by EDIT_TIMEOUT so a hung/unreachable unit can't freeze the UI. Only
        mirrors the cache from the post-apply device state, never
        optimistically — matches HueController.apply_light_edit. ``fld.value``
        is always in the unit's own *display* scale (°F if the unit prefers
        it) — conversion to the wire unit (°C) happens here, not by callers."""
        dev = self._devices.get(unit_id)
        if dev is None:
            return

        async def _do() -> MideaUnit:
            if fld.api_key == "display_on":
                await dev.toggle_display()
            else:
                value: Any = fld.value
                if fld.api_key == "target_temperature" and dev.fahrenheit:
                    value = _f_to_c(value)
                enum_classes: dict[str, Any] = {
                    "operational_mode": AirConditioner.OperationalMode,
                    "fan_speed": AirConditioner.FanSpeed,
                    "swing_mode": AirConditioner.SwingMode,
                }
                if fld.api_key in enum_classes:
                    value = getattr(enum_classes[fld.api_key], value)
                setattr(dev, fld.api_key, value)
                await dev.apply()
            prev = self._units.get(unit_id)
            return _unit_from_device(dev, prev.name if prev else "")

        result = self._run_coro(_do(), EDIT_TIMEOUT)
        if result is not None:
            with self._lock:
                self._units[unit_id] = result
        else:
            self.error = f"Command to unit {unit_id} timed out"

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


class MideaSystem(System):
    name = "Midea AC"
    color_key = "midea"

    def __init__(self) -> None:
        self.ctl = MideaController()
        self.cursor = 0
        self.scroll = 0
        self.mode = "list"  # "list" | "device"
        self.info_fields: list[EditableField] = []
        self.info_read_only: list[tuple[str, str]] = []
        self.info_cursor = 0
        self.info_unit_id = 0
        self.info_fahrenheit = True
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

    # -- rendering -----------------------------------------------------
    def collapsed_lines(self, width: int) -> list[Line]:
        units = self._units()
        if not units:
            msg = self.ctl.error or "discovering…"
            wrapped = textwrap.wrap(msg, max(10, width)) or [msg]
            return [[Seg(line, dim=True)] for line in wrapped[:_STATUS_WRAP_LINES]]
        return [self._unit_row(u, width, selected=False) for u in units]

    def render_expanded(self, region: Region) -> None:
        units = self._units()
        if not units:
            msg = self.ctl.error or "Discovering units…"
            wrapped = textwrap.wrap(msg, max(10, region.width)) or [msg]
            for i, line in enumerate(wrapped):
                if i >= region.height:
                    break
                region.text(i, 0, line, dim=True)
            return
        rows, focus = self._composite_rows(units, region.width)
        self._clamp_scroll(len(rows), focus, region.height)
        for i in range(region.height):
            idx = self.scroll + i
            if idx >= len(rows):
                break
            region.segs(i, rows[idx])

    def _composite_rows(self, units: list[MideaUnit], width: int) -> tuple[list[Line], int]:
        rows: list[Line] = []
        focus = 0
        for i, u in enumerate(units):
            selected = self.mode == "list" and i == self.cursor
            row = self._unit_row(u, width, selected=selected)
            if selected:
                focus = len(rows)
            rows.append(row)
            if self.mode == "device" and u.id == self.info_unit_id:
                extra, dfocus = self._device_rows()
                focus = len(rows) + dfocus
                rows.extend(extra)
        return rows, focus

    def _clamp_scroll(self, total: int, focus: int, visible: int) -> None:
        if focus < self.scroll:
            self.scroll = focus
        elif focus >= self.scroll + visible:
            self.scroll = focus - visible + 1
        self.scroll = max(0, min(self.scroll, max(0, total - visible)))

    @staticmethod
    def _highlight(line: Line) -> Line:
        for s in line:
            s.bold = True
            s.dim = False
        return line

    def _unit_row(self, u: MideaUnit, width: int, *, selected: bool) -> Line:
        cursor = Seg("▶ ", self.color, bold=True) if selected else Seg("  ")
        label, color = unit_badge(u)
        left = [cursor, Seg(label, color, bold=(color == "midea_teal")), Seg(f"  {u.name}", bold=selected)]
        if not u.online or not u.power:
            line = left
        else:
            cur = _fmt_temp(u.indoor_temp_c, u.fahrenheit)
            tgt = _fmt_temp(u.target_temp_c, u.fahrenheit)
            right = [Seg(f"{cur} → {tgt}   {u.fan_speed.replace('_', ' ').title()}", dim=True)]
            line = justify(left, right, width)
        return self._highlight(line) if selected else line

    def _device_rows(self) -> tuple[list[Line], int]:
        rows: list[Line] = []
        for i, fld in enumerate(self.info_fields):
            sel = i == self.info_cursor
            marker = "▶ " if sel else "  "
            name = fld.name.ljust(_FIELD_NAME_WIDTH)
            text = f"{_FIELD_INDENT}{marker}{name}{self._field_value(fld, sel)}"
            rows.append([Seg(text, self.color if sel else "", bold=sel)])
        focus = self.info_cursor
        for label, val in self.info_read_only:
            rows.append([Seg(f"{_FIELD_INDENT}  {label}: {val}", dim=True)])
        return rows, focus

    def _field_value(self, fld: EditableField, sel: bool) -> str:
        if self._num_buf is not None and sel and fld.field_type in ("int", "float"):
            return self._num_buf + "_"
        if fld.field_type in ("bool", "action"):
            return "on" if fld.value else "off"
        if fld.api_key == "target_temperature":
            return f"{fld.value}°{'F' if self.info_fahrenheit else 'C'}"
        if fld.field_type == "enum":
            return str(fld.value).replace("_", " ").title()
        return str(fld.value)

    # -- dialog build --------------------------------------------------
    def _build_unit_fields(self, u: MideaUnit) -> None:
        if u.fahrenheit:
            cur_t = round(_c_to_f(u.target_temp_c))
            min_t = round(_c_to_f(u.min_temp_c))
            max_t = round(_c_to_f(u.max_temp_c))
        else:
            cur_t, min_t, max_t = round(u.target_temp_c), round(u.min_temp_c), round(u.max_temp_c)
        fields = [
            EditableField("power", "power_state", u.power, field_type="bool"),
            EditableField("mode", "operational_mode", u.mode, step=list(u.supported_modes), field_type="enum"),
            EditableField("fan speed", "fan_speed", u.fan_speed, step=list(u.supported_fan_speeds), field_type="enum"),
            EditableField("target temp", "target_temperature", cur_t, min_t, max_t, 1, "int"),
            EditableField("swing", "swing_mode", u.swing_mode, step=list(u.supported_swing_modes), field_type="enum"),
        ]
        if u.supports_eco:
            fields.append(EditableField("eco", "eco", u.eco, field_type="bool"))
        if u.supports_turbo:
            fields.append(EditableField("turbo", "turbo", u.turbo, field_type="bool"))
        if u.supports_display_control:
            fields.append(EditableField("display", "display_on", u.display_on, field_type="action"))
        self.info_fields = fields
        self.info_read_only = [
            ("Indoor", _fmt_temp(u.indoor_temp_c, u.fahrenheit)),
            ("Outdoor", _fmt_temp(u.outdoor_temp_c, u.fahrenheit)),
        ]
        if u.error_code:
            self.info_read_only.append(("Error", str(u.error_code)))
        if u.filter_alert:
            self.info_read_only.append(("Filter", "clean/replace"))
        self.info_cursor = 0
        self.info_unit_id = u.id
        self.info_fahrenheit = u.fahrenheit

    # -- toolbar/help --------------------------------------------------
    def _list_toolbar_hints(self) -> Line:
        return hint_row(
            hint("↕", "nav", self.color),
            hint("ENTER", "power", self.color),
            hint("i", "nfo", self.color, paren=True),
        )

    def _device_toolbar_hints(self) -> Line:
        if self._num_buf is not None:
            return hint_row(hint("type", "value", self.color), hint("ENTER", "set", self.color),
                            hint("ESC", "cancel", self.color))
        return hint_row(
            hint("↕", "nav", self.color), hint("←→", "adjust", self.color),
            hint("ENTER", "edit/toggle", self.color), hint("i/ESC", "back", self.color),
        )

    def toolbar(self) -> str:
        if self.mode == "device":
            if self._num_buf is not None:
                return "type value   ENTER set   ESC cancel"
            return "↕ nav   ←→ adjust   ENTER edit/toggle   i/ESC back"
        return "↕ nav   ENTER power   i info"

    def toolbar_line(self) -> Line | None:
        return self._device_toolbar_hints() if self.mode == "device" else self._list_toolbar_hints()

    def help_notes(self) -> list[str]:
        return [
            "Auto-discovers via LAN broadcast; pin IPs in [midea] units to skip it.",
            "Uses msmart-ng's shared demo cloud account once, then caches the token.",
        ]

    # -- input -----------------------------------------------------------
    def handle_key(self, key: int) -> bool:
        if self.mode == "device":
            return self._handle_device_key(key)
        return self._handle_list_key(key)

    def _handle_list_key(self, key: int) -> bool:
        units = self._units()
        if key in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(max(0, len(units) - 1), self.cursor + 1)
        elif key in (ord("\n"), curses.KEY_ENTER):
            if units and 0 <= self.cursor < len(units):
                self.ctl.toggle_power(units[self.cursor].id)
        elif key == ord("i"):
            if units and 0 <= self.cursor < len(units):
                self._open_device(units[self.cursor])
        else:
            return False
        return True

    def _open_device(self, u: MideaUnit) -> None:
        self._build_unit_fields(u)
        self._num_buf = None
        self.mode = "device"

    def _close_device(self) -> None:
        self._num_buf = None
        self.mode = "list"

    def _handle_device_key(self, key: int) -> bool:
        if self._num_buf is not None:
            return self._handle_num_entry(key)
        if key in (curses.KEY_UP, ord("k")):
            self.info_cursor = max(0, self.info_cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.info_cursor = min(len(self.info_fields) - 1, self.info_cursor + 1)
        elif key in (curses.KEY_LEFT, ord("h")):
            self._device_step(-1)
        elif key in (curses.KEY_RIGHT, ord("l")):
            self._device_step(1)
        elif key in (ord("\n"), curses.KEY_ENTER):
            self._device_enter()
        elif key in (27, ord("i"), ord("q")):
            self._close_device()
        else:
            return False
        return True

    def _device_step(self, direction: int) -> None:
        fld = self.info_fields[self.info_cursor]
        if fld.field_type in ("bool", "action"):
            fld.value = not fld.value
            self._apply(fld)
        elif fld.field_type in ("int", "float"):
            nv = fld.value + direction * fld.step
            nv = min(fld.max_val, max(fld.min_val, nv))
            fld.value = round(nv, 4) if fld.field_type == "float" else nv
            self._apply(fld)
        elif fld.field_type == "enum":
            fld.value = self._cycle_enum(fld, direction)
            self._apply(fld)

    def _device_enter(self) -> None:
        fld = self.info_fields[self.info_cursor]
        if fld.field_type in ("bool", "action"):
            fld.value = not fld.value
            self._apply(fld)
        elif fld.field_type in ("int", "float"):
            self._num_buf = ""
        elif fld.field_type == "enum":
            fld.value = self._cycle_enum(fld, 1)
            self._apply(fld)

    def _handle_num_entry(self, key: int) -> bool:
        fld = self.info_fields[self.info_cursor]
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
                    self._apply(fld)
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

    def _apply(self, fld: EditableField) -> None:
        self.ctl.apply_edit(self.info_unit_id, fld)

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
