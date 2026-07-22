"""Sonos panel, ported from standalone-apps/sonos/sonos-control/.

Split into:
  * SonosController — SoCo discovery, polling, and commands (transport, volume,
    grouping, queue, Sonos Favorites). Lazy `soco` import; lock-guarded so the
    poll thread and main-thread commands share state safely. Mock fixtures via
    HOME_CONTROL_MOCK=1.
  * SonosSystem    — the curses panel: collapsed 2-line summary; expanded main
    view (speaker list + now-playing detail) with queue / favorites / group
    sub-modes.

Deferred from the original (network/auth-heavy and untestable without the LAN):
iTunes library browsing + local HTTP streaming server, YouTube Music search, and
the device-info EQ editing overlay. Hooks left as TODOs.

Note: SoCo paths cannot be exercised here (no devices / no soco). They're ported
faithfully from the working standalone app; verification covered the panel via
mock data only.
"""

from __future__ import annotations

import curses
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from .. import config
from ..ui import (
    BADGE_ACTIVE,
    BADGE_IDLE,
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
    select_row,
    toggle_dot,
)
from .base import Popup, System

# SoCo's default REQUEST_TIMEOUT is 20s. On a healthy LAN a speaker answers in
# milliseconds, but when a speaker responds to SSDP discovery yet its control
# port (1400) isn't reachable — e.g. the laptop on WiFi, the speakers wired —
# every SOAP call blocks for the full timeout. Fail fast instead (matches Hue's
# CONNECT_TIMEOUT) so a background retry is cheap, not a multi-second wedge.
SONOS_REQUEST_TIMEOUT = 4

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TrackInfo:
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: str = "0:00:00"
    position: str = "0:00:00"
    queue_index: int = 0  # 1-based


@dataclass
class ZoneState:
    name: str
    transport_state: str = "STOPPED"  # PLAYING | PAUSED_PLAYBACK | TRANSITIONING | STOPPED
    volume: int = 0
    muted: bool = False
    grouped: bool = False
    queue_size: int = 0
    track: TrackInfo | None = None
    shuffle: bool = False
    repeat: bool = False
    cross_fade: bool = False


@dataclass
class QueueItem:
    index: int
    title: str
    artist: str


@dataclass
class FavoriteItem:
    title: str
    uri: str
    metadata: str
    description: str


@dataclass
class DeviceField:
    """One row in the device-info / EQ overlay."""

    label: str
    value: Any
    kind: str            # "info" | "bool" | "int" | "sep" | "desc"
    attr: str = ""       # SoCo property name to set (or special: "balance" | "sleep")
    min_val: int = 0
    max_val: int = 0
    step: int = 1
    unit: str = ""       # display suffix for ints, e.g. "K", " min"

    @property
    def editable(self) -> bool:
        return self.kind in ("bool", "int")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_BADGES = {
    "PLAYING": ("▶ PLAYING", BADGE_ACTIVE),
    "PAUSED_PLAYBACK": ("⏸ PAUSED", BADGE_IDLE),
    "TRANSITIONING": ("⟳ LOADING", BADGE_ACTIVE),
    "STOPPED": ("■ STOPPED", BADGE_IDLE),
}


def badge(transport_state: str) -> tuple[str, str]:
    """(label, badge state) for a zone; `ui.badge_color` turns the state into a
    color. A transitioning zone counts as active — it is on its way to playing."""
    return _BADGES.get(transport_state, ("■ STOPPED", BADGE_IDLE))


# Width of the widest badge label ("▶ PLAYING" / "■ STOPPED" / "⟳ LOADING"),
# so the state column lines up across the independent per-speaker rows.
BADGE_W = max(len(text) for text, _ in _BADGES.values())


def _fully_grouped(zones: list[ZoneState]) -> bool:
    """True when every speaker is joined into a group — the case the 2-line
    grouped summary describes. Any standalone speaker → show independent rows."""
    return len(zones) > 1 and all(z.grouped for z in zones)


def _parse_speakers(raw: Any) -> list[tuple[str, str | None]]:
    """Parse ``[sonos] speakers`` into (ip, name_override) pairs, in config order.

    Each entry is a table ``{ ip = "1.2.3.4", name = "Kitchen" }``; ``name`` is
    optional. Entries without an ``ip`` are skipped. Non-list input yields []."""
    out: list[tuple[str, str | None]] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        ip = str(entry.get("ip", "")).strip()
        if not ip:
            continue
        name = entry.get("name")
        out.append((ip, str(name) if name else None))
    return out


