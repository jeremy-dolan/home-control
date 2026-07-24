"""Lighting panel (Philips Hue), ported from standalone-apps/hue/hue-control.py.

Split into:
  * HueController — all phue2/HTTP calls + cached state. Lock-guarded so the
    background poll thread and main-thread commands can both touch it.
  * HueSystem    — the curses panel: collapsed summary, expanded room/light
    list with brightness bars, plus three sub-modes — scenes, a per-light/room
    device dialog (on/bri/colour/effect), and a bridge system-info view.

Set HOME_CONTROL_MOCK=1 to render fixture data without a real bridge (dev only).
"""

from __future__ import annotations

import colorsys
import curses
import math
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .. import config
from ..ui import (
    BADGE_ACTIVE,
    Line,
    Region,
    Seg,
    badge_color,
    cursor,
    highlight,
    hint,
    hint_row,
    justify,
    level_bar,
    pad_between,
    rgb_color,
)
from .base import System, VoiceAction

BRIDGE_IP = "192.168.1.99"
CONNECT_TIMEOUT = 4  # seconds; phue's default 10 hangs the UI when unreachable
CLOCK_DRIFT_WARN = 60  # seconds of bridge/system clock disagreement before flagging it

# Brightness bar width in cells. The ladder below has one notch per cell so a
# single ←/→ press moves the knob exactly one cell (= 5%).
BAR_WIDTH = 20

# Discrete brightness ladder: 20 notches of 5% each across Hue's 1..254 range
# ([13, 25, …, 254]). One notch per ←/→ press, aligned to BAR_WIDTH cells.
BRI_STOPS = [round(i / BAR_WIDTH * 254) for i in range(1, BAR_WIDTH + 1)]

# Effects: "colorloop" is plain API v1; candle/fire/prism are API v2 dynamic
# effects set over the clip/v2 HTTPS endpoint. Group effects only do none/loop.
V2_EFFECTS = {"candle", "fire", "prism"}
EFFECT_CYCLE = ["none", "colorloop", "candle", "fire", "prism"]
# Color-temperature bounds, in Kelvin, for the device dialog's `ct` field.
CT_MIN_K, CT_MAX_K = 2000, 6536


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Light:
    id: int
    name: str
    on: bool
    brightness: int  # 0..254
    reachable: bool
    # Extended state for the device dialog (None when the light lacks the feature).
    light_type: str = ""
    modelid: str = ""
    swversion: str = ""
    colormode: str | None = None       # "ct" | "xy" | "hs"
    hue: int | None = None             # 0..65535
    saturation: int | None = None      # 0..254
    colortemp: int | None = None       # mired
    xy: list[float] | None = None
    effect: str | None = None


@dataclass
class Room:
    id: int  # -1 for the synthetic "Other" room
    name: str
    light_ids: list[int] = field(default_factory=list)


@dataclass
class Scene:
    scene_id: str
    name: str
    group_id: int


@dataclass
class EditableField:
    """One editable row in the device dialog. ``field_type`` drives both how the
    value renders and how ←/→/ENTER mutate it: bool toggles, int/float step by
    ``step`` (and accept typed entry), enum cycles a list (held in ``step``)."""
    name: str
    api_key: str
    value: Any
    min_val: Any = None
    max_val: Any = None
    step: Any = None
    field_type: str = "info"  # "bool" | "int" | "float" | "enum"


def _supported_colormodes(has_xy: bool, has_ct: bool) -> list[str]:
    modes: list[str] = []
    if has_ct:
        modes.append("ct")
    if has_xy:
        modes.append("xy")
    return modes


@dataclass
class BridgeInfo:
    """Static hardware/firmware facts from the bridge's /config endpoint."""
    name: str = ""
    model: str = ""        # modelid, e.g. "BSB002"
    swversion: str = ""
    apiversion: str = ""
    mac: str = ""


# modelid → friendly hardware name (the round v1 vs. square v2 bridge).
_BRIDGE_MODELS = {"BSB001": "Hue Bridge v1", "BSB002": "Hue Bridge v2"}


def bridge_model_label(model: str) -> str:
    """Friendly bridge hardware name, falling back to the raw modelid."""
    if not model:
        return ""
    return _BRIDGE_MODELS.get(model, model)


# ---------------------------------------------------------------------------
# Brightness helpers
# ---------------------------------------------------------------------------


def next_bri(current: int, direction: int) -> int | None:
    """Next brightness stop in `direction` (+1/-1). None means 'turn off'."""
    if direction > 0:
        return next((s for s in BRI_STOPS if s > current), 254)
    return next((s for s in reversed(BRI_STOPS) if s < current), None)


def brightness_bar(bri: int, on: bool, color: str = "", width: int = BAR_WIDTH) -> Line:
    """The shared `ui.level_bar` over Hue's 0-254 brightness scale. An off light
    draws the empty track — its ● ON/OFF badge already carries the state."""
    return level_bar(bri, 254, color, width, empty=not on)


# ---------------------------------------------------------------------------
# Colour helpers — turn a light's state into a true-colour swatch + label
# ---------------------------------------------------------------------------


def _clamp8(v: float) -> int:
    return int(max(0, min(255, round(v))))


def _kelvin_to_rgb(kelvin: int) -> tuple[int, int, int]:
    """Approximate a colour temperature (Kelvin) as RGB (Tanner Helland)."""
    t = max(1000, min(40000, kelvin)) / 100
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
    if t >= 66:
        b = 255.0
    elif t <= 19:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(t - 10) - 305.0447927307
    return _clamp8(r), _clamp8(g), _clamp8(b)


def _xy_to_rgb(x: float, y: float) -> tuple[int, int, int]:
    """CIE xy (at full luminance) → display RGB via Philips' Wide RGB D65 matrix,
    normalised to pure chroma so the swatch stays visible regardless of brightness."""
    if y <= 0:
        return 255, 255, 255
    big_y = 1.0
    big_x = (big_y / y) * x
    big_z = (big_y / y) * (1 - x - y)
    r = big_x * 1.656492 - big_y * 0.354851 - big_z * 0.255038
    g = -big_x * 0.707196 + big_y * 1.655397 + big_z * 0.036152
    b = big_x * 0.051713 - big_y * 0.121364 + big_z * 1.011530

    def gamma(c: float) -> float:
        c = max(0.0, c)
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055

    r, g, b = gamma(r), gamma(g), gamma(b)
    m = max(r, g, b)
    if m > 0:
        r, g, b = r / m, g / m, b / m
    return _clamp8(r * 255), _clamp8(g * 255), _clamp8(b * 255)


def light_color(ls: Light) -> tuple[tuple[int, int, int], str] | None:
    """A light's (swatch RGB, mode label), or None when it has no colour info.

    Label is the Kelvin value for colour-temperature mode (e.g. "2700K") or
    "RGB" for full-colour (xy/hue-sat) lights."""
    cm = ls.colormode
    if cm == "xy" and ls.xy:
        return _xy_to_rgb(ls.xy[0], ls.xy[1]), "RGB"
    if cm == "hs" and ls.hue is not None:
        r, g, b = colorsys.hsv_to_rgb(ls.hue / 65535, (ls.saturation or 0) / 254, 1.0)
        return (_clamp8(r * 255), _clamp8(g * 255), _clamp8(b * 255)), "RGB"
    if ls.colortemp:  # ct mode, or a ct-capable light with no colormode set
        kelvin = round(1_000_000 / ls.colortemp)
        rgb = _kelvin_to_rgb(kelvin)
        m = max(rgb)
        if m:
            rgb = (round(rgb[0] * 255 / m), round(rgb[1] * 255 / m), round(rgb[2] * 255 / m))
        return rgb, f"{kelvin}K"
    return None


