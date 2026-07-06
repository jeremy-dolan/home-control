"""Roku panel, ported from standalone-apps/roku/roku-remote.py.

Split into:
  * RokuController — ECP (External Control Protocol) over HTTP: SSDP discovery,
    device/active-app/media-player queries, keypress + app-launch commands.
    Lock-guarded; mock fixtures via HOME_CONTROL_MOCK=1; IP from config or SSDP.
  * RokuSystem    — the panel: collapsed status line, expanded remote (the
    D-pad/playback/volume/app keys act immediately) + an installed-apps sub-mode.

Note: ECP exposes the foreground app + playback state, but not the media *title*
(Roku doesn't surface it), so the collapsed line shows the app, not a track name.
Like the other systems, the live HTTP paths can't be exercised without a device;
panel/rendering/key-dispatch are verified via mock.
"""

from __future__ import annotations

import curses
import os
import socket
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .. import config
from ..ui import Line, Region, Seg, select_row
from .base import System, VoiceAction

ECP_PORT = 8060
# Steady-state timeout for refresh polls and keypress/launch commands. Keep it
# short: keypresses run synchronously on the UI thread, so a long timeout would
# freeze the remote on a single press.
HTTP_TIMEOUT = 2  # seconds
# The initial device-info probe gets a longer leash: a Roku waking from standby
# can take several seconds to answer its first ECP request, and if every connect
# attempt timed out at 2s we'd never get past "Connecting…".
CONNECT_TIMEOUT = 5  # seconds
# Consecutive failed refreshes tolerated before we consider the device gone. A
# single missed poll is almost always a transient blip, not a disconnect — don't
# tear down the panel for it.
RECONNECT_GRACE = 3
# Seconds a Roku may randomize its SSDP reply over (the MX header). Discovery
# listens at least this long so a late responder isn't missed.
SSDP_MX = 2

# App-launch shortcuts: key -> (app_id, name). IDs are Roku channel store ids.
APP_SHORTCUTS = {
    "Y": ("837", "YouTube"),
    "N": ("12", "Netflix"),
    "Z": ("13", "Prime Video"),
    "H": ("61322", "HBO Max"),
    "A": ("551012", "Apple TV"),
}

_BADGE = {
    "play": ("▶ PLAYING", "green"),
    "pause": ("⏸ PAUSED", "yellow"),
}

# Voice button name -> ECP keypress.
_VOICE_KEYS = {
    "home": "Home", "back": "Back", "play": "Play", "pause": "Play",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right", "select": "Select",
    "volume_up": "VolumeUp", "volume_down": "VolumeDown", "mute": "VolumeMute", "power": "Power",
}


def badge(state: str) -> tuple[str, str]:
    return _BADGE.get(state, ("■ IDLE", ""))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RokuDevice:
    name: str = ""
    model: str = ""
    sw: str = ""


@dataclass
class RokuMedia:
    state: str = ""      # play | pause | close | stop | none
    app: str = ""        # foreground app name
    position: str = ""   # "m:ss"
    duration: str = ""   # "m:ss"


# ---------------------------------------------------------------------------
# Controller (no curses)
# ---------------------------------------------------------------------------