def trunc(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return "." * width
    return text[: width - 3] + "..."


def volume_bar(vol: int, color: str = "", width: int = 12) -> Line:
    """The shared `ui.level_bar` over a 0-100 volume percentage."""
    return level_bar(vol, 100, color, width)


def _balance_to_pct(dev) -> int:
    """SoCo balance is a (left, right) 0..100 tuple; collapse to -100..+100."""
    try:
        left, right = dev.balance
        return max(-100, min(100, int(right) - int(left)))
    except Exception:  # noqa: BLE001
        return 0


def _pct_to_balance(pct: int) -> tuple[int, int]:
    """Inverse of _balance_to_pct: keep the louder side at 100."""
    pct = max(-100, min(100, pct))
    if pct >= 0:
        return (100 - pct, 100)
    return (100, 100 + pct)


def _int_slider(value: int, lo: int, hi: int, width: int = 15) -> str:
    """A ──────●────── slider with a centre tick when the range straddles zero."""
    span = max(1, hi - lo)
    pos = max(0, min(width - 1, round((value - lo) / span * (width - 1))))
    chars = ["─"] * width
    if lo < 0 < hi:
        zero = round((0 - lo) / span * (width - 1))
        if 0 <= zero < width and zero != pos:
            chars[zero] = "│"
    chars[pos] = "●"
    return "[" + "".join(chars) + "]"


# ---------------------------------------------------------------------------
# Controller (no curses)
# ---------------------------------------------------------------------------


class SonosController:
    def __init__(self):
        self._lock = threading.RLock()
        self._devices: list = []  # list[soco.SoCo]
        self.zones: list[ZoneState] = []
        self.active_idx = 0
        self.discovered = False
        self.error = ""
        self.status = ""
        self.queue_items: list[QueueItem] = []
        self.favorites: list[FavoriteItem] = []
        self.mock = os.environ.get("HOME_CONTROL_MOCK") == "1"
        # Pinned speakers (ip, name_override) skip SSDP; empty → auto-discover.
        self._pinned = _parse_speakers(config.get("sonos", "speakers", []))
        self._name_overrides = {ip: name for ip, name in self._pinned if name}
        # Household speakers seen in topology but absent from the pinned list.
        self._new_devices: list[str] = []
        self._fast_tick = 0

    # -- polling (background thread) ---------------------------------------
    def poll(self, focused: bool) -> None:
        if self.mock:
            self._load_mock()
            return
        if not self.discovered and not self._discover():
            return
        # Full refresh every ~5 cycles; otherwise a light refresh of the active zone.
        self._fast_tick += 1
        if self._fast_tick >= 5 or not focused:
            self._poll_all()
            self._fast_tick = 0
        else:
            self._poll_active_fast()

    def _discover(self) -> bool:
        try:
            import soco
            import soco.config  # lazy: app runs without soco installed
        except ImportError:
            self.error = "soco not installed"
            return False
        soco.config.REQUEST_TIMEOUT = SONOS_REQUEST_TIMEOUT  # fail fast, see constant
        if self._pinned:
            return self._connect_pinned(soco)
        try:
            raw = list(soco.discover(timeout=2) or [])
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return False
        if not raw:
            self.error = "no Sonos devices found"
            return False
        # Resolve ordering OUTSIDE the lock: each d.player_name is a live SoCo
        # property that fires a SOAP request, so holding self._lock across it would
        # block the main thread's snapshot() — and thus every render frame — for the
        # whole request timeout. The poll-thread contract is: never hold the lock
        # during network I/O.
        ordered = self._apply_order(raw)
        with self._lock:
            self._devices = ordered
            self.discovered = True
            self.error = ""
        self._poll_all()
        return True

    def _connect_pinned(self, soco) -> bool:
        """Build SoCo handles straight from the pinned IPs — no SSDP sweep.

        soco.SoCo(ip) does no network I/O, so this is instant; the first
        _poll_all() below does the real (per-device, millisecond) SOAP work.
        Display order is the pinned order, so no player_name round-trips here."""
        devices = []
        for ip, _ in self._pinned:
            try:
                devices.append(soco.SoCo(ip))
            except Exception:  # noqa: BLE001
                continue
        if not devices:
            self.error = "no pinned Sonos speakers reachable"
            return False
        self._detect_unpinned(devices)
        with self._lock:
            self._devices = devices
            self.discovered = True
            self.error = ""
        self._poll_all()
        return True

    def _detect_unpinned(self, devices: list) -> None:
        """Flag household speakers missing from the pinned list. One speaker's
        zone-group topology names every speaker on the network, so a single
        visible_zones query (outside the lock) surfaces anything unpinned."""
        pinned_ips = {ip for ip, _ in self._pinned}
        for seed in devices:
            try:
                zones = list(seed.visible_zones)
            except Exception:  # noqa: BLE001
                continue
            found = []
            for z in zones:
                ip = getattr(z, "ip_address", "")
                if ip and ip not in pinned_ips:
                    found.append(getattr(z, "player_name", None) or ip)
            with self._lock:
                self._new_devices = sorted(found)
            return

    def _display_name(self, device) -> str:
        """The name to show for a device: the config override if pinned with one,
        else the speaker's own Sonos room name (never raises)."""
        ip = getattr(device, "ip_address", "")
        if ip in self._name_overrides:
            return self._name_overrides[ip]
        try:
            return device.player_name
        except Exception:  # noqa: BLE001
            return "Unknown"

    # -- pinning / new-device accessors ------------------------------------
    def pinned_count(self) -> int:
        return len(self._pinned)

    @property
    def new_devices(self) -> list[str]:
        with self._lock:
            return list(self._new_devices)

    def ack_new_devices(self) -> None:
        with self._lock:
            self._new_devices = []

    def _apply_order(self, devices: list) -> list:
        """Sort auto-discovered devices alphabetically by room name (pin speakers
        in [sonos] speakers to control the order instead)."""
        return sorted(devices, key=lambda d: d.player_name)

    def _coordinator(self, device):
        try:
            group = device.group
            if group:
                return group.coordinator
        except Exception:  # noqa: BLE001
            pass
        return device

    def _poll_all(self) -> None:
        zones = [self._read_zone(d) for d in self._devices]
        with self._lock:
            self.zones = zones
            if self.active_idx >= len(zones):
                self.active_idx = 0

    def _read_zone(self, device) -> ZoneState:
        try:
            coord = self._coordinator(device)
            transport = coord.get_current_transport_info()
            ti = coord.get_current_track_info()
            track = None
            if ti.get("title") or ti.get("artist"):
                try:
                    qidx = int(ti.get("playlist_position", 0))
                except (ValueError, TypeError):
                    qidx = 0
                track = TrackInfo(
                    title=ti.get("title", ""), artist=ti.get("artist", ""),
                    album=ti.get("album", ""), duration=ti.get("duration", "0:00:00"),
                    position=ti.get("position", "0:00:00"), queue_index=qidx,
                )
            try:
                queue_size = len(device.get_queue())
            except Exception:  # noqa: BLE001
                queue_size = 0
            try:
                grouped = len(device.group.members) > 1
            except Exception:  # noqa: BLE001
                grouped = False
            # Play modes live on the coordinator (they describe the shared queue).
            try:
                shuffle, repeat, cross_fade = coord.shuffle, coord.repeat, coord.cross_fade
            except Exception:  # noqa: BLE001
                shuffle = repeat = cross_fade = False
            return ZoneState(
                name=self._display_name(device),
                transport_state=transport.get("current_transport_state", "STOPPED"),
                volume=device.volume, muted=device.mute,
                grouped=grouped, queue_size=queue_size, track=track,
                shuffle=shuffle, repeat=repeat, cross_fade=cross_fade,
            )
        except Exception:  # noqa: BLE001
            return ZoneState(name=self._display_name(device))

    def _poll_active_fast(self) -> None:
        """Light refresh of just the active zone's transport + track."""
        with self._lock:
            idx = self.active_idx
            if not (0 <= idx < len(self._devices)):
                return
            device = self._devices[idx]
        try:
            coord = self._coordinator(device)
            transport = coord.get_current_transport_info()
            ti = coord.get_current_track_info()
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            if not (0 <= idx < len(self.zones)):
                return
            zone = self.zones[idx]
            zone.transport_state = transport.get("current_transport_state", zone.transport_state)
            title, artist = ti.get("title", ""), ti.get("artist", "")
            if title or artist:
                if zone.track is None:
                    zone.track = TrackInfo()
                zone.track.title = title or zone.track.title
                zone.track.artist = artist or zone.track.artist
                zone.track.album = ti.get("album", "") or zone.track.album
                zone.track.position = ti.get("position", zone.track.position)
                zone.track.duration = ti.get("duration", zone.track.duration)
            else:
                zone.track = None

    # -- snapshot ----------------------------------------------------------
    def snapshot(self) -> tuple[list[ZoneState], int]:
        with self._lock:
            return list(self.zones), self.active_idx

    @property
    def active_zone(self) -> ZoneState | None:
        with self._lock:
            if 0 <= self.active_idx < len(self.zones):
                return self.zones[self.active_idx]
        return None

    def _active_device(self):
        if 0 <= self.active_idx < len(self._devices):
            return self._devices[self.active_idx]
        return None

    # -- navigation --------------------------------------------------------
    def select(self, delta: int) -> None:
        with self._lock:
            if self.zones:
                self.active_idx = max(0, min(len(self.zones) - 1, self.active_idx + delta))

    # -- commands (main thread) -------------------------------------------
    def _call(self, fn, *a) -> bool:
        if self.mock:
            return True
        try:
            with self._lock:
                fn(*a)
            return True
        except Exception as e:  # noqa: BLE001
            # SoCoUPnPException 701 (transition not available) is benign; ignore label.
            self.error = str(e)
            return False

    def play_pause(self) -> None:
        zone, dev = self.active_zone, self._active_device()
        if self.mock:
            if zone:
                zone.transport_state = "PAUSED_PLAYBACK" if zone.transport_state == "PLAYING" else "PLAYING"
            return
        if dev is None or zone is None:
            return
        coord = self._coordinator(dev)
        self._call(coord.pause if zone.transport_state == "PLAYING" else coord.play)
        self._refresh_active_after()

    def stop(self) -> None:
        dev = self._active_device()
        if dev is not None:
            self._call(self._coordinator(dev).stop)
            self._refresh_active_after()

    def next_track(self) -> None:
        dev = self._active_device()
        if dev is not None:
            self._call(self._coordinator(dev).next)
            self._refresh_active_after()

    def prev_track(self) -> None:
        dev = self._active_device()
        if dev is not None:
            self._call(self._coordinator(dev).previous)
            self._refresh_active_after()

    def _refresh_active_after(self) -> None:
        if not self.mock:
            time.sleep(0.3)
            self._poll_active_fast()

    def set_volume(self, delta: int) -> None:
        zone, dev = self.active_zone, self._active_device()
        if zone is None:
            return
        new_vol = max(0, min(100, zone.volume + delta))
        if self.mock or dev is None:
            zone.volume = new_vol
            return
        try:
            with self._lock:
                # Relative, server-side clamp — avoids a read/write race in groups.
                dev.set_relative_volume(delta)
                zone.volume = new_vol
        except Exception as e:  # noqa: BLE001
            self.error = str(e)

    def _toggle_coord_bool(self, attr: str) -> None:
        """Toggle a coordinator-level play-mode bool (shuffle/repeat/cross_fade)."""
        zone, dev = self.active_zone, self._active_device()
        if zone is None:
            return
        new = not getattr(zone, attr)
        if self.mock or dev is None:
            setattr(zone, attr, new)
            return
        try:
            with self._lock:
                setattr(self._coordinator(dev), attr, new)
                setattr(zone, attr, new)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)

    def toggle_shuffle(self) -> None:
        self._toggle_coord_bool("shuffle")

    def toggle_repeat(self) -> None:
        self._toggle_coord_bool("repeat")

    def toggle_crossfade(self) -> None:
        self._toggle_coord_bool("cross_fade")

    def toggle_mute(self) -> None:
        zone, dev = self.active_zone, self._active_device()
        if zone is None:
            return
        if self.mock or dev is None:
            zone.muted = not zone.muted
            return
        try:
            with self._lock:
                dev.mute = not zone.muted
                zone.muted = not zone.muted
        except Exception as e:  # noqa: BLE001
            self.error = str(e)

    # -- grouping ----------------------------------------------------------
    def is_active_grouped(self) -> bool:
        zone = self.active_zone
        return bool(zone and zone.grouped)

    def other_speaker_name(self) -> str | None:
        """For exactly-two-speaker setups: the name of the non-active speaker."""
        with self._lock:
            if len(self.zones) == 2:
                return self.zones[1 - self.active_idx].name
        return None

    def join_other(self) -> None:
        with self._lock:
            if len(self._devices) != 2:
                return
            dev = self._devices[self.active_idx]
            other = self._devices[1 - self.active_idx]
        if not self.mock:
            self._call(dev.join, self._coordinator(other))
            self._poll_all()

    def group_all(self) -> None:
        """Party mode: pull every speaker into the active speaker's group."""
        dev = self._active_device()
        if dev is not None and not self.mock:
            self._call(dev.partymode)
            self._poll_all()

    def ungroup_active(self) -> None:
        dev = self._active_device()
        if dev is not None and not self.mock:
            self._call(dev.unjoin)
            self._poll_all()

    # -- queue -------------------------------------------------------------
    def load_queue(self) -> None:
        dev = self._active_device()
        if self.mock or dev is None:
            return
        try:
            coord = self._coordinator(dev)
            with self._lock:
                raw = coord.get_queue(max_items=500)
            items = []
            for i, it in enumerate(raw):
                items.append(QueueItem(index=i, title=getattr(it, "title", "") or "",
                                       artist=getattr(it, "creator", "") or ""))
            self.queue_items = items
        except Exception as e:  # noqa: BLE001
            self.error = str(e)

    def play_queue_index(self, index: int) -> None:
        dev = self._active_device()
        if dev is not None and not self.mock:
            self._call(self._coordinator(dev).play_from_queue, index)
            self._refresh_active_after()

    def remove_from_queue(self, index: int) -> None:
        dev = self._active_device()
        if dev is not None and not self.mock:
            self._call(self._coordinator(dev).remove_from_queue, index)
            self.load_queue()

    def clear_queue(self) -> None:
        dev = self._active_device()
        if dev is not None and not self.mock:
            self._call(self._coordinator(dev).clear_queue)
        self.queue_items = []

    # -- favorites (Sonos Favorites) --------------------------------------
    def load_favorites(self) -> None:
        dev = self._active_device()
        if self.mock or dev is None:
            return
        try:
            with self._lock:
                # Canonical SoCo call (returns a SearchResult of DidlObjects).
                raw = dev.get_sonos_favorites(max_items=200)
            items = []
            for f in raw:
                if not getattr(f, "resources", None):
                    continue
                items.append(FavoriteItem(
                    title=f.title, uri=f.resources[0].uri,
                    metadata=getattr(f, "resource_meta_data", "") or "",
                    description=getattr(f, "description", "") or "",
                ))
            self.favorites = items
        except Exception as e:  # noqa: BLE001
            self.error = str(e)

    def play_favorite(self, fav: FavoriteItem) -> None:
        dev = self._active_device()
        if dev is None or self.mock:
            return
        coord = self._coordinator(dev)
        if self._call(coord.add_uri_to_queue, fav.uri, fav.metadata):
            self.load_queue()
            if self.queue_items:
                self.play_queue_index(len(self.queue_items) - 1)

    # -- device info / EQ --------------------------------------------------
    def fetch_device_info(self) -> list[DeviceField]:
        """Build the device-info/EQ field list for the active speaker.

        Fields are capability-gated against the real SoCo property surface
        (soundbar-only night/dialog/audio-delay, sub-only gain, etc.).
        """
        if self.mock:
            return self._mock_device_fields()
        dev = self._active_device()
        if dev is None:
            return []
        rows: list[DeviceField] = []

        def info(label: str, getter) -> None:
            try:
                rows.append(DeviceField(label, getter(), "info"))
            except Exception:  # noqa: BLE001
                pass

        def num(label: str, attr: str, lo: int, hi: int, step: int = 1, unit: str = "") -> None:
            try:
                val = getattr(dev, attr)
                if val is None:
                    return
                rows.append(DeviceField(label, int(val), "int", attr=attr,
                                        min_val=lo, max_val=hi, step=step, unit=unit))
            except Exception:  # noqa: BLE001
                pass

        def flag(label: str, attr: str) -> None:
            try:
                val = getattr(dev, attr)
                if val is not None:
                    rows.append(DeviceField(label, bool(val), "bool", attr=attr))
            except Exception:  # noqa: BLE001
                pass

        with self._lock:
            spec = {}
            try:
                spec = dev.get_speaker_info()
            except Exception:  # noqa: BLE001
                pass
            rows.append(DeviceField("Model", spec.get("model_name", "—"), "info"))
            rows.append(DeviceField("Firmware",
                                    spec.get("display_version") or spec.get("software_version", "—"), "info"))
            rows.append(DeviceField("Serial", spec.get("serial_number", "—"), "info"))
            rows.append(DeviceField("IP", dev.ip_address, "info"))
            info("Coordinator", lambda: "yes" if dev.is_coordinator else "no")
            info("Mic", lambda: "on" if dev.mic_enabled else "off")  # read-only on SoCo
            try:
                bat = dev.get_battery_info()
                if bat:
                    rows.append(DeviceField("Battery",
                        f"{bat.get('Level', '?')}%  {bat.get('PowerSource', '')}", "info"))
            except Exception:  # noqa: BLE001
                pass

            rows.append(DeviceField("", "", "sep"))
            num("Volume", "volume", 0, 100, 5)
            num("Bass", "bass", -10, 10, 1)
            num("Treble", "treble", -10, 10, 1)
            rows.append(DeviceField("Balance", _balance_to_pct(dev), "int", attr="balance",
                                    min_val=-100, max_val=100, step=10))
            flag("Loudness", "loudness")
            flag("Cross fade", "cross_fade")
            flag("Shuffle", "shuffle")
            flag("Repeat", "repeat")
            flag("Status light", "status_light")
            flag("Touch controls", "buttons_enabled")
            flag("Trueplay", "trueplay")

            soundbar = False
            try:
                soundbar = dev.is_soundbar
            except Exception:  # noqa: BLE001
                pass
            if soundbar:
                rows.append(DeviceField("", "", "sep"))
                flag("Night mode", "night_mode")
                flag("Speech enhance", "dialog_mode")
                num("Audio delay", "audio_delay", 0, 5, 1)
                flag("Surround", "surround_enabled")
                num("Surround lvl", "surround_level", -15, 15, 1)

            has_sub = False
            try:
                has_sub = dev.has_subwoofer
            except Exception:  # noqa: BLE001
                pass
            if has_sub:
                rows.append(DeviceField("", "", "sep"))
                flag("Subwoofer", "sub_enabled")
                num("Sub gain", "sub_gain", -15, 15, 1)

            rows.append(DeviceField("", "", "sep"))
            try:
                remaining = dev.get_sleep_timer() or 0
            except Exception:  # noqa: BLE001
                remaining = 0
            rows.append(DeviceField("Sleep timer", int(remaining // 60), "int", attr="sleep",
                                    min_val=0, max_val=120, step=15, unit=" min"))
        return rows

    def apply_device_field(self, field: DeviceField) -> None:
        if self.mock:
            return
        dev = self._active_device()
        if dev is None or not field.attr:
            return
        try:
            with self._lock:
                if field.attr == "balance":
                    dev.balance = _pct_to_balance(int(field.value))
                elif field.attr == "sleep":
                    secs = int(field.value) * 60
                    dev.set_sleep_timer(secs if secs > 0 else None)
                elif field.attr in ("shuffle", "repeat", "cross_fade"):
                    setattr(self._coordinator(dev), field.attr, field.value)
                else:
                    setattr(dev, field.attr, field.value)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)

    def _mock_device_fields(self) -> list[DeviceField]:
        return [
            DeviceField("Model", "Sonos One (Gen 2)", "info"),
            DeviceField("Firmware", "16.1", "info"),
            DeviceField("Serial", "AA-BB-CC-DD-EE-FF:1", "info"),
            DeviceField("IP", "192.168.1.42", "info"),
            DeviceField("Coordinator", "yes", "info"),
            DeviceField("Mic", "off", "info"),
            DeviceField("", "", "sep"),
            DeviceField("Volume", 38, "int", attr="volume", min_val=0, max_val=100, step=5),
            DeviceField("Bass", 2, "int", attr="bass", min_val=-10, max_val=10),
            DeviceField("Treble", -1, "int", attr="treble", min_val=-10, max_val=10),
            DeviceField("Balance", 0, "int", attr="balance", min_val=-100, max_val=100, step=10),
            DeviceField("Loudness", True, "bool", attr="loudness"),
            DeviceField("Cross fade", False, "bool", attr="cross_fade"),
            DeviceField("Shuffle", False, "bool", attr="shuffle"),
            DeviceField("Repeat", False, "bool", attr="repeat"),
            DeviceField("Status light", True, "bool", attr="status_light"),
            DeviceField("Touch controls", True, "bool", attr="buttons_enabled"),
            DeviceField("Trueplay", False, "bool", attr="trueplay"),
            DeviceField("", "", "sep"),
            DeviceField("Sleep timer", 0, "int", attr="sleep", min_val=0, max_val=120, step=15, unit=" min"),
        ]

    # -- mock fixtures -----------------------------------------------------
    def _load_mock(self) -> None:
        if self.zones:
            return
        track = TrackInfo(title="Mercy, Mercy, Mercy (Live)", artist="Cannonball Adderley",
                          album="Mercy, Mercy, Mercy! Live at 'The Club'",
                          duration="0:04:12", position="0:01:33", queue_index=3)
        with self._lock:
            self.discovered = True
            self.zones = [
                ZoneState("Living Room", "PLAYING", 38, False, True, 12, track),
                ZoneState("Kitchen", "PLAYING", 25, False, True, 12, track),
            ]
        self.queue_items = [
            QueueItem(0, "Autumn Leaves", "Cannonball Adderley"),
            QueueItem(1, "Work Song", "Nat Adderley"),
            QueueItem(2, "Mercy, Mercy, Mercy (Live)", "Cannonball Adderley"),
            QueueItem(3, "Sack o' Woe", "Cannonball Adderley"),
        ]
        self.favorites = [
            FavoriteItem("Jazz Classics", "x-rincon-cpcontainer:1", "", "YouTube Music Playlist"),
            FavoriteItem("Kind of Blue", "x-rincon-cpcontainer:2", "", "Album"),
            FavoriteItem("Morning Coffee", "x-rincon-cpcontainer:3", "", "YouTube Music Playlist"),
        ]