# ---------------------------------------------------------------------------
# Controller (no curses)
# ---------------------------------------------------------------------------


class HueController:
    def __init__(self, ip: str | None = None):
        self.ip = ip or config.get("hue", "bridge_ip", BRIDGE_IP)
        self._bridge = None
        self._lock = threading.Lock()
        self.connected = False
        self.error = ""
        self.rooms: list[Room] = []
        self.lights: dict[int, Light] = {}
        self.scenes_by_room: dict[int, list[Scene]] = {}
        self.info = BridgeInfo()
        self._v2_ids: dict[int, str] = {}  # v1 light id → clip/v2 UUID (lazy)
        self.mock = os.environ.get("HOME_CONTROL_MOCK") == "1"
        self._mock_now: datetime | None = None  # set by push_time() under mock

    # -- connection / polling (background thread) --------------------------
    def poll(self) -> None:
        if self.mock:
            self._load_mock()
            return
        if not self.connected and not self._connect():
            return
        try:
            rooms, lights = self._fetch_state()
            with self._lock:
                self.rooms, self.lights = rooms, lights
        except Exception as e:  # noqa: BLE001 — surface any bridge/network failure
            self.connected = False
            self.error = str(e)

    def _connect(self) -> bool:
        try:
            from phue import Bridge  # imported lazily so the app runs without phue
            from phue.exceptions import PhueRegistrationException
        except ImportError:
            self.error = "phue not installed"
            return False
        try:
            # Short timeout so an unreachable bridge fails fast instead of
            # hanging the poll thread (and any command) for phue's 10s default.
            self._bridge = Bridge(self.ip, timeout=CONNECT_TIMEOUT)
            self.connected = True
            self.error = ""
            return True
        except PhueRegistrationException:
            self.error = "Press the bridge link button, then wait..."
            return False
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return False

    def _fetch_state(self) -> tuple[list[Room], dict[int, Light]]:
        b = self._bridge
        assert b is not None
        if not self.info.model:  # static — fetch once
            self._fetch_config()
        raw_lights = b.request("GET", f"/api/{b.username}/lights/")
        raw_groups = b.request("GET", f"/api/{b.username}/groups/")

        lights: dict[int, Light] = {}
        for lid_str, ld in raw_lights.items():
            lid = int(lid_str)
            st = ld.get("state", {})
            lights[lid] = Light(
                id=lid,
                name=ld.get("name", f"Light {lid}"),
                on=st.get("on", False),
                brightness=st.get("bri", 0),
                reachable=st.get("reachable", False),
                light_type=ld.get("type", ""),
                modelid=ld.get("modelid", ""),
                swversion=ld.get("swversion", ""),
                colormode=st.get("colormode"),
                hue=st.get("hue"),
                saturation=st.get("sat"),
                colortemp=st.get("ct"),
                xy=st.get("xy"),
                effect=st.get("effect"),
            )

        rooms: list[Room] = []
        grouped: set[int] = set()
        for gid_str, gd in sorted(raw_groups.items(), key=lambda x: int(x[0])):
            if gd.get("type") != "Room":  # skip zones to avoid overlap
                continue
            ids = [int(x) for x in gd.get("lights", [])]
            rooms.append(Room(id=int(gid_str), name=gd.get("name", gid_str), light_ids=ids))
            grouped.update(ids)
        ungrouped = [lid for lid in sorted(lights) if lid not in grouped]
        if ungrouped:
            rooms.append(Room(id=-1, name="Other", light_ids=ungrouped))
        return rooms, lights

    def _fetch_config(self) -> None:
        b = self._bridge
        assert b is not None
        try:
            cfg = b.request("GET", f"/api/{b.username}/config")
        except Exception:  # noqa: BLE001 — config is non-essential, ignore failures
            return
        if isinstance(cfg, dict):
            self.info = BridgeInfo(
                name=cfg.get("name", ""), model=cfg.get("modelid", ""),
                swversion=cfg.get("swversion", ""), apiversion=cfg.get("apiversion", ""),
                mac=cfg.get("mac", ""),
            )

    # -- snapshot for rendering -------------------------------------------
    def snapshot(self) -> tuple[list[Room], dict[int, Light]]:
        with self._lock:
            return list(self.rooms), dict(self.lights)

    @property
    def summary(self) -> str:
        n_on = sum(1 for ls in self.lights.values() if ls.on)
        return f"{len(self.rooms)} rooms/{len(self.lights)} lights · {n_on} on"

    # -- commands (main thread; brief blocking HTTP) -----------------------
    def _set_light(self, lid: int, key, value=None) -> bool:
        if self.mock:
            return True
        if not self.connected or self._bridge is None:
            return False  # no-op instantly when the bridge is unreachable
        try:
            if value is None:
                self._bridge.set_light(lid, key)  # key is a dict
            else:
                self._bridge.set_light(lid, key, value)
            return True
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return False

    def _set_group(self, gid: int, key, value=None) -> bool:
        if self.mock:
            return True
        if not self.connected or self._bridge is None:
            return False
        try:
            if value is None:
                self._bridge.set_group(gid, key)
            else:
                self._bridge.set_group(gid, key, value)
            return True
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return False

    def toggle_light(self, lid: int) -> None:
        ls = self.lights.get(lid)
        if ls and self._set_light(lid, "on", not ls.on):
            with self._lock:
                ls.on = not ls.on

    def adjust_light(self, lid: int, direction: int) -> None:
        ls = self.lights.get(lid)
        if not ls:
            return
        if not ls.on and direction > 0:
            self._set_light(lid, "on", True)
            with self._lock:
                ls.on = True
        new = next_bri(ls.brightness, direction)
        if new is None:  # stepped below minimum → off
            if self._set_light(lid, "on", False):
                with self._lock:
                    ls.on = False
            return
        if self._set_light(lid, "bri", new):
            with self._lock:
                ls.brightness = new

    def _room(self, gid: int) -> Room | None:
        return next((r for r in self.rooms if r.id == gid), None)

    def _room_lights(self, room: Room) -> list[Light]:
        return [self.lights[lid] for lid in room.light_ids if lid in self.lights]

    def toggle_room(self, gid: int) -> None:
        room = self._room(gid)
        if not room:
            return
        new_on = not any(ls.on for ls in self._room_lights(room))
        ok = self._toggle_room_call(room, new_on)
        if ok:
            with self._lock:
                for ls in self._room_lights(room):
                    ls.on = new_on

    def _toggle_room_call(self, room: Room, new_on: bool) -> bool:
        if room.id == -1:  # synthetic group → set each light
            return all(self._set_light(lid, "on", new_on) for lid in room.light_ids)
        return self._set_group(room.id, "on", new_on)

    def adjust_room(self, gid: int, direction: int) -> None:
        room = self._room(gid)
        if not room:
            return
        if room.id == -1:
            for lid in room.light_ids:
                self.adjust_light(lid, direction)
            return
        members = self._room_lights(room)
        if not any(ls.on for ls in members) and direction > 0:
            self._set_group(room.id, "on", True)
            with self._lock:
                for ls in members:
                    ls.on = True
        avg = sum(ls.brightness for ls in members) // len(members) if members else 128
        new = next_bri(avg, direction)
        if new is None:
            if self._set_group(room.id, "on", False):
                with self._lock:
                    for ls in members:
                        ls.on = False
            return
        if self._set_group(room.id, "bri", new):
            with self._lock:
                for ls in members:
                    ls.brightness = new

    # -- scenes ------------------------------------------------------------
    def load_scenes(self, gid: int) -> list[Scene]:
        if self.mock:
            scenes = self.scenes_by_room.get(gid, [])
            return scenes
        if not self.connected or self._bridge is None:
            return []
        try:
            b = self._bridge
            raw = b.request("GET", f"/api/{b.username}/scenes/")
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return []
        scenes = [
            Scene(scene_id=sid, name=sd.get("name", sid), group_id=int(sd.get("group", "0")))
            for sid, sd in raw.items()
            if sd.get("group") and int(sd["group"]) == gid
        ]
        scenes.sort(key=lambda s: s.name.lower())
        return scenes

    def activate_scene(self, scene: Scene) -> None:
        if self.mock or not self.connected or self._bridge is None:
            return
        try:
            self._bridge.activate_scene(scene.group_id, scene.scene_id)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)

    # -- device dialog / system info --------------------------------------
    def full_config(self) -> dict:
        """Raw bridge ``/config`` for the system-info view ({} on failure)."""
        if self.mock:
            return self._mock_config()
        if not self.connected or self._bridge is None:
            return {}
        try:
            b = self._bridge
            cfg = b.request("GET", f"/api/{b.username}/config")
            return cfg if isinstance(cfg, dict) else {}
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return {}

    def push_time(self) -> bool:
        """Set the bridge clock to the local host's UTC time. Returns success.

        The bridge normally keeps time over NTP; when that is blocked its clock
        drifts and silently misfires schedules. ``PUT /config`` with a UTC
        stamp is writable (verified against a real bridge), so this is the
        in-app nudge for the drift the bridge-info view flags.
        """
        now = datetime.now(UTC)
        if self.mock:
            self._mock_now = now
            return True
        if not self.connected or self._bridge is None:
            return False
        try:
            b = self._bridge
            b.request("PUT", f"/api/{b.username}/config", {"UTC": now.strftime("%Y-%m-%dT%H:%M:%S")})
            return True
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return False

    def group_action(self, gid: int) -> tuple[dict, dict]:
        """(action, state) for a Room group; used to seed the device dialog."""
        if self.mock:
            return self._mock_group_action(gid)
        if not self.connected or self._bridge is None:
            return {}, {}
        try:
            b = self._bridge
            gd = b.request("GET", f"/api/{b.username}/groups/{gid}")
            return gd.get("action", {}), gd.get("state", {})
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return {}, {}

    def refresh_light(self, lid: int) -> None:
        """Re-pull one light's full state (after dialog edits) into the cache."""
        if self.mock or not self.connected or self._bridge is None:
            return
        try:
            b = self._bridge
            ld = b.request("GET", f"/api/{b.username}/lights/{lid}")
            st = ld.get("state", {})
            with self._lock:
                ls = self.lights.get(lid)
                if ls:
                    ls.on = st.get("on", ls.on)
                    ls.brightness = st.get("bri", ls.brightness)
                    ls.reachable = st.get("reachable", ls.reachable)
                    ls.colormode = st.get("colormode")
                    ls.hue = st.get("hue")
                    ls.saturation = st.get("sat")
                    ls.colortemp = st.get("ct")
                    ls.xy = st.get("xy")
                    ls.effect = st.get("effect")
        except Exception:  # noqa: BLE001 — refresh is best-effort
            pass

    def apply_light_edit(self, lid: int, fld: EditableField, fields: list[EditableField]) -> None:
        """Push one edited field to a light and mirror it into the cache."""
        ls = self.lights.get(lid)
        if fld.api_key == "effect":
            if fld.value == "none":
                self._clear_effect_with_color(lid, ls)
            elif fld.value in V2_EFFECTS:
                if self._set_v2_effect(lid, fld.value) and ls:
                    with self._lock:
                        ls.effect = fld.value
            elif self._set_light(lid, "effect", fld.value) and ls:
                with self._lock:
                    ls.effect = fld.value
            return
        if fld.api_key == "colormode":
            if fld.value == "ct" and ls and ls.colortemp is not None:
                self._set_light(lid, "ct", ls.colortemp)
            elif fld.value == "xy" and ls and ls.xy is not None:
                self._set_light(lid, "xy", ls.xy)
            if ls:
                with self._lock:
                    ls.colormode = fld.value
            return
        if fld.api_key in ("x", "y"):
            xy = self._xy_from_fields(fields)
            if xy and self._set_light(lid, "xy", xy) and ls:
                with self._lock:
                    ls.xy = xy
            return
        if fld.api_key == "ct":
            mired = round(1_000_000 / fld.value)
            if self._set_light(lid, "ct", mired) and ls:
                with self._lock:
                    ls.colortemp = mired
            return
        if self._set_light(lid, fld.api_key, fld.value) and ls:
            with self._lock:
                if fld.api_key == "on":
                    ls.on = fld.value
                elif fld.api_key == "bri":
                    ls.brightness = fld.value
                elif fld.api_key == "hue":
                    ls.hue = fld.value
                elif fld.api_key == "sat":
                    ls.saturation = fld.value

    def apply_group_edit(self, gid: int, fld: EditableField, fields: list[EditableField]) -> None:
        """Push one edited field to a Room group, mirroring on/bri to members."""
        if fld.api_key == "effect" and fld.value == "none":
            cmd: dict[str, Any] = {"effect": "none"}
            cm = next((f for f in fields if f.api_key == "colormode"), None)
            cmv = cm.value if cm else None
            if cmv == "ct":
                ct = next((f for f in fields if f.api_key == "ct"), None)
                if ct:
                    cmd["ct"] = round(1_000_000 / ct.value)
            elif cmv == "xy":
                xy = self._xy_from_fields(fields)
                if xy:
                    cmd["xy"] = xy
            self._set_group(gid, cmd)
            return
        if fld.api_key == "colormode":
            if fld.value == "ct":
                ct = next((f for f in fields if f.api_key == "ct"), None)
                if ct:
                    self._set_group(gid, "ct", round(1_000_000 / ct.value))
            elif fld.value == "xy":
                xy = self._xy_from_fields(fields)
                if xy:
                    self._set_group(gid, "xy", xy)
            return
        if fld.api_key in ("x", "y"):
            xy = self._xy_from_fields(fields)
            if xy:
                self._set_group(gid, "xy", xy)
            return
        if fld.api_key == "ct":
            self._set_group(gid, "ct", round(1_000_000 / fld.value))
            return
        if self._set_group(gid, fld.api_key, fld.value):
            room = self._room(gid)
            if room and fld.api_key in ("on", "bri"):
                with self._lock:
                    for ls in self._room_lights(room):
                        if fld.api_key == "on":
                            ls.on = fld.value
                        else:
                            ls.brightness = fld.value

    @staticmethod
    def _xy_from_fields(fields: list[EditableField]) -> list[float] | None:
        x = next((f for f in fields if f.api_key == "x"), None)
        y = next((f for f in fields if f.api_key == "y"), None)
        return [x.value, y.value] if x and y else None

    def _clear_effect_with_color(self, lid: int, ls: Light | None) -> None:
        """Turn an effect off, restoring the light's last static colour."""
        cmd: dict[str, Any] = {"effect": "none"}
        if ls:
            if ls.colormode == "ct" and ls.colortemp is not None:
                cmd["ct"] = ls.colortemp
            elif ls.colormode == "xy" and ls.xy is not None:
                cmd["xy"] = ls.xy
            elif ls.colormode == "hs" and ls.hue is not None:
                cmd["hue"] = ls.hue
                if ls.saturation is not None:
                    cmd["sat"] = ls.saturation
            if ls.effect in V2_EFFECTS:
                self._set_v2_effect(lid, "none")
        if self._set_light(lid, cmd) and ls:
            with self._lock:
                ls.effect = "none"

    def _ensure_v2_ids(self) -> None:
        if self._v2_ids or self._bridge is None:
            return
        import httpx
        b = self._bridge
        try:
            resp = httpx.get(
                f"https://{b.ip}/clip/v2/resource/light",
                headers={"hue-application-key": b.username},
                verify=False, timeout=5,
            )
            resp.raise_for_status()
            mapping: dict[int, str] = {}
            for item in resp.json().get("data", []):
                id_v1 = item.get("id_v1", "")
                if id_v1.startswith("/lights/"):
                    try:
                        mapping[int(id_v1.split("/")[2])] = item["id"]
                    except (ValueError, IndexError, KeyError):
                        pass
            self._v2_ids = mapping
        except Exception as e:  # noqa: BLE001
            self.error = str(e)

    def _set_v2_effect(self, lid: int, effect: str) -> bool:
        if self.mock:
            return True
        if self._bridge is None:
            return False
        self._ensure_v2_ids()
        uuid = self._v2_ids.get(lid)
        if not uuid:
            self.error = "No v2 ID for this light"
            return False
        import httpx
        b = self._bridge
        v2_effect = "no_effect" if effect == "none" else effect
        try:
            resp = httpx.put(
                f"https://{b.ip}/clip/v2/resource/light/{uuid}",
                headers={"hue-application-key": b.username},
                json={"effects": {"effect": v2_effect}},
                verify=False, timeout=5,
            )
            resp.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            self.error = f"v2 effect error: {e}"
            return False

    # -- voice helpers (room lookup by name; absolute set) -----------------
    def room_names(self) -> list[str]:
        with self._lock:
            return [r.name for r in self.rooms]

    def find_room(self, name: str) -> Room | None:
        q = name.strip().lower()
        with self._lock:
            rooms = list(self.rooms)
        for r in rooms:  # exact name first
            if r.name.lower() == q:
                return r
        for r in rooms:  # then loose substring either direction
            if q and (q in r.name.lower() or r.name.lower() in q):
                return r
        return None

    def set_room_power(self, name: str, on: bool) -> bool:
        room = self.find_room(name)
        if not room:
            return False
        if self._toggle_room_call(room, on):
            with self._lock:
                for ls in self._room_lights(room):
                    ls.on = on
            return True
        return False

    def set_all_power(self, on: bool) -> bool:
        names = self.room_names()
        if not names:
            return False
        return all(self.set_room_power(n, on) for n in names)

    def set_room_brightness(self, name: str, percent: int) -> bool:
        room = self.find_room(name)
        if not room:
            return False
        bri = max(1, min(254, round(percent / 100 * 254)))
        if room.id == -1:
            ok = all(self._set_light(lid, "on", True) for lid in room.light_ids) and all(
                self._set_light(lid, "bri", bri) for lid in room.light_ids
            )
        else:
            ok = self._set_group(room.id, "on", True) and self._set_group(room.id, "bri", bri)
        if ok:
            with self._lock:
                for ls in self._room_lights(room):
                    ls.on = True
                    ls.brightness = bri
        return ok

    def activate_scene_by_name(self, room: str, scene: str) -> bool:
        room_obj = self.find_room(room)
        if not room_obj:
            return False
        scenes = self.load_scenes(room_obj.id)
        q = scene.strip().lower()
        match = next((s for s in scenes if s.name.lower() == q), None)
        if match is None:
            match = next((s for s in scenes if q and q in s.name.lower()), None)
        if match is None:
            return False
        self.activate_scene(match)
        return True

    # -- mock fixtures (dev) ----------------------------------------------
    def _load_mock(self) -> None:
        if self.lights:
            return  # load once
        self.connected = True
        spec = [
            ("Living room", [("Living room Credenza", True, 254), ("Living room Window", True, 254),
                             ("Living room 1", True, 70), ("Living room 2", True, 70),
                             ("Living room 3", True, 70), ("Living room 4", True, 70)]),
            ("Bedroom", [("Bedroom 2", False, 0), ("Bedroom 3", False, 0), ("Bedroom 1", False, 0)]),
            ("Dining", [("Dining 1", True, 200), ("Dining 2", True, 200)]),
            ("Kitchen", [("Kitchen 1", True, 254), ("Kitchen 2", True, 254)]),
            ("Hall", [("Hall", True, 160)]),
        ]
        # Mock colour variety: alternate ct/xy so both swatch styles are visible.
        ct_palette = [153, 250, 366, 450]  # mired: cool → warm
        xy_palette = [[0.675, 0.322], [0.408, 0.517], [0.167, 0.04], [0.45, 0.41]]
        lights: dict[int, Light] = {}
        rooms: list[Room] = []
        lid = 1
        for gid, (rname, ls) in enumerate(spec, start=1):
            ids = []
            for name, on, bri in ls:
                i = lid - 1
                lights[lid] = Light(
                    id=lid, name=name, on=on, brightness=bri, reachable=True,
                    light_type="Extended color light", modelid="LCT015",
                    swversion="1.122.2", colormode="xy" if i % 2 else "ct",
                    colortemp=ct_palette[i % len(ct_palette)],
                    xy=xy_palette[i % len(xy_palette)], hue=8597, saturation=121,
                    effect="none",
                )
                ids.append(lid)
                lid += 1
            rooms.append(Room(id=gid, name=rname, light_ids=ids))
        with self._lock:
            self.rooms, self.lights = rooms, lights
        self.info = BridgeInfo(name="Philips hue", model="BSB002",
                               swversion="1969113040", apiversion="1.65.0",
                               mac="ec:b5:fa:bf:6b:24")
        self.scenes_by_room = {1: [Scene("s1", "Relax", 1), Scene("s2", "Concentrate", 1),
                                   Scene("s3", "Energize", 1), Scene("s4", "Nightlight", 1)]}

    def _mock_config(self) -> dict:
        # Fixed-in-the-past clock so the drift path is always live under mock;
        # push_time() records _mock_now, letting a test see the drift clear.
        utc, local = "2026-07-21T13:00:00", "2026-07-21T09:00:00"
        if self._mock_now is not None:
            utc = self._mock_now.strftime("%Y-%m-%dT%H:%M:%S")
            local = self._mock_now.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M:%S")
        return {
            "name": "Philips hue", "modelid": "BSB002",
            "bridgeid": "ECB5FAFFFEBF6B24", "ipaddress": self.ip,
            "mac": "ec:b5:fa:bf:6b:24", "swversion": "1969113040",
            "apiversion": "1.65.0", "zigbeechannel": 25,
            "timezone": "America/New_York",
            "UTC": utc, "localtime": local,
            "swupdate2": {"bridge": {"state": "noupdates"}},
            "internetservices": {"internet": "connected"},
            "whitelist": {"a": {"name": "home-control#dev",
                                "last use date": "2026-06-16T12:00:00"}},
        }

    def _mock_group_action(self, gid: int) -> tuple[dict, dict]:
        room = self._room(gid)
        members = self._room_lights(room) if room else []
        any_on = any(ls.on for ls in members)
        avg = sum(ls.brightness for ls in members) // len(members) if members else 127
        action = {"on": any_on, "bri": avg, "ct": 366, "xy": [0.45, 0.41],
                  "colormode": "ct", "effect": "none"}
        state = {"any_on": any_on, "all_on": all(ls.on for ls in members) if members else False}
        return action, state