class RokuController:
    def __init__(self, ip: str | None = None):
        self.ip = ip or config.get("roku", "ip")  # None → SSDP auto-discover
        self.auto = not self.ip      # whether the IP came from discovery (vs config)
        self.port = ECP_PORT
        self._lock = threading.Lock()
        self.connected = False
        self.ever_connected = False  # latches once we've seen the device at least once
        self.fail_count = 0          # consecutive failed refreshes (see RECONNECT_GRACE)
        self.discovered_count = 0    # verified Rokus seen in the last SSDP sweep
        self.error = ""
        self.device = RokuDevice()
        self.media = RokuMedia()
        self.apps: list[tuple[str, str]] = []
        self.mock = os.environ.get("HOME_CONTROL_MOCK") == "1"

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{self.port}"

    # -- polling (background thread) ---------------------------------------
    def poll(self, focused: bool) -> None:
        if self.mock:
            self._load_mock()
            return
        if not self.connected and not self._connect():
            return
        self._refresh()

    def _connect(self) -> bool:
        if not self.ip:
            # No IP yet → still searching. Leave the error empty; the panel shows
            # "Discovering…" while self.ip is None (see RokuSystem._status).
            found = self._discover()
            if not found:
                return False
            self.ip = found
        # Silence here is not "unreachable" — the box may just be slow to wake.
        # Leave the error empty so the panel stays on "Connecting…" and retries.
        info = self._get_xml("device-info", timeout=CONNECT_TIMEOUT)
        if info is None:
            # If we auto-discovered this IP, it may be a stale or wrong host (e.g. a
            # non-Roku that answered the M-SEARCH). Drop it so the next poll
            # re-discovers rather than retrying a dead address forever. A
            # user-configured IP is left alone — we keep trying that one.
            if self.auto:
                self.ip = None
            return False
        with self._lock:
            self.device = RokuDevice(
                name=info.findtext("user-device-name") or info.findtext("friendly-device-name") or "Roku",
                model=info.findtext("model-name") or "",
                sw=info.findtext("software-version") or "",
            )
            self.connected = True
            self.ever_connected = True
            self.fail_count = 0
            self.error = ""
        return True

    def _refresh(self) -> None:
        active = self._get_xml("active-app")
        media = self._get_xml("media-player")
        if active is None and media is None:
            # Transient blip: keep the last-known snapshot and the panel intact.
            # Only after RECONNECT_GRACE consecutive misses do we treat it as gone.
            with self._lock:
                self.fail_count += 1
                if self.fail_count >= RECONNECT_GRACE:
                    self.connected = False
            return
        app_name = ""
        if active is not None:
            app = active.find("app")
            if app is not None:
                app_name = app.text or ""
        state, pos, dur = "", "", ""
        if media is not None:
            state = media.get("state", "")
            plugin = media.find("plugin")
            if plugin is not None and not app_name:
                app_name = plugin.get("name", "")
            pos = _fmt_ms(media.findtext("position"))
            dur = _fmt_ms(media.findtext("duration"))
        with self._lock:
            self.fail_count = 0
            self.media = RokuMedia(state=state, app=app_name, position=pos, duration=dur)

    # -- discovery (SSDP) --------------------------------------------------
    def _discover(self) -> str | None:
        """Find a Roku via SSDP and return a *verified* Roku's IP, or None.

        SSDP is lossy UDP, so we re-send the M-SEARCH and listen for the full MX
        window. Crucially we then verify each responder actually answers as a
        Roku before returning it — some devices reply to every M-SEARCH
        regardless of the ST filter, and latching onto one of those would wedge
        the panel on a dead address. Returning None keeps us in "Discovering…".
        """
        msg = "\r\n".join([
            "M-SEARCH * HTTP/1.1", "HOST: 239.255.255.250:1900",
            'MAN: "ssdp:discover"', f"MX: {SSDP_MX}", "ST: roku:ecp", "", "",
        ]).encode()
        candidates: list[str] = []
        deadline = time.time() + max(SSDP_MX + 1, 3.0)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)  # short, so we loop and re-send rather than block
            last_send = 0.0
            while time.time() < deadline:
                if time.time() - last_send >= 1.0:
                    try:
                        sock.sendto(msg, ("239.255.255.250", 1900))
                    except OSError:
                        pass
                    last_send = time.time()
                try:
                    data, _ = sock.recvfrom(2048)
                except TimeoutError:
                    continue  # nothing this slice — keep waiting / re-sending
                except OSError:
                    break
                for line in data.decode("utf-8", "ignore").split("\r\n"):
                    if line.lower().startswith("location:") and "://" in line:
                        host = line.split("://", 1)[1].split("/")[0].split(":")[0]
                        if host not in candidates:
                            candidates.append(host)
        except OSError:
            return None
        finally:
            try:
                sock.close()
            except OSError:
                pass
        # Verify which responders are actually Rokus (some devices answer every
        # M-SEARCH). We connect to the first, but count them all so the UI can
        # note when more than one is present. Verifying every candidate costs an
        # extra device-info round-trip each, but candidates already answered SSDP
        # so they're reachable, and the common case is a single device.
        rokus = [host for host in candidates if self._is_roku(host)]
        with self._lock:
            self.discovered_count = len(rokus)
        return rokus[0] if rokus else None

    def _is_roku(self, host: str) -> bool:
        """True if `host` answers device-info as a Roku (generous timeout: a box
        waking from standby is slow to reply, and we don't want to reject it)."""
        try:
            url = f"http://{host}:{self.port}/query/device-info"
            with urllib.request.urlopen(url, timeout=CONNECT_TIMEOUT) as resp:
                if resp.status != 200:
                    return False
                root = ET.parse(resp).getroot()
        except Exception:  # noqa: BLE001 — any network/parse failure → not (yet) a Roku
            return False
        vendor = (root.findtext("vendor-name") or "").lower()
        return "roku" in vendor or bool(root.findtext("model-name"))

    # -- HTTP helpers ------------------------------------------------------
    def _get_xml(self, endpoint: str, timeout: float = HTTP_TIMEOUT) -> ET.Element | None:
        try:
            req = urllib.request.Request(f"{self.base_url}/query/{endpoint}", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return ET.parse(resp).getroot()
        except Exception:  # noqa: BLE001 — any network/parse failure → unavailable
            pass
        return None

    def _post(self, path: str) -> bool:
        if not self.connected or not self.ip:
            return False
        try:
            req = urllib.request.Request(f"{self.base_url}/{path}", data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status == 200
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return False

    # -- commands ----------------------------------------------------------
    def key(self, name: str) -> None:
        if not self.mock:
            self._post(f"keypress/{name}")

    def launch(self, app_id: str) -> None:
        if not self.mock:
            self._post(f"launch/{app_id}")

    def load_apps(self) -> None:
        if self.mock:
            return
        root = self._get_xml("apps")
        if root is None:
            return
        apps = [(a.get("id", ""), a.text or "") for a in root.findall(".//app")]
        with self._lock:
            self.apps = sorted((a for a in apps if a[0] and a[1]), key=lambda x: x[1].lower())

    # -- snapshot ----------------------------------------------------------
    def snapshot(self) -> tuple[RokuDevice, RokuMedia]:
        with self._lock:
            return self.device, self.media

    # -- mock --------------------------------------------------------------
    def _load_mock(self) -> None:
        if self.connected:
            return
        with self._lock:
            self.connected = True
            self.ever_connected = True
            self.device = RokuDevice(name="Living Room TV", model="Roku Ultra", sw="12.0.0")
            self.media = RokuMedia(state="pause", app="YouTube", position="1:23", duration="45:00")
            self.apps = [
                ("837", "YouTube"), ("12", "Netflix"), ("13", "Prime Video"),
                ("551012", "Apple TV"), ("61322", "HBO Max"), ("2285", "Hulu"),
                ("291097", "Disney+"), ("143", "Spotify"),
            ]


def _fmt_ms(raw: str | None) -> str:
    """ECP returns e.g. '83000 ms'; format as m:ss."""
    if not raw:
        return ""
    try:
        total = int(raw.split()[0]) // 1000
        return f"{total // 60}:{total % 60:02d}"
    except (ValueError, IndexError):
        return ""


# ---------------------------------------------------------------------------
# Panel (curses)
# ---------------------------------------------------------------------------

# Remote key legend: (label, key-hints). Shown in the expanded view.
_LEGEND = [
    ("D-pad", "↑ ↓ ← →   ENTER ok   ⌫ back   h home   i ✶"),
    ("Play", "p play/pause   r rew   f ffwd   R replay"),
    ("Volume", "+ − volume   m mute   P power"),
    ("Apps", "a list   Y YouTube  N Netflix  Z Prime  H HBO  A AppleTV"),
]


class RokuSystem(System):
    name = "Roku"
    color_key = "roku"
    collapsed_height = 1

    def __init__(self):
        self.ctl = RokuController()
        self.mode = "remote"  # remote | apps
        self.app_cursor = 0
        self.app_scroll = 0

    def poll(self, focused: bool) -> None:
        self.ctl.poll(focused)

    def _status(self) -> str:
        """Connection phase text. Distinguishes searching for *any* device
        (Discovering…) from talking to a *specific* one (Connecting…), and a
        blip on a device we already had (reconnecting…)."""
        if self.ctl.ever_connected:
            return "reconnecting…"
        if self.ctl.error:
            return self.ctl.error
        if not self.ctl.ip:
            return "Discovering…"  # no IP yet → SSDP search in progress
        n = self.ctl.discovered_count
        if n > 1:
            return f"Connecting… (first of {n} discovered)"
        return "Connecting…"       # have an IP → handshaking with that box

    # -- collapsed ---------------------------------------------------------
    def collapsed_lines(self, width: int) -> list[Line]:
        if not self.ctl.connected:
            return [[Seg(self._status(), dim=True)]]
        _, media = self.ctl.snapshot()
        label, color = badge(media.state)
        detail = media.app or self.ctl.device.name or ""
        # IDLE has no semantic colour → use the Roku accent (bold purple), mirroring
        # Router's "● ONLINE" / Lighting's "● CONNECTED".
        return [[Seg(label, color or self.color, bold=True), Seg("    " + detail)]]

    # -- expanded ----------------------------------------------------------
    def render_expanded(self, region: Region) -> None:
        if self.mode == "apps":
            self._render_apps(region)
            return
        # Only show the bare screen before the first successful connect. Once
        # we've seen the device, a blip keeps the full remote on screen (with a
        # marker) rather than dropping every hotkey and control hint.
        if not self.ctl.connected and not self.ctl.ever_connected:
            region.text(0, 0, self._status(), dim=True)
            return
        dev, media = self.ctl.snapshot()
        # Line 0: status badge (bold accent) + what's on, mirroring Router/Lighting.
        label, color = badge(media.state)
        region.text(0, 0, label, color or self.color, bold=True)
        line = media.app or "Home"
        if media.position and media.duration:
            line += f"   {media.position} / {media.duration}"
        if not self.ctl.connected:
            line += "   (reconnecting…)"
        region.text(0, 12, line)
        # Line 1: device identity — model first, then version and IP, parallel to
        # Lighting's "Hue Bridge v2 (192.168.1.99)".
        info = " ".join(p for p in (dev.model or "Roku", f"v{dev.sw}" if dev.sw else "") if p)
        if self.ctl.ip:
            info += f" ({self.ctl.ip})"
        region.text(1, 0, info)

        row = 3
        for lbl, hint in _LEGEND:
            if row >= region.height:
                break
            region.text(row, 0, f"{lbl:<8}", self.color, bold=True)
            region.text(row, 9, hint)
            row += 1

    def _render_apps(self, region: Region) -> None:
        apps = self.ctl.apps
        region.text(0, 0, f"Apps  ({len(apps)})", self.color, bold=True)
        if not apps:
            region.text(2, 0, "No apps found.", dim=True)
            return
        top = 2
        visible = region.height - top
        self.app_scroll = _clamp_scroll(self.app_cursor, self.app_scroll, visible)
        for r in range(visible):
            i = self.app_scroll + r
            if i >= len(apps):
                break
            _, name = apps[i]
            select_row(region, top + r, name, sel=i == self.app_cursor, accent=self.color)

    def toolbar(self) -> str:
        if self.mode == "apps":
            return "↕ nav   ENTER launch   ESC back"
        return "↕←→ navigate     ENTER select   p play/pause   a apps   ⌫ back"

    def help_notes(self) -> list[str]:
        return [
            "Auto-discovers via SSDP (~3s); shows Discovering… while searching.",
            "Multiple Rokus: connects to the first found (first of N discovered).",
            'Set [roku] ip in config to skip discovery and connect instantly.',
        ]

    # -- input -------------------------------------------------------------
    def handle_key(self, key: int) -> bool:
        if self.mode == "apps":
            return self._handle_apps_key(key)
        return self._handle_remote_key(key)

    def _handle_remote_key(self, key: int) -> bool:
        ctl = self.ctl
        if key == curses.KEY_UP:
            ctl.key("Up")
        elif key == curses.KEY_DOWN:
            ctl.key("Down")
        elif key == curses.KEY_LEFT:
            ctl.key("Left")
        elif key == curses.KEY_RIGHT:
            ctl.key("Right")
        elif key in (ord("\n"), curses.KEY_ENTER):
            ctl.key("Select")
        elif key in (curses.KEY_BACKSPACE, 127, 8, ord("b")):
            ctl.key("Back")
        elif key == ord("h"):
            ctl.key("Home")
        elif key == ord("i"):
            ctl.key("Info")
        elif key == ord("p"):
            ctl.key("Play")
        elif key == ord("r"):
            ctl.key("Rev")
        elif key == ord("f"):
            ctl.key("Fwd")
        elif key == ord("R"):
            ctl.key("InstantReplay")
        elif key in (ord("+"), ord("=")):
            ctl.key("VolumeUp")
        elif key == ord("-"):
            ctl.key("VolumeDown")
        elif key == ord("m"):
            ctl.key("VolumeMute")
        elif key == ord("P"):
            ctl.key("Power")
        elif key == ord("a"):  # lowercase a = apps list (distinct from 'A' = Apple TV)
            ctl.load_apps()
            self.app_cursor = self.app_scroll = 0
            self.mode = "apps"
        elif (32 <= key < 127) and chr(key) in APP_SHORTCUTS:
            app_id, app_name = APP_SHORTCUTS[chr(key)]
            ctl.launch(app_id)
            self.set_status(f"Launching {app_name}")
        else:
            return False
        return True

    # -- voice -------------------------------------------------------------
    def voice_actions(self) -> list[VoiceAction]:
        return [
            VoiceAction(
                name="roku_launch",
                description="Open / launch an app on the Roku TV by name (e.g. YouTube, Netflix).",
                parameters={"app": {"type": "string", "description": "App name."}},
                required=["app"],
                handler=self._voice_launch,
            ),
            VoiceAction(
                name="roku_button",
                description="Press a button on the Roku remote (navigation, playback, volume, power).",
                parameters={
                    "button": {
                        "type": "string",
                        "enum": sorted(_VOICE_KEYS),
                        "description": "Remote button to press.",
                    }
                },
                required=["button"],
                handler=self._voice_button,
            ),
        ]

    def voice_context(self) -> str:
        apps = [name for _, name in self.ctl.apps]
        return f"Roku apps: {', '.join(apps)}" if apps else ""

    def _voice_button(self, args: dict) -> str:
        button = str(args.get("button", "")).lower()
        name = _VOICE_KEYS.get(button)
        if not name:
            return f"Unknown button '{button}'"
        self.ctl.key(name)
        return f"Pressed {button.replace('_', ' ')}"

    def _voice_launch(self, args: dict) -> str:
        want = str(args.get("app", "")).strip().lower()
        candidates = [*APP_SHORTCUTS.values(), *self.ctl.apps]  # (app_id, name) pairs
        match = next((c for c in candidates if c[1].lower() == want), None)
        if match is None:
            match = next((c for c in candidates if want and want in c[1].lower()), None)
        if match is None:
            return f"No app matching '{args.get('app', '')}'"
        self.ctl.launch(match[0])
        self.set_status(f"Launching {match[1]}")
        return f"Launching {match[1]}"

    def _handle_apps_key(self, key: int) -> bool:
        n = len(self.ctl.apps)
        if key in (27, ord("q"), ord("a")):
            self.mode = "remote"
        elif key in (curses.KEY_DOWN, ord("j")):
            self.app_cursor = min(n - 1, self.app_cursor + 1) if n else 0
        elif key in (curses.KEY_UP, ord("k")):
            self.app_cursor = max(0, self.app_cursor - 1)
        elif key in (ord("\n"), curses.KEY_ENTER):
            if 0 <= self.app_cursor < n:
                app_id, app_name = self.ctl.apps[self.app_cursor]
                self.ctl.launch(app_id)
                self.set_status(f"Launching {app_name}")
                self.mode = "remote"
        else:
            return False
        return True


def _clamp_scroll(cursor: int, scroll: int, visible: int) -> int:
    if visible <= 0:
        return scroll
    if cursor < scroll:
        return cursor
    if cursor >= scroll + visible:
        return cursor - visible + 1
    return scroll
