"""Midea AC panel (midea-local, the extracted core of the Home Assistant
midea_ac_lan integration).

Split into:
  * MideaController — thin sync wrapper. midea-local's device objects are
    plain ``threading.Thread`` subclasses: each connected unit
    runs its own persistent background thread doing heartbeats/refreshes and
    parsing pushed state updates, and ``dev.attributes`` is a live-updated
    dict you can read with zero network I/O. Only the one-time cloud login
    (V3 token/key pairing) is ``async def`` — done via a single blocking
    ``asyncio.run(...)`` call, no dedicated event-loop thread needed.

    Nothing here ever waits on a query. midea-local's ``connect(
    check_protocol=True)`` sends 8 queries and blocks for each reply in turn,
    so the two this unit ignores cost QUERY_TIMEOUT (2s) each and a card takes
    ~7s to appear. Instead we connect without the protocol probe (TCP + V3
    auth only), start the device's own thread, and hand it the two queries
    that actually paint a card — status and B5 capabilities — as
    fire-and-forget sends. The thread parses the replies into
    ``dev.attributes``/``dev.capabilities`` and the next poll tick picks them
    up: measured 0.2-0.4s to a full card against a quiet unit. The remaining
    queries follow once the card is up, so a silent query costs nothing but
    an unanswered packet.
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
default) and "SmartHome"/MSmartHome (what Midea is migrating users to) are
the two relevant ones; set ``[midea] cloud = "smarthome"`` in config to use
the latter. This library's SmartHomeCloud implementation is the reason for the
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
from midealocal.devices.ac.message import MessageCapabilitiesQuery, MessageQuery
from midealocal.discover import discover as midea_discover

from .. import config
from ..ui import (
    BADGE_ACTIVE,
    BADGE_IDLE,
    Line,
    Region,
    Seg,
    badge_color,
    cursor,
    hint,
    hint_row,
    justify,
    toggle_dot,
    wrap,
)
from .base import System, VoiceAction

# Minimum gap between discovery attempts. A failure here can be a cloud-side
# auth rate limit — retrying every poll tick (as fast as 1s when focused)
# would hammer that endpoint, unlike Roku's cheap local-only SSDP retry.
DISCOVERY_RETRY_INTERVAL = 60
# The AC's setpoint resolution: it stores half-degrees Celsius, and
# MessageGeneralSet encodes the integer part with int() but the half-degree
# bit with round(t * 2) — so a value that isn't already a multiple of 0.5
# can set those two halves from different sides and land somewhere else
# entirely. 22.78°C (73°F stepped up from 72°F) encodes as a flat 22.0°C:
# the setpoint never moves and the arrow key looks dead. Quantize first and
# every whole °F maps to its own half-degree, round-tripping exactly.
TEMP_STEP_C = 0.5

# A freshly connected unit's first status reply lands on the device's own
# thread a fraction of a second after we ask for it — but it only reaches the
# panel when a poll tick re-reads it, and Midea sits last in the panel order,
# so at startup that tick is poll_interval_idle (5s) away. Poll fast while any
# connected unit is still waiting for that first reply, then drop back to the
# normal cadence. SETTLE_WINDOW caps it so a unit that never answers can't
# hold the fast cadence forever.
SETTLE_POLL_INTERVAL = 0.2
SETTLE_WINDOW = 15.0
# A unit's picture arrives in three waves — status, then B5 capabilities, then
# the follow-up queries (_fill_in) carrying screen_display/error_code. Each
# wave is useless until a poll tick reads it, so the fast cadence has to
# outlast the last one, not just the first. There's no ack to wait on for the
# follow-up burst, so hold the fast cadence briefly after sending it.
FILL_GRACE = 1.5

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
    # True once we've actually read this unit's state at least once. A pinned
    # unit we've never reached (placeholder/failed pairing) leaves this False,
    # so the panel can show a bare "connecting…" card instead of fabricated
    # default field values. Once contacted it stays True even if the unit
    # later goes offline — then its dimmed card is genuine last-known state.
    contacted: bool = False
    # Capabilities are a second, separate reply from the status one, and it
    # can land a beat later. Until it does, the supported_* lists below are
    # placeholder defaults, not this unit's real options — the panel shows
    # the header alone rather than a card claiming the unit has one mode and
    # one fan speed.
    caps_known: bool = False
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
    # Defaults to False like every other toggle: an offline/placeholder unit
    # whose real state we haven't read yet must not render a phantom "on"
    # chip (real units always get the true value from _unit_from_device).
    display_on: bool = False
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
    """(badge text, badge state) for a unit's status dot: the mode word while it
    is actively conditioning, "FAN" for fan-only, "OFF" when powered off, "????"
    when unreachable. `ui.badge_color` turns the state into a color. Fan-only
    counts as idle: the unit is reachable and running, but not conditioning, and
    that is the distinction the dot is there to make. An unreachable *unit* is
    idle rather than a fault — one AC that dropped off the LAN is routine and
    reads as calm grey, where BADGE_FAULT is reserved for a whole panel's device
    being unreachable. The label is padded to 4 chars (the widest: "????",
    "COOL", "AUTO") so shorter ones ("OFF"/"FAN"/"DRY") don't shift the name
    column that follows the badge."""
    if not u.online:
        label, state = "????", BADGE_IDLE
    elif not u.power:
        label, state = "OFF", BADGE_IDLE
    elif u.mode == "FAN_ONLY":
        label, state = "FAN", BADGE_IDLE
    else:
        label, state = _MODE_LABEL.get(u.mode, u.mode), BADGE_ACTIVE
    return f"● {label:<4}", state


def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def _f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def _quantize_c(c: float) -> float:
    """Snap a Celsius setpoint to the half-degree grid the unit stores."""
    return round(c / TEMP_STEP_C) * TEMP_STEP_C


def _fmt_temp(c: float | None, fahrenheit: bool) -> str:
    if c is None:
        return "—"
    return f"{round(_c_to_f(c)) if fahrenheit else round(c)}°{'F' if fahrenheit else 'C'}"


# ---------------------------------------------------------------------------
# Token/key cache — best-effort, never fatal (a cache miss just re-pairs).
# Entries also carry the device's discovery metadata (ip/port/protocol/type/
# model) once it has connected successfully, so pinned units skip the UDP
# discovery round on later runs (discover() always blocks its full 5s socket
# timeout, even for a single IP that answers instantly).
#
# Only positive facts belong here. midealocal's per-connection
# _unsupported_protocol list was cached here once and must not be again: it
# is *derived from* query timeouts, so a unit that was merely slow or busy
# gets a working query marked dead forever, and it isn't stable across clean
# runs — three probes of the same unit learned two different lists. Caching
# it left the capabilities query permanently skipped, and every card rendered
# as a bare Fan/Auto row. Nothing reads that list now (we never ask
# midea-local to probe — see _try_connect), but the temptation returns every
# time someone measures a connect.
# ---------------------------------------------------------------------------


def _load_token_cache(path: Path = TOKEN_CACHE_PATH) -> dict[str, dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_token_cache(cache: dict[str, dict[str, Any]], path: Path = TOKEN_CACHE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache))
    except OSError:
        pass


def _status_ready(dev: MideaACDevice) -> bool:
    """Has a status reply landed yet? ``mode`` is 0 until one does, and no
    real mode maps to 0 — before that every attribute is a library default,
    which would render as a confident card full of numbers we never read."""
    return bool(dev.attributes.get("mode"))


def _caps_ready(dev: MideaACDevice) -> bool:
    """Has the B5 capabilities reply landed? Every AC reports at least one
    mode flag, so their absence means it hasn't."""
    caps = dev.capabilities or {}
    return any(k in caps for k in ("cool_mode", "heat_mode", "auto_mode", "dry_mode"))