# ---------------------------------------------------------------------------
# Panel (curses)
# ---------------------------------------------------------------------------


class HueSystem(System):
    name = "Lighting"
    color_key = "hue"
    collapsed_height = 1

    def __init__(self):
        self.ctl = HueController()
        self.mode = "list"  # "list" | "scenes" | "device" | "sysinfo"
        self.cursor = 0
        self.scroll = 0
        self._items: list[tuple[str, object]] = []  # ("room", Room) | ("light", Light)
        # scenes sub-mode
        self.scenes: list[Scene] = []
        self.scene_room = ""
        self.scene_room_id = -1
        self.scene_cursor = 0
        # device dialog sub-mode (per-light or per-room full controls)
        self.info_fields: list[EditableField] = []
        self.info_read_only: list[tuple[str, str]] = []
        self.info_cursor = 0
        self.info_type = "light"  # "light" | "group"
        self.info_id = 0
        self.info_name = ""
        self._num_buf: str | None = None  # active typed-entry buffer, else None
        # system-info sub-mode (bridge config + lights + connected apps)
        self.sysinfo_lines: list[tuple[str, str]] = []  # (text, style)
        self.sysinfo_scroll = 0
        self._sysinfo_visible = 1  # visible row count, set each render for paging

    # -- lifecycle ---------------------------------------------------------
    def poll(self, focused: bool) -> None:
        self.ctl.poll()

    # -- collapsed ---------------------------------------------------------
    def collapsed_lines(self, width: int) -> list[Line]:
        if not self.ctl.connected:
            msg = self.ctl.error or "connecting..."
            return [[Seg(f"{self.ctl.ip}: {msg}", dim=True)]]
        # Badge mirrors the Router's "● ONLINE": accent colour, bold, on the left.
        badge = "● CONNECTED"
        line = pad_between(badge, self.ctl.summary, width)
        return [[Seg(badge, badge_color(BADGE_ACTIVE, self.color), bold=True),
                 Seg(line[len(badge):])]]

    # -- expanded ----------------------------------------------------------
    def _build_items(self, rooms: list[Room], lights: dict[int, Light]) -> None:
        items: list[tuple[str, object]] = []
        for room in rooms:
            items.append(("room", room))
            for lid in room.light_ids:
                if lid in lights:
                    items.append(("light", lights[lid]))
        self._items = items
        if self.cursor >= len(items):
            self.cursor = max(0, len(items) - 1)

    def render_expanded(self, region: Region) -> None:
        if self.mode == "sysinfo":
            self._render_sysinfo(region)
            return
        rooms, lights = self.ctl.snapshot()
        if not self.ctl.connected:
            if self.ctl.error:
                region.text(0, 0, f"Bridge unreachable ({self.ctl.ip})", "fault", bold=True)
                region.text_wrapped(1, 0, self.ctl.error, dim=True)
            else:
                region.text(0, 0, f"Connecting to {self.ctl.ip}...", dim=True)
            return

        region.segs(0, self.collapsed_lines(region.width)[0])
        region.text(1, 0, self._info_line(region.width))
        self._build_items(rooms, lights)

        top = 3  # rows 0-1 = overview, row 2 = blank separator
        visible = region.height - top
        if visible <= 0:
            return
        rows, focus = self._composite_rows(region.width)
        self._clamp_scroll_rows(len(rows), focus, visible)
        for i in range(visible):
            idx = self.scroll + i
            if idx >= len(rows):
                break
            region.segs(top + i, rows[idx])

    def _info_line(self, width: int) -> str:
        """Second expanded line: bridge hardware/firmware facts and the IP address."""
        model = bridge_model_label(self.ctl.info.model) or "Hue Bridge"
        return f"{model} ({self.ctl.ip})"[:width]

    def _clamp_scroll_rows(self, total: int, focus: int, visible: int) -> None:
        if focus < self.scroll:
            self.scroll = focus
        elif focus >= self.scroll + visible:
            self.scroll = focus - visible + 1
        self.scroll = max(0, min(self.scroll, max(0, total - visible)))

    def _room_row(self, room: Room, width: int, *, selected: bool, scenes_suffix: bool = False) -> Line:
        # Selection cue: an accent ▶ cursor + the whole row bolded.
        cur = cursor(self.color, selected)
        name = f"{room.name} — Scenes" if scenes_suffix else room.name
        left = [cur, Seg(name, self.color, bold=True)]
        right = [] if scenes_suffix else [Seg("←→ bri", self.color, dim=True)]
        line = justify(left, right, width)
        return highlight(line, self.color) if selected else line

    def _light_row(self, ls: Light, width: int, *, selected: bool) -> Line:
        cur = cursor(self.color, selected)
        # Columns: cursor(2) · name(24) · 4-space gap · brightness bar(BAR_WIDTH) ·
        # ● state · … · colour swatch + mode label (right-aligned).
        name = Seg(f"  {ls.name}"[:24].ljust(24))
        if not ls.reachable:
            # "unreachable" floats in the slider's lane; a lone ● sits in the
            # on/off dot column. Both grey, no colour swatch.
            line = [cur, name, Seg("    "),
                    Seg("unreachable".center(BAR_WIDTH), "muted"),
                    Seg("  "), Seg("●", "muted")]
            return highlight(line, self.color) if selected else line
        # On/off is the panel accent vs muted, not green vs red: an off lamp is
        # an ordinary state, and red made a dim room look like a failure.
        state_color = self.color if ls.on else "muted"
        left = [
            cur, name, Seg("    "),
            *brightness_bar(ls.brightness, ls.on, self.color),
            Seg("  "), Seg("● ", state_color), Seg("ON" if ls.on else "OFF", state_color),
        ]
        info = light_color(ls) if ls.on else None
        if info:
            rgb, label = info
            right = [Seg("●", rgb_color(*rgb)), Seg(f" {label}")]
            line = justify(left, right, width)
        else:
            line = left
        return highlight(line, self.color) if selected else line

    def _composite_rows(self, width: int) -> tuple[list[Line], int]:
        """Room/light rows for the main list, with the active scenes list or
        device dialog spliced in place of (scenes) or after (device) their
        target — so the rest of the room/light list stays visible around them."""
        rows: list[Line] = []
        focus = 0
        current_room_id: int | None = None
        for idx, (kind, obj) in enumerate(self._items):
            if kind == "room":
                current_room_id = obj.id  # type: ignore[attr-defined]
            if kind == "light" and self.mode == "scenes" and current_room_id == self.scene_room_id:
                continue  # this room's lights are replaced by its scene list below
            selected = self.mode == "list" and idx == self.cursor
            if kind == "room":
                suffix = self.mode == "scenes" and obj.id == self.scene_room_id  # type: ignore[attr-defined]
                row = self._room_row(obj, width, selected=selected, scenes_suffix=suffix)  # type: ignore[arg-type]
            else:
                row = self._light_row(obj, width, selected=selected)  # type: ignore[arg-type]
            if selected:
                focus = len(rows)
            rows.append(row)
            if kind == "room" and self.mode == "scenes" and obj.id == self.scene_room_id:  # type: ignore[attr-defined]
                extra, sfocus = self._scene_rows()
                focus = len(rows) + sfocus
                rows.extend(extra)
            is_target = self.mode == "device" and (
                (kind == "light" and self.info_type == "light" and obj.id == self.info_id) or  # type: ignore[attr-defined]
                (kind == "room" and self.info_type == "group" and obj.id == self.info_id)  # type: ignore[attr-defined]
            )
            if is_target:
                extra, dfocus = self._device_rows()
                focus = len(rows) + dfocus
                rows.extend(extra)
        return rows, focus

    def _scene_rows(self) -> tuple[list[Line], int]:
        if not self.scenes:
            return [[Seg("    (no scenes for this room)", dim=True)]], 0
        rows: list[Line] = []
        for i, sc in enumerate(self.scenes):
            sel = i == self.scene_cursor
            cur = cursor(self.color, sel)
            rows.append([cur, Seg(f"  {sc.name}", bold=sel)])
        return rows, self.scene_cursor

    # Nests device-dialog rows one level under their light/room row (which
    # itself indents its name 4 cols). The cursor takes 2 more, so field
    # names start at col 6; values right-align within a fixed-width name+value
    # field so they sit close to the name instead of flush against the box edge.
    _FIELD_INDENT = "      "
    _FIELD_WIDTH = 20

    def _device_rows(self) -> tuple[list[Line], int]:
        title = "Device Details" if self.info_type == "light" else "Room Details"
        rows: list[Line] = [[Seg(f"{self._FIELD_INDENT}{title}", self.color, bold=True)]]
        for i, fld in enumerate(self.info_fields):
            sel = i == self.info_cursor
            # Selection cue matches the main list/scenes rows: an accent ▶
            # cursor + bold (default-colour) text, not a solid accent fill.
            cur = cursor(self.color, sel)
            field = pad_between(fld.name, self._field_value(fld, sel), self._FIELD_WIDTH)
            rows.append([Seg(self._FIELD_INDENT), cur, Seg(field, bold=sel)])
        focus = self.info_cursor + 1  # +1 for the title row above the fields
        for label, val in self.info_read_only:
            rows.append([Seg(f"{self._FIELD_INDENT}{label}: {val}", dim=True)])
        return rows, focus

    # -- device dialog -----------------------------------------------------
    def _build_light_fields(self, ls: Light) -> None:
        fields: list[EditableField] = [
            EditableField("on", "on", ls.on, field_type="bool"),
            EditableField("brightness", "bri", ls.brightness, 1, 254, 25, "int"),
        ]
        modes = _supported_colormodes(ls.xy is not None, ls.colortemp is not None)
        if len(modes) > 1:
            fields.append(EditableField("colormode", "colormode", ls.colormode or modes[0],
                                        step=modes, field_type="enum"))
        cm = ls.colormode
        if cm == "ct" and ls.colortemp is not None:
            fields.append(self._ct_field(ls.colortemp))
        elif cm == "xy" and ls.xy is not None:
            fields += self._xy_fields(ls.xy)
        elif cm == "hs":
            if ls.hue is not None:
                fields.append(EditableField("hue", "hue", ls.hue, 0, 65535, 1000, "int"))
            if ls.saturation is not None:
                fields.append(EditableField("saturation", "sat", ls.saturation, 0, 254, 25, "int"))
        elif ls.colortemp is not None:  # ct-capable light with no colormode set
            fields.append(self._ct_field(ls.colortemp))
        if ls.effect is not None:
            fields.append(EditableField("effect", "effect", ls.effect, field_type="enum"))
        self.info_fields = fields
        self.info_read_only = [
            ("Type", ls.light_type or "?"), ("Model", ls.modelid or "?"),
            ("Firmware", ls.swversion or "?"),
            ("Reachable", "yes" if ls.reachable else "no"),
        ]
        self.info_cursor = 0
        self.info_type, self.info_id, self.info_name = "light", ls.id, ls.name

    def _build_group_fields(self, room: Room) -> None:
        action, gstate = self.ctl.group_action(room.id)
        fields: list[EditableField] = [
            EditableField("on", "on", action.get("on", False), field_type="bool"),
            EditableField("brightness", "bri", action.get("bri", 127), 1, 254, 25, "int"),
        ]
        modes = _supported_colormodes("xy" in action, "ct" in action)
        cm = action.get("colormode")
        if len(modes) > 1:
            fields.append(EditableField("colormode", "colormode", cm or modes[0],
                                        step=modes, field_type="enum"))
        if cm == "ct" and "ct" in action:
            fields.append(self._ct_field(action["ct"]))
        elif cm == "xy" and "xy" in action:
            fields += self._xy_fields(action["xy"])
        elif cm == "hs":
            if "hue" in action:
                fields.append(EditableField("hue", "hue", action["hue"], 0, 65535, 1000, "int"))
            if "sat" in action:
                fields.append(EditableField("saturation", "sat", action["sat"], 0, 254, 25, "int"))
        elif "ct" in action:
            fields.append(self._ct_field(action["ct"]))
        if "effect" in action:
            fields.append(EditableField("effect", "effect", action["effect"], field_type="enum"))
        self.info_fields = fields
        self.info_read_only = [
            ("Lights", str(len(room.light_ids))),
            ("Any on", "yes" if gstate.get("any_on") else "no"),
            ("All on", "yes" if gstate.get("all_on") else "no"),
        ]
        self.info_cursor = 0
        self.info_type, self.info_id, self.info_name = "group", room.id, room.name

    @staticmethod
    def _ct_field(mired: int) -> EditableField:
        """A `ct` field whose displayed/edited unit is Kelvin (1e6 / mired)."""
        return EditableField("colortemp", "ct", round(1_000_000 / mired),
                             CT_MIN_K, CT_MAX_K, 100, "int")

    @staticmethod
    def _xy_fields(xy: list[float]) -> list[EditableField]:
        return [EditableField("x", "x", xy[0], 0.0, 1.0, 0.01, "float"),
                EditableField("y", "y", xy[1], 0.0, 1.0, 0.01, "float")]

    def _field_value(self, fld: EditableField, sel: bool) -> str:
        if self._num_buf is not None and sel and fld.field_type in ("int", "float"):
            return self._num_buf + "_"
        if fld.field_type == "bool":
            return "on" if fld.value else "off"
        if fld.field_type == "float":
            return f"{fld.value:.4f}"
        if fld.api_key == "ct":
            return f"{fld.value}K"
        return str(fld.value)

    # -- system info -------------------------------------------------------
    def _build_sysinfo_lines(self) -> list[tuple[str, str]]:
        cfg = self.ctl.full_config()
        _, lights = self.ctl.snapshot()
        lines: list[tuple[str, str]] = [("BRIDGE", "header")]

        def kv(k: str, v: object, style: str = "") -> None:
            lines.append((f"  {k:<16}{v}", style))

        kv("Name", cfg.get("name", "?"))
        kv("Model", cfg.get("modelid", "?"))
        kv("Bridge ID", cfg.get("bridgeid", "?"))
        kv("IP", cfg.get("ipaddress", self.ctl.ip))
        kv("MAC", cfg.get("mac", "?"))
        kv("Firmware", cfg.get("swversion", "?"))
        kv("API version", cfg.get("apiversion", "?"))
        kv("ZigBee channel", cfg.get("zigbeechannel", "?"))
        kv("Timezone", cfg.get("timezone", "?"))

        utc_str = cfg.get("UTC", "")
        out_of_sync = False
        if utc_str:
            try:
                bridge_utc = datetime.fromisoformat(utc_str).replace(tzinfo=UTC)
                out_of_sync = abs((datetime.now(UTC) - bridge_utc).total_seconds()) > CLOCK_DRIFT_WARN
            except ValueError:
                pass
        # A drift pairs two alert rows in the same red: the local-time row
        # names the fault ("out of sync?"), the UTC row offers the fix.
        drift_badge = " (out of sync?)" if out_of_sync else ""
        sync_hint = " ('s' to sync local host time)" if out_of_sync else ""
        alert = "alert" if out_of_sync else ""
        kv("Local time", cfg.get("localtime", "?").replace("T", " ") + drift_badge, alert)
        kv("UTC time", (utc_str or "?").replace("T", " ") + sync_hint, alert)
        kv("Updates", cfg.get("swupdate2", {}).get("bridge", {}).get("state", "?"))
        kv("Internet", cfg.get("internetservices", {}).get("internet", "?"))

        ordered = sorted(lights.values(), key=lambda ls: ls.name.lower())
        lines.append(("", ""))
        lines.append((f"LIGHTS ({len(ordered)})", "header"))
        for ls in ordered:
            reach = "reachable" if ls.reachable else "unreachable"
            name = ls.name[:20].ljust(20)
            model = (ls.modelid or "?")[:8].ljust(8)
            sw = (f"v{ls.swversion}" if ls.swversion else "?").ljust(10)
            lines.append((f"  {name}  {model}  {sw}  {reach}", "" if ls.reachable else "unreachable"))

        apps = []
        for _wid, wd in cfg.get("whitelist", {}).items():
            last = wd.get("last use date", "?")
            if last and "T" in last:
                last = last.split("T")[0]
            apps.append((wd.get("name", "?"), last))
        apps.sort(key=lambda x: x[1], reverse=True)
        if apps:
            lines.append(("", ""))
            lines.append((f"CONNECTED APPS ({len(apps)})", "header"))
            for name, last in apps:
                lines.append((f"  {name[:30].ljust(30)}  {last}", ""))
        return lines

    def _render_sysinfo(self, region: Region) -> None:
        region.text(0, 0, "Bridge Info", self.color, bold=True)
        top = 2
        visible = region.height - top
        if visible <= 0:
            return
        self._sysinfo_visible = visible  # remembered for PgUp/PgDn paging
        total = len(self.sysinfo_lines)
        self.sysinfo_scroll = max(0, min(self.sysinfo_scroll, max(0, total - visible)))
        for i in range(visible):
            idx = self.sysinfo_scroll + i
            if idx >= total:
                break
            text, style = self.sysinfo_lines[idx]
            if style == "header":
                region.text(top + i, 0, text, self.color, bold=True)
            elif style == "unreachable":
                region.text(top + i, 0, text, "muted")
            elif style == "alert":
                # A drifted bridge clock silently misfires every schedule the
                # bridge runs, so it earns "fault" red rather than "warn" amber.
                region.text(top + i, 0, text, "fault")
            else:
                region.text(top + i, 0, text)

    def _list_toolbar_hints(self) -> Line:
        return hint_row(
            hint("↕", "nav", self.color),
            hint("←→", "bri", self.color),
            hint("ENTER", "on/off", self.color),
            hint("s", "scenes", self.color, paren=True),
            hint("d", "details", self.color, paren=True),
            hint("b", "bridge info", self.color, paren=True),
        )

    def _scenes_toolbar_hints(self) -> Line:
        return hint_row(
            hint("↕", "nav", self.color),
            hint("ENTER", "activate", self.color),
            hint("s/ESC", "back", self.color),
        )

    def _device_toolbar_hints(self) -> Line:
        if self._num_buf is not None:
            return hint_row(
                [Seg("type value", self.color)],
                hint("ENTER", "set", self.color),
                hint("ESC", "cancel", self.color),
            )
        return hint_row(
            hint("↕", "nav", self.color),
            hint("←→", "adjust", self.color),
            hint("ENTER", "edit/toggle", self.color),
            hint("d/ESC", "back", self.color),
        )

    def _sysinfo_toolbar_hints(self) -> Line:
        return hint_row(
            hint("↕/PgUp/PgDn", "scroll", self.color),
            hint("s", "sync time", self.color, paren=True),
            hint("b/ESC", "back", self.color),
        )

    def toolbar_line(self) -> Line | None:
        if self.mode == "scenes":
            return self._scenes_toolbar_hints()
        if self.mode == "device":
            return self._device_toolbar_hints()
        if self.mode == "sysinfo":
            return self._sysinfo_toolbar_hints()
        return self._list_toolbar_hints()

    def help_notes(self) -> list[str]:
        # Keep in sync with the Lighting entry in README.md "Device support".
        return [
            "Controls a Philips Hue bridge via the phue2 library for on/off, "
            "brightness, colour, scenes, and per-room or per-light control, "
            "plus direct HTTPS calls to Hue's CLIP v2 API for the dynamic "
            "effects (candle, fire, prism; plain colorloop is the older v1 "
            "API).",
            "Config: [hue] bridge_ip points at the bridge. The first "
            "connection has to be authorized by pressing the physical link "
            "button on the bridge — the panel will prompt you — after which "
            "the credential is cached and reconnects are automatic.",
        ]

    # -- input -------------------------------------------------------------
    def handle_key(self, key: int) -> bool:
        if self.mode == "scenes":
            return self._handle_scenes_key(key)
        if self.mode == "device":
            return self._handle_device_key(key)
        if self.mode == "sysinfo":
            return self._handle_sysinfo_key(key)
        return self._handle_list_key(key)

    def _handle_list_key(self, key: int) -> bool:
        if key in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(len(self._items) - 1, self.cursor + 1)
        elif key == curses.KEY_PPAGE:
            self.cursor = max(0, self.cursor - 5)
        elif key == curses.KEY_NPAGE:
            self.cursor = min(len(self._items) - 1, self.cursor + 5)
        elif key in (curses.KEY_LEFT, ord("h")):
            self._adjust(-1)
        elif key in (curses.KEY_RIGHT, ord("l")):
            self._adjust(1)
        elif key in (ord("\n"), curses.KEY_ENTER):
            self._toggle()
        elif key == ord("s"):
            self._open_scenes()
        elif key == ord("d"):
            self._open_device()
        elif key == ord("b"):
            self._open_sysinfo()
        else:
            return False
        return True

    def _current(self) -> tuple[str, object] | None:
        if 0 <= self.cursor < len(self._items):
            return self._items[self.cursor]
        return None

    def _adjust(self, direction: int) -> None:
        cur = self._current()
        if not cur:
            return
        kind, obj = cur
        if kind == "light":
            self.ctl.adjust_light(obj.id, direction)  # type: ignore[attr-defined]
        else:
            self.ctl.adjust_room(obj.id, direction)  # type: ignore[attr-defined]

    def _toggle(self) -> None:
        cur = self._current()
        if not cur:
            return
        kind, obj = cur
        if kind == "light":
            self.ctl.toggle_light(obj.id)  # type: ignore[attr-defined]
        else:
            self.ctl.toggle_room(obj.id)  # type: ignore[attr-defined]

    def _open_scenes(self) -> None:
        cur = self._current()
        if not cur:
            return
        kind, obj = cur
        room_id = obj.id if kind == "room" else self._room_of_light(obj.id)  # type: ignore[attr-defined]
        if room_id is None:
            return
        self.scenes = self.ctl.load_scenes(room_id)
        self.scene_room = next((r.name for r in self.ctl.rooms if r.id == room_id), "")
        self.scene_room_id = room_id
        self.scene_cursor = 0
        self.mode = "scenes"

    def _room_of_light(self, lid: int) -> int | None:
        return next((r.id for r in self.ctl.rooms if lid in r.light_ids), None)

    # -- device dialog input ----------------------------------------------
    def _open_device(self) -> None:
        cur = self._current()
        if not cur:
            return
        kind, obj = cur
        if kind == "light":
            self._build_light_fields(obj)  # type: ignore[arg-type]
        else:
            room: Room = obj  # type: ignore[assignment]
            if room.id == -1:  # synthetic "Other" room — no real group endpoint
                self.set_status("No device info for ‘Other’")
                return
            self._build_group_fields(room)
        self._num_buf = None
        self.mode = "device"

    def _handle_device_key(self, key: int) -> bool:
        if self._num_buf is not None:
            return self._handle_num_entry(key)
        if not self.info_fields:
            if key in (27, ord("q"), ord("d")):
                self.mode = "list"
                return True
            return False
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
        elif key in (27, ord("q"), ord("d")):
            self._close_device()
        else:
            return False
        return True

    def _device_step(self, direction: int) -> None:
        fld = self.info_fields[self.info_cursor]
        if fld.field_type == "bool":
            fld.value = not fld.value
            self._apply(fld)
        elif fld.field_type in ("int", "float"):
            if fld.api_key == "bri":
                new = next_bri(fld.value, direction)
                if new is None:  # stepped below minimum → turn the device off
                    on = next((f for f in self.info_fields if f.api_key == "on"), None)
                    if on:
                        on.value = False
                        self._apply(on)
                else:
                    fld.value = new
                    self._apply(fld)
            else:
                nv = fld.value + direction * fld.step
                nv = min(fld.max_val, max(fld.min_val, nv))
                fld.value = round(nv, 4) if fld.field_type == "float" else nv
                self._apply(fld)
        elif fld.field_type == "enum":
            fld.value = self._cycle_enum(fld, direction)
            self._apply(fld)

    def _device_enter(self) -> None:
        fld = self.info_fields[self.info_cursor]
        if fld.field_type == "bool":
            fld.value = not fld.value
            self._apply(fld)
        elif fld.field_type in ("int", "float"):
            self._num_buf = ""  # begin inline typed entry
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
        return True  # capture all keys while typing

    def _cycle_enum(self, fld: EditableField, direction: int) -> Any:
        if fld.api_key == "effect":
            cycle = ["none", "colorloop"] if self.info_type == "group" else EFFECT_CYCLE
        else:  # colormode — list lives in fld.step
            cycle = fld.step or []
        if not cycle:
            return fld.value
        try:
            idx = cycle.index(fld.value)
        except ValueError:
            return cycle[0]
        return cycle[(idx + direction) % len(cycle)]

    def _apply(self, fld: EditableField) -> None:
        if self.info_type == "light":
            self.ctl.apply_light_edit(self.info_id, fld, self.info_fields)
        else:
            self.ctl.apply_group_edit(self.info_id, fld, self.info_fields)
        if fld.api_key == "colormode":  # available color fields change with mode
            self._rebuild_fields()

    def _rebuild_fields(self) -> None:
        old = self.info_cursor
        if self.info_type == "light":
            ls = self.ctl.lights.get(self.info_id)
            if ls:
                self._build_light_fields(ls)
        else:
            room = next((r for r in self.ctl.rooms if r.id == self.info_id), None)
            if room:
                self._build_group_fields(room)
        for i, f in enumerate(self.info_fields):
            if f.api_key == "colormode":
                self.info_cursor = i
                break
        else:
            self.info_cursor = min(old, max(0, len(self.info_fields) - 1))

    def _close_device(self) -> None:
        self._num_buf = None
        if self.info_type == "light":
            self.ctl.refresh_light(self.info_id)
        else:
            room = next((r for r in self.ctl.rooms if r.id == self.info_id), None)
            if room:
                for lid in room.light_ids:
                    self.ctl.refresh_light(lid)
        self.mode = "list"

    # -- system info input -------------------------------------------------
    def _open_sysinfo(self) -> None:
        self.sysinfo_lines = self._build_sysinfo_lines()
        self.sysinfo_scroll = 0
        self.mode = "sysinfo"

    def _handle_sysinfo_key(self, key: int) -> bool:
        page = max(1, self._sysinfo_visible - 1)  # keep one line of context
        if key in (curses.KEY_UP, ord("k")):
            self.sysinfo_scroll = max(0, self.sysinfo_scroll - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.sysinfo_scroll += 1
        elif key == curses.KEY_PPAGE:
            self.sysinfo_scroll = max(0, self.sysinfo_scroll - page)
        elif key == curses.KEY_NPAGE:
            self.sysinfo_scroll += page
        elif key == ord("s"):
            self._push_time()
        elif key in (ord("b"), 27, ord("q")):
            self.mode = "list"
        else:
            return False
        return True

    def _push_time(self) -> None:
        ok = self.ctl.push_time()
        self.set_status("Bridge clock synced to host" if ok else "Clock sync failed")
        self.sysinfo_lines = self._build_sysinfo_lines()  # re-poll config to reflect it

    # -- voice -------------------------------------------------------------
    def voice_actions(self) -> list[VoiceAction]:
        return [
            VoiceAction(
                name="lights_power",
                description="Turn lights on or off in a room, or in the whole home if no room is given.",
                parameters={
                    "room": {"type": "string", "description": "Room name, e.g. 'Kitchen'. Omit for all rooms."},
                    "on": {"type": "boolean", "description": "true to turn on, false to turn off."},
                },
                required=["on"],
                handler=self._voice_power,
            ),
            VoiceAction(
                name="lights_brightness",
                description="Set the brightness of a room's lights to a percentage (0-100).",
                parameters={
                    "room": {"type": "string", "description": "Room name."},
                    "percent": {"type": "integer", "description": "Brightness 0-100."},
                },
                required=["room", "percent"],
                handler=self._voice_brightness,
            ),
            VoiceAction(
                name="lights_scene",
                description="Activate a lighting scene in a room (e.g. Relax, Concentrate, Energize).",
                parameters={
                    "room": {"type": "string", "description": "Room name."},
                    "scene": {"type": "string", "description": "Scene name."},
                },
                required=["room", "scene"],
                handler=self._voice_scene,
            ),
        ]

    def voice_context(self) -> str:
        names = self.ctl.room_names()
        return f"Lighting rooms: {', '.join(names)}" if names else ""

    def _voice_power(self, args: dict) -> str:
        on = bool(args.get("on"))
        room = args.get("room")
        if room:
            ok = self.ctl.set_room_power(room, on)
            return f"{'Turned on' if on else 'Turned off'} {room}" if ok else f"No room '{room}'"
        ok = self.ctl.set_all_power(on)
        return f"All lights {'on' if on else 'off'}" if ok else "No lights to control"

    def _voice_brightness(self, args: dict) -> str:
        room = args.get("room", "")
        pct = int(args.get("percent", 0))
        if self.ctl.set_room_brightness(room, pct):
            return f"{room} set to {pct}%"
        return f"Couldn't set brightness for '{room}'"

    def _voice_scene(self, args: dict) -> str:
        room = args.get("room", "")
        scene = args.get("scene", "")
        if self.ctl.activate_scene_by_name(room, scene):
            self.set_status(f"Scene: {scene}")
            return f"Activated '{scene}' in {room}"
        return f"No scene '{scene}' in {room}"

    def _handle_scenes_key(self, key: int) -> bool:
        if key in (curses.KEY_UP, ord("k")):
            self.scene_cursor = max(0, self.scene_cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.scene_cursor = min(len(self.scenes) - 1, self.scene_cursor + 1)
        elif key in (ord("\n"), curses.KEY_ENTER):
            if self.scenes:
                scene = self.scenes[self.scene_cursor]
                self.ctl.activate_scene(scene)
                self.set_status(f"Scene: {scene.name}")
            self.mode = "list"
        elif key in (ord("s"), 27, ord("q")):
            self.mode = "list"
        else:
            return False
        return True