# ---------------------------------------------------------------------------
# Panel (curses)
# ---------------------------------------------------------------------------


class SonosSystem(System):
    name = "Sonos"
    color_key = "sonos"

    def __init__(self):
        self.ctl = SonosController()
        self.mode = "main"  # main | queue | favorites | group_confirm | device_info
        self._name_w = 0  # speaker-name column width, sized once state arrives
        self.queue_cursor = 0
        self.queue_scroll = 0
        self.fav_cursor = 0
        self.fav_scroll = 0
        self.group_action = ""  # "join" | "unjoin" | "party"
        self.info_rows: list[DeviceField] = []
        self.info_cursor = 0
        self.info_scroll = 0

    def poll(self, focused: bool) -> None:
        self.ctl.poll(focused)

    # -- collapsed ---------------------------------------------------------
    @property
    def collapsed_height(self) -> int:
        """Rows this panel occupies when collapsed. Dynamic so the box is sized
        right from the very first frame — 1 while discovering, one row per pinned
        speaker before state arrives (no jump), one row per speaker when
        ungrouped, the 2-line summary when fully grouped."""
        zones, _ = self.ctl.snapshot()
        if not zones:
            return self.ctl.pinned_count() or 1
        if _fully_grouped(zones):
            return 2
        return len(zones)

    def collapsed_lines(self, width: int) -> list[Line]:
        zones, active_idx = self.ctl.snapshot()
        if not zones:
            pinned = self.ctl.pinned_count()
            msg = self.ctl.error or ("Connecting..." if pinned else "Discovering...")
            # Pad to the pinned row count so the panel doesn't resize once speakers populate.
            return [[Seg(msg, dim=True)]] + [[Seg("")] for _ in range(max(0, pinned - 1))]
        if _fully_grouped(zones):
            return self._grouped_lines(zones, active_idx, width)
        # Ungrouped: one independent row per speaker (like the Midea AC panel).
        self._name_w = max(self._name_w, max(len(z.name) for z in zones))
        return [self._independent_row(z, width) for z in zones]

    def _grouped_lines(self, zones: list[ZoneState], active_idx: int, width: int) -> list[Line]:
        zone = zones[active_idx]
        label, state = badge(zone.transport_state)
        color = badge_color(state, self.color)
        track = zone.track
        detail = ""
        if track and track.title:
            detail = track.title + (f" ─ {track.artist}" if track.artist else "")
        line1 = [Seg(label, color, bold=True), Seg("  " + trunc(detail, width - len(label) - 3))]

        n = len(zones)
        left = f"{n} speakers joined"
        vols = "  ".join(f"{z.name} (vol {z.volume})" for z in zones)
        line2_text = pad_between(left, vols, width)
        line2 = [Seg(line2_text[: len(left)], dim=True), Seg(line2_text[len(left):])]
        # Stopped group → dim everything (song, room names, volumes).
        if zone.transport_state == "STOPPED":
            for s in (*line1, *line2):
                s.dim = True
        return [line1, line2]

    def _independent_row(self, zone: ZoneState, width: int) -> Line:
        """One speaker's status on a single line: state badge, name, now-playing,
        and a right-aligned volume. Song + volume dim when it isn't playing."""
        label, state = badge(zone.transport_state)
        color = badge_color(state, self.color)
        playing = zone.transport_state == "PLAYING"
        vol_text = f"vol {zone.volume}"
        track = zone.track
        song = ""
        if track and track.title:
            song = track.title + (f" ─ {track.artist}" if track.artist else "")
        prefix_w = BADGE_W + 2 + self._name_w + 2
        song = trunc(song, max(0, width - prefix_w - len(vol_text) - 2))
        left: Line = [
            Seg(f"{label:<{BADGE_W}}", color, bold=True),
            Seg("  "),
            Seg(f"{zone.name:<{self._name_w}}"),
            Seg("  "),
            Seg(song, dim=not playing),
        ]
        return justify(left, [Seg(vol_text, dim=not playing)], width)

    # -- expanded ----------------------------------------------------------
    def render_expanded(self, region: Region) -> None:
        if self.mode == "queue":
            self._render_queue(region)
        elif self.mode == "favorites":
            self._render_favorites(region)
        elif self.mode == "group_confirm":
            self._render_group_confirm(region)
        elif self.mode == "device_info":
            self._render_device_info(region)
        else:
            self._render_main(region)

    def _render_main(self, region: Region) -> None:
        zones, active_idx = self.ctl.snapshot()
        if not zones:
            region.text_wrapped(0, 0, self.ctl.error or "Discovering speakers...", dim=True)
            return

        y = 0
        for i, zone in enumerate(zones):
            if y >= region.height:
                return
            region.segs(y, self._zone_row(zone, i == active_idx, region.width))
            y += 1

        y += 1  # blank
        zone = zones[active_idx]
        track = zone.track
        if track and (track.title or track.artist):
            for label, value in (("Title", track.title or "—"),
                                 ("Artist", track.artist or "—"),
                                 ("Album", track.album or "—")):
                if y >= region.height:
                    return
                region.text(y, 0, f"{label:<8}", self.color, bold=True)
                region.text(y, 9, trunc(value, region.width - 9), bold=True)
                y += 1
            y += 1
            if y < region.height:
                prog = f"{track.position} / {track.duration}"
                if track.queue_index and zone.queue_size:
                    prog += f"   ·   {track.queue_index} of {zone.queue_size}"
                region.text(y, 9, prog, dim=True)
                y += 1
        else:
            region.text(y, 0, "Nothing playing.", dim=True)
            y += 1

        # Play-mode indicator line. The first letter of each control is its hotkey
        # (s/r/c), highlighted in plain white so the toolbar needn't list them.
        if y < region.height:
            def control(name: str, on: bool) -> Line:
                return [Seg(name[0], "", bold=True),
                        Seg(f"{name[1:]} {toggle_dot(on)}", dim=True)]
            gap = Seg("   ", dim=True)
            region.segs(y, [*control("shuffle", zone.shuffle), gap,
                            *control("repeat", zone.repeat), gap,
                            *control("crossfade", zone.cross_fade)])
            y += 1

        y += 1
        if y < region.height and zone.queue_size > 0:
            region.text(y, 0, f"{zone.queue_size} tracks in queue  (u to browse)", dim=True)

    def _render_device_info(self, region: Region) -> None:
        zone = self.ctl.active_zone
        region.text(0, 0, f"Device Info - {zone.name if zone else 'Speaker'}", self.color, bold=True)
        rows = self.info_rows
        if not rows:
            region.text(2, 0, "No device info.", dim=True)
            return
        top = 2
        visible = region.height - top
        self.info_scroll = _clamp_scroll(self.info_cursor, self.info_scroll, visible)
        for r in range(visible):
            i = self.info_scroll + r
            if i >= len(rows):
                break
            self._render_field(region, top + r, rows[i], i == self.info_cursor)

    def _render_field(self, region: Region, row: int, f: DeviceField, sel: bool) -> None:
        if f.kind == "sep":
            region.text(row, 0, "─" * region.width, dim=True)
            return
        # Selection cue: accent ▶ cursor + bold label (no reverse video).
        region.text(row, 0, "▶ " if sel else "  ", self.color if sel else "", bold=sel)
        region.text(row, 2, f"{f.label:<16}", "" if sel else self.color, bold=sel)
        if f.kind == "info":
            region.text(row, 20, trunc(str(f.value), region.width - 20), dim=True)
        elif f.kind == "bool":
            color = self.color if f.value else "muted"
            region.text(row, 20, toggle_dot(bool(f.value)), color, bold=sel)
        elif f.kind == "int":
            txt = _int_slider(int(f.value), f.min_val, f.max_val) + f"  {f.value}{f.unit}"
            region.text(row, 20, trunc(txt, region.width - 20), bold=sel)

    def _zone_row(self, zone: ZoneState, active: bool, width: int) -> Line:
        label, state = badge(zone.transport_state)
        color = badge_color(state, self.color)
        mute = "M" if zone.muted else " "
        group = "+" if zone.grouped else " "
        segs: Line = [
            cursor(self.color, active),
            Seg(f"{zone.name:<16}  "),
            Seg(f"{label:<10}", color),
            Seg(f"  {mute}{group}  "),
            *volume_bar(zone.volume, self.color),
            Seg(f" {zone.volume:>3}%  "),
        ]
        used = sum(len(s.text) for s in segs)
        track = zone.track
        if track and track.title and used < width:
            t = track.title + (f"  —  {track.artist}" if track.artist else "")
            segs.append(Seg(trunc(t, width - used), dim=True))
        # Selection cue: bold the row and lift its accent segments (no reverse
        # video) — the badge and bar brighten together with the cursor.
        return highlight(segs, self.color) if active else segs

    def _render_queue(self, region: Region) -> None:
        items = self.ctl.queue_items
        zone = self.ctl.active_zone
        speaker = zone.name if zone else ""
        region.text(0, 0, f"Queue: {speaker}  ({len(items)} tracks)", self.color, bold=True)
        if not items:
            region.text(2, 0, "Queue is empty.", dim=True)
            return
        playing_idx = (zone.track.queue_index - 1) if (zone and zone.track) else -1
        top = 2
        visible = region.height - top
        self.queue_scroll = _clamp_scroll(self.queue_cursor, self.queue_scroll, visible)
        for r in range(visible):
            i = self.queue_scroll + r
            if i >= len(items):
                break
            it = items[i]
            sel = i == self.queue_cursor
            playing = "▶ " if i == playing_idx else ""
            line = f"{it.index + 1:<3} {playing}{it.title}" + (f" — {it.artist}" if it.artist else "")
            select_row(region, top + r, trunc(line, region.width - 2), sel=sel, accent=self.color)

    def _render_favorites(self, region: Region) -> None:
        favs = self.ctl.favorites
        region.text(0, 0, f"Favorites  ({len(favs)} items)", self.color, bold=True)
        if not favs:
            region.text(2, 0, "No favorites loaded.", dim=True)
            return
        top = 2
        visible = region.height - top
        self.fav_scroll = _clamp_scroll(self.fav_cursor, self.fav_scroll, visible)
        for r in range(visible):
            i = self.fav_scroll + r
            if i >= len(favs):
                break
            fav = favs[i]
            sel = i == self.fav_cursor
            desc = f"  [{trunc(fav.description, 22)}]" if fav.description else ""
            select_row(region, top + r, trunc(f"{fav.title}{desc}", region.width - 2),
                       sel=sel, accent=self.color)

    def _render_group_confirm(self, region: Region) -> None:
        zone = self.ctl.active_zone
        speaker = zone.name if zone else "Speaker"
        if self.group_action == "join":
            other = self.ctl.other_speaker_name() or "the other speaker"
            title = "Group Speakers"
            lines = [f"Join {speaker} to {other}?", "",
                     f"{speaker} will stop what it's playing and sync."]
        elif self.group_action == "party":
            title = "Party Mode"
            lines = [f"Group all speakers with {speaker}?", "",
                     "Every speaker will play in sync."]
        else:
            title = "Ungroup Speaker"
            lines = [f"Ungroup {speaker}?", "",
                     f"{speaker} will play independently again."]
        region.text(0, 0, title, self.color, bold=True)
        for i, ln in enumerate(lines):
            region.text(2 + i, 0, ln)
        region.text(2 + len(lines) + 1, 0, "ENTER confirm    ESC cancel", dim=True)

    def _main_toolbar_hints(self) -> Line:
        return hint_row(
            hint("↕", "nav", self.color),
            hint("←→", "vol", self.color),
            hint("ENTER", "play/pause", self.color),
            hint("[ ]", "skip", self.color),
            hint("S", "stop", self.color, paren=True),
            hint("m", "mute", self.color, paren=True),
            hint("u", "queue", self.color, paren=True),
            hint("f", "fav", self.color, paren=True),
            hint("g", "group", self.color, paren=True),
            hint("d", "device", self.color, paren=True),
            sep="  ",
        )

    def _queue_toolbar_hints(self) -> Line:
        return hint_row(
            hint("↕", "nav", self.color),
            hint("ENTER", "play", self.color),
            hint("r", "remove", self.color, paren=True),
            hint("C", "clear", self.color, paren=True),
            hint("ESC", "back", self.color),
        )

    def _favorites_toolbar_hints(self) -> Line:
        return hint_row(
            hint("↕", "nav", self.color),
            hint("ENTER", "play", self.color),
            hint("ESC", "back", self.color),
        )

    def _group_toolbar_hints(self) -> Line:
        return hint_row(
            hint("ENTER", "confirm", self.color),
            hint("ESC", "cancel", self.color),
        )

    def _device_info_toolbar_hints(self) -> Line:
        return hint_row(
            hint("↕", "nav", self.color),
            hint("←→", "adjust", self.color),
            hint("ENTER", "toggle", self.color),
            hint("ESC", "back", self.color),
        )

    def toolbar_line(self) -> Line | None:
        if self.mode == "queue":
            return self._queue_toolbar_hints()
        if self.mode == "favorites":
            return self._favorites_toolbar_hints()
        if self.mode == "group_confirm":
            return self._group_toolbar_hints()
        if self.mode == "device_info":
            return self._device_info_toolbar_hints()
        return self._main_toolbar_hints()

    def help_notes(self) -> list[str]:
        # Keep in sync with the Sonos entry in README.md "Device support".
        return [
            "Controls Sonos speakers through the community-supported SoCo "
            "library: play/pause and transport, volume, grouping, the queue, "
            "and your Sonos Favorites. Speakers are discovered automatically "
            "on the LAN. (Local library serving, streaming service "
            "integration, and EQ editing aren't ported yet.)",
            "Config: [sonos] speakers pins speakers by IP — each entry is "
            "{ ip = \"...\", name = \"...\" } — to skip the ~2s SSDP discovery "
            "and connect instantly; the list order is the display order and the "
            "optional name overrides the speaker's own room name. If additional "
            "speakers are broadcast within the same system as the pinned "
            "speakers, a popup will alert the user about additional devices "
            "being available. If nothing is pinned in the config, speakers are "
            "auto-discovered and listed alphabetically.",
        ]

    # -- modal alerts ------------------------------------------------------
    def pending_popup(self) -> Popup | None:
        names = self.ctl.new_devices
        if not names:
            return None
        lines = ["These Sonos speakers are on your network but",
                 "aren't pinned in [sonos] speakers:", ""]
        lines += [f"  • {n}" for n in names]
        lines += ["", "Add them to your config to control them here."]
        return Popup(title="Unpinned Sonos speakers", lines=lines)

    def dismiss_popup(self) -> None:
        self.ctl.ack_new_devices()

    # -- input -------------------------------------------------------------
    def handle_key(self, key: int) -> bool:
        if self.mode == "queue":
            return self._handle_queue_key(key)
        if self.mode == "favorites":
            return self._handle_favorites_key(key)
        if self.mode == "group_confirm":
            return self._handle_group_key(key)
        if self.mode == "device_info":
            return self._handle_device_info_key(key)
        return self._handle_main_key(key)

    def _handle_main_key(self, key: int) -> bool:
        if key in (curses.KEY_DOWN, ord("j")):
            self.ctl.select(1)
        elif key in (curses.KEY_UP, ord("k")):
            self.ctl.select(-1)
        elif key in (ord("\n"), curses.KEY_ENTER):
            self.ctl.play_pause()
        elif key == ord("]"):
            self.ctl.next_track()
        elif key == ord("["):
            self.ctl.prev_track()
        elif key in (curses.KEY_RIGHT, ord("+"), ord("=")):
            self.ctl.set_volume(5)
        elif key in (curses.KEY_LEFT, ord("-")):
            self.ctl.set_volume(-5)
        elif key == ord("m"):
            self.ctl.toggle_mute()
        elif key == ord("S"):  # capital S to stop
            self.ctl.stop()
        elif key == ord("s"):
            self.ctl.toggle_shuffle()
        elif key == ord("r"):
            self.ctl.toggle_repeat()
        elif key == ord("c"):
            self.ctl.toggle_crossfade()
        elif key == ord("u"):
            self.ctl.load_queue()
            self.queue_cursor = self.queue_scroll = 0
            self.mode = "queue"
        elif key == ord("f"):
            self.ctl.load_favorites()
            self.fav_cursor = self.fav_scroll = 0
            self.mode = "favorites"
        elif key == ord("g"):
            self._open_group()
        elif key == ord("d"):
            self.info_rows = self.ctl.fetch_device_info()
            self.info_scroll = 0
            self._first_editable()
            self.mode = "device_info"
        else:
            return False
        return True

    def _open_group(self) -> None:
        if self.ctl.is_active_grouped():
            self.group_action = "unjoin"
        elif self.ctl.other_speaker_name():
            self.group_action = "join"
        else:
            # 3+ speakers, none grouped → offer party mode (group all).
            self.group_action = "party"
        self.mode = "group_confirm"

    def _handle_queue_key(self, key: int) -> bool:
        n = len(self.ctl.queue_items)
        if key in (27, ord("q")):
            self.mode = "main"
        elif key in (curses.KEY_DOWN, ord("j")):
            self.queue_cursor = min(n - 1, self.queue_cursor + 1) if n else 0
        elif key in (curses.KEY_UP, ord("k")):
            self.queue_cursor = max(0, self.queue_cursor - 1)
        elif key in (ord("\n"), curses.KEY_ENTER):
            if 0 <= self.queue_cursor < n:
                self.ctl.play_queue_index(self.queue_cursor)
        elif key == ord("r"):
            if 0 <= self.queue_cursor < n:
                self.ctl.remove_from_queue(self.queue_cursor)
                self.queue_cursor = min(self.queue_cursor, len(self.ctl.queue_items) - 1)
        elif key == ord("C"):
            self.ctl.clear_queue()
            self.set_status("Queue cleared")
        else:
            return False
        return True

    def _handle_favorites_key(self, key: int) -> bool:
        n = len(self.ctl.favorites)
        if key in (27, ord("q")):
            self.mode = "main"
        elif key in (curses.KEY_DOWN, ord("j")):
            self.fav_cursor = min(n - 1, self.fav_cursor + 1) if n else 0
        elif key in (curses.KEY_UP, ord("k")):
            self.fav_cursor = max(0, self.fav_cursor - 1)
        elif key in (ord("\n"), curses.KEY_ENTER):
            if 0 <= self.fav_cursor < n:
                fav = self.ctl.favorites[self.fav_cursor]
                self.ctl.play_favorite(fav)
                self.set_status(f"Playing {fav.title}")
                self.mode = "main"
        else:
            return False
        return True

    def _handle_group_key(self, key: int) -> bool:
        if key in (27, ord("q")):
            self.mode = "main"
        elif key in (ord("\n"), curses.KEY_ENTER):
            if self.group_action == "unjoin":
                self.ctl.ungroup_active()
                self.set_status("Ungrouped")
            elif self.group_action == "party":
                self.ctl.group_all()
                self.set_status("Grouped all speakers")
            else:
                self.ctl.join_other()
                self.set_status("Speakers grouped")
            self.mode = "main"
        else:
            return False
        return True

    # -- device info -------------------------------------------------------
    def _first_editable(self) -> None:
        """Place the cursor on the first editable (bool/int) row."""
        for j, f in enumerate(self.info_rows):
            if f.editable:
                self.info_cursor = j
                return
        self.info_cursor = 0

    def _handle_device_info_key(self, key: int) -> bool:
        rows = self.info_rows
        if key in (27, ord("q"), ord("d")):
            self.mode = "main"
            return True
        if not rows:
            return True
        if key in (curses.KEY_DOWN, ord("j")):
            self._step_editable(1)
        elif key in (curses.KEY_UP, ord("k")):
            self._step_editable(-1)
        elif key in (curses.KEY_LEFT, ord("-")):
            self._adjust_field(-1)
        elif key in (curses.KEY_RIGHT, ord("+"), ord("=")):
            self._adjust_field(1)
        elif key in (ord("\n"), curses.KEY_ENTER):
            f = rows[self.info_cursor]
            if f.kind == "bool":
                self._adjust_field(1)
        else:
            return False
        return True

    def _step_editable(self, direction: int) -> None:
        rows = self.info_rows
        i = self.info_cursor
        for _ in range(len(rows)):
            i += direction
            if i < 0 or i >= len(rows):
                return
            if rows[i].editable:
                self.info_cursor = i
                return

    def _adjust_field(self, direction: int) -> None:
        f = self.info_rows[self.info_cursor]
        if f.kind == "bool":
            f.value = not f.value
        elif f.kind == "int":
            new = int(f.value) + direction * f.step
            f.value = max(f.min_val, min(f.max_val, new))
        else:
            return
        self.ctl.apply_device_field(f)


def _clamp_scroll(cursor: int, scroll: int, visible: int) -> int:
    if visible <= 0:
        return scroll
    if cursor < scroll:
        return cursor
    if cursor >= scroll + visible:
        return cursor - visible + 1
    return scroll