def _unit_from_device(dev: MideaACDevice, ip: str) -> MideaUnit:
    a = dev.attributes
    caps = dev.capabilities or {}
    if not _caps_ready(dev):
        # Status without capabilities: report the live fields and leave every
        # supported_*/supports_* at its dataclass default, so nothing
        # downstream mistakes "not told yet" for "not supported".
        vertical, horizontal = bool(a.get("swing_vertical")), bool(a.get("swing_horizontal"))
        return MideaUnit(
            id=dev.device_id, ip=ip, name=dev.name or f"AC {dev.device_id}",
            online=dev.available, contacted=True, caps_known=False,
            power=bool(a.get("power")),
            mode=_INT_TO_MODE.get(int(a.get("mode") or 0), "COOL"),
            fan_speed=_INT_TO_FAN.get(int(a.get("fan_speed") or 0), "AUTO"),
            swing_mode=_BOOLS_TO_SWING.get((vertical, horizontal), "OFF"),
            target_temp_c=float(a.get("target_temperature") or 24.0),
            indoor_temp_c=a.get("indoor_temperature"),
            outdoor_temp_c=a.get("outdoor_temperature"),
            fahrenheit=bool(a.get("temp_fahrenheit")),
            eco=bool(a.get("eco_mode")), turbo=bool(a.get("boost_mode")),
            display_on=bool(a.get("screen_display")),
            filter_alert=bool(a.get("full_dust")), error_code=int(a.get("error_code") or 0),
        )
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
        online=dev.available, contacted=True, caps_known=True,
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
        self._filled: dict[int, float] = {}   # device id -> when its follow-up burst was sent
        self._settle_deadline = 0.0      # poll fast until this time (see SETTLE_WINDOW)
        self._token_cache = _load_token_cache()
        self.error = ""
        self.mock = os.environ.get("HOME_CONTROL_MOCK") == "1"
        self._attempted = False           # has a real discovery pass ever run
        self._last_attempt_t = 0.0
        self._last_seen_count = 0         # raw devices seen in the last pass
        self._last_connect_error = ""     # most recent per-device connect/auth failure
        if not self.mock:
            # Pinned units render immediately as (offline) placeholder cards
            # rather than a bare "Discovering..." while the first connect
            # pass runs; same synthetic-id scheme as _discover_all's loop.
            for i, p in enumerate(self._pinned):
                pid = -(1000 + i)
                self._units[pid] = MideaUnit(id=pid, ip=p["ip"], name=p.get("name") or p["ip"], online=False)

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
    def _cached_raw(self, ip: str, name: str) -> dict[str, Any] | None:
        """Rebuild a discovery-response dict for a pinned IP from cached
        metadata (written on every successful connect), so an already-paired
        unit connects with no UDP discovery round at all."""
        for did_s, entry in self._token_cache.items():
            if entry.get("ip") == ip and "port" in entry:
                return {
                    "device_id": int(did_s), "type": entry["type"], "ip_address": ip,
                    "port": entry["port"], "protocol": entry["protocol"],
                    "model": entry.get("model", ""), "_name": name, "_cached": True,
                }
        return None

    def _discover_raw(self) -> dict[int, dict[str, Any]]:
        if self._pinned:
            found: dict[int, dict[str, Any]] = {}
            for p in self._pinned:
                cached = self._cached_raw(p["ip"], p.get("name") or "")
                if cached is not None:
                    found[cached["device_id"]] = cached
                    continue
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
        # check_protocol=False keeps this to the TCP connect and the V3 auth
        # handshake — still a real pairing verdict (a bad token/key raises and
        # returns False), without the 8-query probe that blocks on every
        # silent one. set_available() is connect()'s job only in the
        # check_protocol branch, so do it here.
        if not dev.connect(check_protocol=False):
            return None
        dev.set_available(True)
        return dev

    @staticmethod
    def _prime(dev: MideaACDevice) -> None:
        """Ask for the two replies a card is made of — live status and B5
        capabilities — and don't wait for either. The device's own thread is
        already running and parses whatever comes back."""
        try:
            version = dev._message_protocol_version
            dev.build_send(MessageQuery(version), query=True)
            dev.build_send(MessageCapabilitiesQuery(version), query=True)
        except Exception:
            # A dead socket here just means no card yet; the device thread's
            # own connect loop takes over from this point.
            pass

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
            # Either branch below keys this IP by its real device_id, so any
            # synthetic placeholder for it (seeded in __init__ or by the
            # pinned loop below) is now stale.
            for stale_id in [uid for uid, u in self._units.items() if uid < 0 and u.ip == d["ip_address"]]:
                del self._units[stale_id]
            if dev is not None:
                dev.daemon = True
                dev.open()
                self._prime(dev)
                self._settle_deadline = time.time() + SETTLE_WINDOW
                self._devices[did] = dev
                self._ips[did] = d["ip_address"]
                connected_any = True
                # Connected, but the primed status reply is still in flight and
                # _refresh_snapshot won't publish a card until it lands. The
                # synthetic placeholder keyed by IP was just dropped, so without
                # this the panel has no units at all for that window and falls
                # back to "Discovering...". Re-key it by the real device id.
                self._units.setdefault(
                    did,
                    MideaUnit(id=did, ip=d["ip_address"], name=d.get("_name") or d["ip_address"],
                              online=False),
                )
                meta = {"ip": d["ip_address"], "port": d["port"], "type": d["type"],
                        "protocol": d["protocol"], "model": d.get("model", "")}
                entry = self._token_cache.setdefault(str(did), {})
                if any(entry.get(k) != v for k, v in meta.items()):
                    entry.update(meta)
                    _save_token_cache(self._token_cache)
            else:
                # Responded to the UDP broadcast (so we have a real device_id)
                # but pairing failed — still show a dimmed placeholder card
                # rather than vanishing entirely.
                self._units[did] = MideaUnit(id=did, ip=d["ip_address"], name=d.get("_name") or "", online=False)
                if not self._last_connect_error:
                    self._last_connect_error = f"{d['ip_address']} did not respond to pairing"
                if d.get("_cached"):
                    # Cache-built params failed to connect: the metadata may
                    # be stale (unit replaced, port/protocol changed), and
                    # nothing would ever correct it — drop it so the next
                    # pass falls back to a real discovery probe. A merely
                    # unplugged unit re-pairs and re-caches when it returns.
                    entry = self._token_cache.get(str(did))
                    if entry:
                        for k in ("ip", "port", "type", "protocol", "model", "unsupported"):
                            entry.pop(k, None)
                        _save_token_cache(self._token_cache)

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
            self._refresh_snapshot()
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
        state. A device whose first status reply hasn't arrived keeps its
        placeholder card: there is nothing real to show yet."""
        if not self._devices:
            return
        with self._lock:
            for did, dev in self._devices.items():
                if _status_ready(dev):
                    self._units[did] = _unit_from_device(dev, self._ips.get(did, ""))

    def _fill_in(self) -> None:
        """Once a unit's card is up, ask for everything else exactly once —
        the fields the two priming queries don't carry (screen_display and
        error_code ride on MessageNewProtocolQuery, plus power/humidity
        counters). Fire-and-forget again, so the queries this unit ignores
        cost nothing but an unanswered packet."""
        for did, dev in self._devices.items():
            if did in self._filled or not _status_ready(dev):
                continue
            self._filled[did] = time.time()
            try:
                dev.refresh_status()
            except Exception:
                self._filled.pop(did, None)

    def poll(self, focused: bool) -> None:
        if self.mock:
            self._load_mock()
            return
        self._discover_all()
        self._refresh_snapshot()
        self._fill_in()

    def _fully_read(self, did: int, dev: MideaACDevice, now: float) -> bool:
        """Has every wave of this unit's first picture arrived — status,
        capabilities, and the follow-up burst (which has no ack, so it just
        gets FILL_GRACE to land)?"""
        if not (_status_ready(dev) and _caps_ready(dev)):
            return False
        sent_at = self._filled.get(did)
        return sent_at is not None and now - sent_at >= FILL_GRACE

    @property
    def settling(self) -> bool:
        """Is any connected unit still filling in its first picture? While one
        is, the panel polls at SETTLE_POLL_INTERVAL so each wave reaches the
        card as it lands instead of waiting out a 5s idle tick."""
        now = time.time()
        if now > self._settle_deadline:
            return False
        return not all(self._fully_read(did, dev, now) for did, dev in self._devices.items())

    # -- reads/commands (main thread) -------------------------------------
    def snapshot(self) -> dict[int, MideaUnit]:
        with self._lock:
            return dict(self._units)

    def target_c_for(self, unit_id: int, display_value: float) -> float:
        """Convert a setpoint typed/stepped in the unit's own display scale to
        the half-degree Celsius the protocol can actually carry."""
        unit = self._units.get(unit_id)
        fahrenheit = unit.fahrenheit if unit else False
        return _quantize_c(_f_to_c(display_value) if fahrenheit else float(display_value))

    def apply_edit(self, unit_id: int, fld: EditableField) -> None:
        """Send one field to the unit and reflect it locally straight away.

        This runs on the main thread from handle_key, so it must not wait on
        the network: midea-local's setters are fire-and-forget at the protocol
        layer, and the real value arrives on the device's own thread when it
        parses the ack. It used to sleep 0.3s here and re-read — which stalled
        the UI on every keypress *and* still lost races, leaving the next
        arrow press to step from a stale setpoint. Now the send returns
        immediately and the local mirror is corrected by the next poll tick,
        at most a second later."""
        if not self.mock:
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
                    dev.set_target_temperature(self.target_c_for(unit_id, fld.value), None)
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
        self._mirror_edit(unit_id, fld)

    def _mirror_edit(self, unit_id: int, fld: EditableField) -> None:
        """Apply an edit to the cached unit so the card responds to the
        keypress at once. Safe to do ahead of the device for temperature
        because the value sent is already on the unit's half-degree grid, so
        this mirrors exactly what it will report back; any other field the
        unit refuses is corrected within a poll tick. In mock mode this *is*
        the whole edit — there's no device to send to."""
        prev = self._units.get(unit_id)
        if prev is None:
            return
        if fld.api_key == "target_temperature":
            with self._lock:
                self._units[unit_id] = dataclasses.replace(
                    prev, target_temp_c=self.target_c_for(unit_id, fld.value)
                )
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
                    contacted=True, power=True, mode="COOL", fan_speed="MEDIUM", swing_mode="OFF",
                    target_temp_c=24.0, indoor_temp_c=24.0, outdoor_temp_c=23.5, fahrenheit=True,
                    display_on=True,  # keep one demo unit with its display on
                ),
                151732604866907: MideaUnit(
                    id=151732604866907, ip="192.168.1.51", name="Bedroom", online=True,
                    contacted=True, power=True, mode="FAN_ONLY", fan_speed="LOW", swing_mode="VERTICAL",
                    target_temp_c=22.0, indoor_temp_c=26.0, outdoor_temp_c=23.5, fahrenheit=True,
                    eco=True, display_on=False, filter_alert=True,
                ),
                151732604866908: MideaUnit(
                    id=151732604866908, ip="192.168.1.52", name="Office", online=False,
                    contacted=True, power=False, fahrenheit=True,  # offline, showing last-known
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
    just not selectable, since there's nothing live to command. Powered-off
    units dim the same way but stay selectable (only the selection cursor
    keeps its accent), so ENTER can wake them."""

    name = "Midea AC"
    color_key = "midea"

    def __init__(self) -> None:
        self.ctl = MideaController()
        self.selected = 0  # index into _online_units()
        self.scroll = 0
        self._num_buf: str | None = None

    @property
    def poll_interval_focused(self) -> float:
        return SETTLE_POLL_INTERVAL if self.ctl.settling else 1.0

    @property
    def poll_interval_idle(self) -> float:
        return SETTLE_POLL_INTERVAL if self.ctl.settling else 5.0

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
            return [[Seg(line, dim=True)] for line in wrap(msg, max(10, width), _STATUS_WRAP_LINES)]
        return [self._unit_row(u, width) for u in units]

    def _unit_row(self, u: MideaUnit, width: int) -> Line:
        label, state = unit_badge(u)
        color = badge_color(state, self.color)
        left = [Seg(label, color, bold=(state == BADGE_ACTIVE)), Seg(f"  {u.name}")]
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
            region.text_wrapped(0, 0, msg, dim=True)
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
        if not u.contacted:
            # Never reached this unit — we have no real field values, so show
            # a single dimmed "connecting…" line rather than a card full of
            # fabricated defaults.
            return [self._dim(header)]
        if not u.caps_known:
            # Status has landed but the capabilities reply hasn't: the header
            # is all real (badge, name, temperatures), while the option rows
            # would be placeholder defaults. Show the header alone until the
            # unit tells us what it actually supports.
            return [header if u.online and u.power else self._dim(header)]
        row_a = self._row_a(u, width)
        row_b = self._row_b(u, width)
        if not u.online or not u.power:
            rows = [self._dim(header), self._dim(row_a), self._dim(row_b)]
            if u.online and is_selected:
                # An off unit is still selectable (to power it back on), so
                # the accent cursor must survive the dimming.
                rows[0][0] = header[0]
            return rows
        return [header, row_a, row_b]

    @staticmethod
    def _dim(line: Line) -> Line:
        return [Seg(s.text, "", dim=True) for s in line]

    def _header_row(self, u: MideaUnit, is_selected: bool, width: int) -> Line:
        label, state = unit_badge(u)
        color = badge_color(state, self.color)
        cur = cursor(self.color, is_selected)
        if not u.online:
            # "connecting…" while we've never reached it; "unreachable" only
            # once we had it and lost contact (so the card's dimmed values are
            # real last-known state, not defaults).
            status = "unreachable" if u.contacted else "connecting..."
            return [cur, Seg(label, color), Seg(f"  {u.name} ({status})")]
        left: Line = [cur, Seg(label, color, bold=(state == BADGE_ACTIVE)), Seg(f"  {u.name}")]
        right: Line = []
        if u.filter_alert:
            right.append(Seg("filter!  ", "warn"))
        if u.error_code:
            right.append(Seg(f"err {u.error_code}  ", "warn"))
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
        dot = toggle_dot(value)
        if value:
            return [Seg(f"{label} {dot}", self.color, bold=True)]
        return [Seg(label[0], self.color, bold=True), Seg(f"{label[1:]} {dot}", dim=True)]

    # -- toolbar/help --------------------------------------------------
    def toolbar_line(self) -> Line | None:
        if self._num_buf is not None:
            return hint_row(hint("type", "temp", self.color), hint("ENTER", "set", self.color),
                            hint("ESC", "cancel", self.color))
        return hint_row(
            hint("↕", "select device", self.color), hint("←→", "temp", self.color),
            hint("ENTER", "power", self.color),
        )

    def help_notes(self) -> list[str]:
        # Keep in sync with the Midea AC entry in README.md "Device support".
        return [
            "Controls Midea air conditioners over their local-LAN protocol "
            "via midea-local (the extracted core of Home Assistant's "
            "midea_ac_lan integration). Each connected unit runs its own "
            "persistent background thread doing heartbeats and state "
            "refreshes, so the cards reflect live state with no polling lag. "
            "Units are auto-discovered by LAN broadcast.",
            "Config [midea]: units pins units by IP, allowing us to skip the "
            "broadcast-discovery scan and display the devices instantly. Also "
            "allows assigning each a friendly name that overrides the "
            'unit\'s firmware name (e.g. "net_ac_16A4"). Newer "V3" units '
            "require a one-time cloud login to fetch a per-device token, "
            "cached locally afterward so the cloud is only touched once; set "
            "account and password to your Midea app login, and cloud to "
            'match which app it belongs to ("nethome_plus" or "smarthome").',
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
