"""Router panel (Verizon Fios CR1000A and other UPnP IGD routers).

Status that needs no login:

  * UPnP IGD (InternetGatewayDevice) — SSDP-discovered, then SOAP queries to the
    WAN services for connection status / uptime / public IP / link rates / byte
    counters. Throughput is derived by sampling the byte counters between polls.
  * A connectivity probe — a short TCP connect to a public host each poll, giving
    latency and a rolling loss percentage (independent of UPnP, so the panel is
    still useful if the router blocks UPnP).

Device list — two sources, preferring the authoritative one:

  * RouterAuthClient (preferred) — when ``[router] password`` is set, we log into
    the CR1000A admin API (SHA-512 challenge over the per-request salt) and read
    the router's own device database (``cgi_owl.js`` → ``known_device_list``).
    Devices with ``activity != 0`` are the ones connected right now, with the
    router's friendly names, device class, and Wi-Fi/Ethernet connection. Login is
    lockout-safe: a wrong password disables further attempts and a reported
    lockout is honoured until it expires.
  * ARP sweep (fallback) — used when no password is configured or login fails: a
    UDP sweep of the subnet primes the ARP cache, then reverse-DNS names each host.

Both run on a worker thread; the headline count falls back to the ARP-cache
neighbour count before the first lookup.

Split into RouterController (no curses, lock-guarded, mock via HOME_CONTROL_MOCK=1)
and RouterSystem (the panel). The live SSDP/SOAP/admin paths need a real router on
the LAN; pure parsing/formatting/crypto helpers are unit-tested headlessly.
"""

from __future__ import annotations

import curses
import dataclasses
import hashlib
import http.cookiejar
import json
import math
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from .. import config
from ..ui import Line, Region, Seg, hint, hint_row, justify, pad_between
from .base import System

HTTP_TIMEOUT = 3        # seconds for SOAP / descriptor fetches
PROBE_TIMEOUT = 2.0     # seconds for the TCP connectivity probe
PROBE_WINDOW = 60       # samples kept for loss% (≈5 min at the 5s idle cadence)
TPUT_WINDOW = 120       # throughput samples kept for the graph
DEFAULT_PROBE_HOST = "1.1.1.1"
SCAN_INTERVAL = 60      # seconds between automatic device sweeps while focused
SCAN_SETTLE = 1.5       # seconds to let the ARP cache fill after triggering it

# IGD device types to search for, newest first.
_IGD_TYPES = [
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Device:
    ip: str
    mac: str = ""
    name: str = ""
    kind: str = ""       # device class from the router, e.g. "Phone", "Speaker"
    conn: str = ""       # "Wi-Fi" | "Ethernet" | "" (unknown)
    online: bool = True  # currently connected


@dataclass
class NetState:
    connected: bool = False      # UPnP IGD discovered + reachable
    online: bool = False         # internet reachable (connectivity probe)
    error: str = ""
    # router identity
    model: str = ""
    name: str = ""
    serial: str = ""
    admin_url: str = ""
    # WAN status (UPnP)
    status: str = ""             # e.g. "Connected"
    uptime_s: int = 0
    external_ip: str = ""
    access_type: str = ""        # e.g. "Ethernet", "DSL"
    up_max_bps: int = 0          # layer-1 max upstream
    down_max_bps: int = 0        # layer-1 max downstream
    bytes_sent: int = 0
    bytes_recv: int = 0
    up_bps: float = 0.0          # derived throughput
    down_bps: float = 0.0
    up_hist: list[float] = field(default_factory=list)    # recent throughput samples
    down_hist: list[float] = field(default_factory=list)  # (oldest → newest)
    # connectivity probe
    latency_ms: float | None = None   # internet RTT
    gateway_ms: float | None = None   # LAN RTT to the router
    loss_pct: float = 0.0
    lan_devices: int = -1        # -1 = unknown (ARP-cache neighbour count)
    # device list (authenticated router lookup, else ARP sweep)
    scanning: bool = False
    scanned: bool = False
    device_source: str = ""      # "router" | "scan" | ""
    auth_error: str = ""         # admin-login problem, if any
    devices: list[Device] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Formatting helpers (pure)
# ---------------------------------------------------------------------------


def _fmt_rate(bps: float) -> str:
    mbps = bps / 1e6
    if mbps >= 100:
        return f"{mbps:.0f} Mbps"
    if mbps >= 1:
        return f"{mbps:.1f} Mbps"
    return f"{bps / 1e3:.0f} Kbps"


def _fmt_uptime(seconds: int) -> str:
    d, rem = divmod(max(0, seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _axis_label(bps: float) -> str:
    """Compact bitrate for a chart's y-axis (e.g. '1.2G', '2M', '100M', '1k', '0').
    Drops a trailing '.0' (and absorbs float noise, so 1999999.9 reads as '2M')."""
    for div, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if bps >= div:
            v = bps / div
            s = f"{v:.0f}" if v >= 10 or round(v, 1) == round(v) else f"{v:.1f}"
            return s + suffix
    return f"{bps:.0f}"


# A "cell" is (glyph, color); color "" means dim (axis/fill/readout text).
_Cell = tuple[str, str]

# Throughput is two stacked line-charts (download on top, upload below) that both grow
# UP from their own 0 baseline. Each uses a *fixed* log y-axis so a small transfer
# stays visible alongside a large one (and a spike doesn't squash older data to the
# baseline). The scale is fixed rather than autoscaled so the gridlines are stable and
# steady traffic always lands on the same line. With floor 10 kbps and ceiling 100 Mbps
# the 4 plot rows land roughly ×20 apart: 10k, 200k, 5M, 100M (baseline → top). The scale
# is log-uniform; the gridline labels are rounded to those nice values (_tick_label rounds
# to one significant figure — e.g. the true 215k row reads "200k"). The ceiling is
# deliberately low (100M) so real traffic on a gigabit line regularly overflows the top
# row — upload eats into the download baseline, download into the title line above.
# Anything at or below the floor reads as idle (sits on the baseline).
_LOG_FLOOR_BPS = 10_000.0      # ≤ this reads as idle (sits on the 0 baseline)
_LOG_CEIL_BPS = 100_000_000.0  # top of the scale (100 Mbps); values above overflow up


def _log_value(p: int, lo: float, hi: float, rows: int) -> float:
    """Inverse of the log scale: the bitrate at plot-row ``p`` (1→lo … rows-1→hi)."""
    frac = (p - 1) / (rows - 2)
    return lo * (hi / lo) ** frac


def _plot(vals: list[float], plot_w: int, rows: int, lo: float, hi: float) -> list[list[str]]:
    """asciichart-style line plot of one series on a **log** y-axis: the baseline
    (bottom row) is "idle" (≤ ``lo``), and ``lo``…``hi`` are spread log-uniformly up
    rows 1…rows-1. Returns a `rows`×`plot_w` grid of glyphs (' ' where empty). Samples
    are anchored to the *right* edge (newest at the far right), so a partial history
    fills from the right and stays clear of the left-aligned readouts."""
    g = [[" "] * plot_w for _ in range(rows)]
    if not vals:
        return g
    span = math.log10(hi) - math.log10(lo)

    def sc(v: float) -> int:  # value → 0..rows-1 (0 = baseline = idle)
        if v <= lo:
            return 0
        if v >= hi:
            return rows - 1
        frac = (math.log10(v) - math.log10(lo)) / span
        return 1 + round(frac * (rows - 2))

    pts = vals[-plot_w:]
    off = plot_w - len(pts)  # right-align
    for i, v in enumerate(pts):
        x = off + i
        y1 = sc(v)
        if i == 0:
            g[(rows - 1) - y1][x] = "─"
            continue
        y0 = sc(pts[i - 1])
        if y0 == y1:
            g[(rows - 1) - y1][x] = "─"
        else:
            g[(rows - 1) - y1][x] = "╰" if y0 > y1 else "╭"
            g[(rows - 1) - y0][x] = "╮" if y0 > y1 else "╯"
            for yy in range(min(y0, y1) + 1, max(y0, y1)):
                g[(rows - 1) - yy][x] = "│"
    return g


def _overlay_readout(cells: list[_Cell], text: str, color: str, col: int = 1) -> list[_Cell]:
    """Splice a `● …` readout over the cells at `col`: bullet in `color`, the rate text
    plain white (the current speed is a live value, so it shouldn't be dimmed)."""
    out = list(cells)
    for i, ch in enumerate(text):
        j = col + i
        if j >= len(out):
            break
        out[j] = (ch, color if i == 0 else "white")
    return out


def _grid_row_to_line(label: str, axis: str, cells: list[_Cell], lw: int) -> Line:
    """Build a styled row: dim y-axis gutter + run-length-grouped plot cells."""
    line: Line = [Seg(label.rjust(lw) + " ", dim=True), Seg(axis, dim=True)]
    i = 0
    while i < len(cells):
        col = cells[i][1]
        j = i
        while j < len(cells) and cells[j][1] == col:
            j += 1
        line.append(Seg("".join(c for c, _ in cells[i:j]), col, dim=(col == "")))
        i = j
    return line


def _round_1sig(x: float) -> float:
    """Round to one significant figure, so a log-uniform tick like 215443 reads '200k'."""
    if x <= 0:
        return x
    mag = math.floor(math.log10(x))
    return round(x, -mag)


def _row_anchors(ph: int) -> list[float]:
    """The bitrate at each plot-row (row 1…ph) as the rounded label values, so plotting
    and the y-axis labels share the SAME gridlines (10k · 200k · 5M · 100M at ph == 4)
    rather than the raw log-uniform 215k/4.6M. Row 1 == floor, row ph == ceiling."""
    if ph <= 1:
        return [_LOG_CEIL_BPS]
    return [_round_1sig(_log_value(p, _LOG_FLOOR_BPS, _LOG_CEIL_BPS, ph + 1))
            for p in range(1, ph + 1)]


def _tick_label(p: int, ph: int) -> str:
    """Y-axis label for plot-row ``p`` (1…ph): the rounded gridline bitrate from
    _row_anchors (row 1 == floor, row ph == ceiling; e.g. 10k · 200k · 5M · 100M)."""
    return _axis_label(_row_anchors(ph)[p - 1])


def _stack_geometry(width: int, height: int) -> tuple[int, int, int, int, list[str], int, int]:
    """Split ``height`` rows into a download chart (on top) and an upload chart (below),
    each growing UP from its own 0 baseline with no gap between them. Returns
    (dph, uph, d_base, u_base, labels, lw, plot_w) — ``*ph`` are the plot-row counts,
    ``*_base`` the baseline row indices, ``labels`` the per-row y-axis text."""
    avail = height - 2                      # two baseline rows
    dph = (avail + 1) // 2                  # download takes the extra row (it's primary)
    uph = avail - dph
    d_base, u_base = dph, height - 1
    labels = [""] * height
    for r in range(dph):                    # download rows, top (100M) → just above 0
        labels[r] = _tick_label(dph - r, dph)
    for r in range(uph):                    # upload rows, top (100M) → just above 0
        labels[d_base + 1 + r] = _tick_label(uph - r, uph)
    labels[d_base] = labels[u_base] = "0"
    lw = max(len(s) for s in labels)
    plot_w = max(1, width - lw - 2)
    return dph, uph, d_base, u_base, labels, lw, plot_w


def _series_cells(vals: list[float], plot_w: int, ph: int, *,
                  overflow: bool) -> list[tuple[int, int, str]]:
    """asciichart-style line for one up-growing series, plotted against the rounded
    gridline anchors (_row_anchors), interpolating log-linearly between them so the
    waveform lines up with the labels (10k · 200k · 5M · 100M). Returns (y, x, glyph)
    where y is rows *above* the baseline (0 = on the baseline, ph = the ceiling). With
    ``overflow`` a value past the ceiling reaches y = ph+1 (the row above the chart), so
    a saturating upload overwrites the download baseline. Samples are right-anchored."""
    anchors = _row_anchors(ph)
    logs = [math.log10(a) for a in anchors]
    top = ph + (1 if overflow else 0)

    def sc(v: float) -> int:
        if v <= anchors[0]:
            return 0                              # at/below the floor gridline → idle
        if v >= anchors[-1]:
            return top                            # at/above the ceiling → top (overflow)
        lv = math.log10(v)
        for k in range(len(anchors) - 1):         # the [anchors[k], anchors[k+1]] band
            if lv < logs[k + 1]:
                frac = (lv - logs[k]) / (logs[k + 1] - logs[k])
                return (k + 1) + round(frac)      # anchors[k] sits on row k+1
        return top

    cells: list[tuple[int, int, str]] = []
    pts = vals[-plot_w:]
    off = plot_w - len(pts)
    for i, v in enumerate(pts):
        x = off + i
        y1 = sc(v)
        if i == 0:
            cells.append((y1, x, "─"))
            continue
        y0 = sc(pts[i - 1])
        if y0 == y1:
            cells.append((y1, x, "─"))
        else:
            cells.append((y1, x, "╰" if y0 > y1 else "╭"))
            cells.append((y0, x, "╮" if y0 > y1 else "╯"))
            for yy in range(min(y0, y1) + 1, max(y0, y1)):
                cells.append((yy, x, "│"))
    return cells


def _stack_chart(down: list[float], up: list[float], width: int, height: int, *,
                 down_color: str, up_color: str,
                 down_text: str, up_text: str) -> list[Line]:
    """Two stacked throughput line-charts that both grow UP from their own 0 baseline:
    download on top, upload below, no gap. Each row maps the fixed log scale 1k…100M.
    Overflow: upload past 100M reaches up into (overwrites) the download baseline;
    download past 100M reaches up into the *title* row. The first returned Line IS that
    title-overlay row (gutter blank, no axis) — the caller draws "Throughput" over its
    left and the download-overflow glyphs ride to the right. Returns 1 + ``height``
    rows, or [] when there's no room or data (caller draws the placeholder)."""
    if height < 4 or max(len(down), len(up)) < 2:
        return []
    dph, uph, d_base, u_base, labels, lw, plot_w = _stack_geometry(width, height)
    if dph < 1 or uph < 1:
        return []

    grid: list[list[_Cell]] = [[(" ", "")] * plot_w for _ in range(height)]
    title: list[_Cell] = [(" ", "")] * plot_w          # download overflow rides the title row
    for r in (d_base, u_base):                         # full-width 0 baselines
        grid[r] = [("─", "")] * plot_w
    for y, x, ch in _series_cells(down, plot_w, dph, overflow=True):
        row = d_base - y                                # y == dph+1 → row -1 (title)
        if row == -1:
            title[x] = (ch, down_color)
        elif 0 <= row < height:
            grid[row][x] = (ch, down_color)
    for y, x, ch in _series_cells(up, plot_w, uph, overflow=True):
        row = u_base - y                                # y == uph+1 lands on d_base
        if 0 <= row < height:
            grid[row][x] = (ch, up_color)

    d_read = 0                                          # readout on each chart's top
    u_read = d_base + 1                                 # (ceiling) row — clear of data
    grid[d_read] = _overlay_readout(grid[d_read], down_text, down_color)
    grid[u_read] = _overlay_readout(grid[u_read], up_text, up_color)

    body = [_grid_row_to_line(labels[r], "┼" if r in (d_base, u_base) else "┤",
                              grid[r], lw) for r in range(height)]
    return [_grid_row_to_line("", " ", title, lw)] + body


def _empty_chart(width: int, height: int, *, down_color: str, up_color: str,
                 down_text: str, up_text: str) -> list[Line]:
    """Stacked-chart placeholder (same footprint) for before enough samples exist: a
    blank title-overlay row, the two 0 baselines, and the readouts near each chart's
    top. (The "collecting throughput..." hint is drawn on the title line by the caller.)"""
    height = max(4, height)
    dph, uph, d_base, u_base, labels, lw, plot_w = _stack_geometry(width, height)
    grid: list[list[_Cell]] = [[(" ", "")] * plot_w for _ in range(height)]
    for r in (d_base, u_base):
        grid[r] = [("─", "")] * plot_w

    d_read = 0
    u_read = d_base + 1
    grid[d_read] = _overlay_readout(grid[d_read], down_text, down_color)
    grid[u_read] = _overlay_readout(grid[u_read], up_text, up_color)

    body = [_grid_row_to_line(labels[r], "┼" if r in (d_base, u_base) else "┤",
                              grid[r], lw) for r in range(height)]
    return [_grid_row_to_line("", " ", [(" ", "")] * plot_w, lw)] + body


# ---------------------------------------------------------------------------
# UPnP / ARP parsing (pure)
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _service_key(service_type: str) -> str:
    """'urn:...:service:WANIPConnection:1' -> 'WANIPConnection'."""
    parts = service_type.split(":")
    return parts[-2] if len(parts) >= 2 and parts[-1].isdigit() else parts[-1]


def parse_root_desc(xml_text: str, location: str) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Parse a UPnP rootDesc.xml. Returns (identity, services) where services maps
    a short service name -> (serviceType, absolute controlURL)."""
    root = ET.fromstring(xml_text)
    base = root.findtext("{*}URLBase") or location
    device = root.find("{*}device")
    identity: dict[str, str] = {}
    if device is not None:
        for tag in ("friendlyName", "modelName", "modelNumber", "serialNumber",
                    "manufacturer", "presentationURL"):
            val = device.findtext(f"{{*}}{tag}")
            if val:
                identity[tag] = val.strip()
    services: dict[str, tuple[str, str]] = {}
    for svc in root.iter():
        if _local(svc.tag) != "service":
            continue
        stype = svc.findtext("{*}serviceType")
        ctrl = svc.findtext("{*}controlURL")
        if stype and ctrl:
            services[_service_key(stype)] = (stype.strip(), urljoin(base, ctrl.strip()))
    return identity, services


def parse_soap(xml_text: str) -> dict[str, str]:
    """Pull the ``New*`` response fields out of a SOAP envelope."""
    out: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for el in root.iter():
        name = _local(el.tag)
        if name.startswith("New") and el.text and el.text.strip():
            out[name] = el.text.strip()
    return out


def _parse_arp_table(text: str, gateway: str = "") -> int:
    """Count distinct reachable LAN neighbours in /proc/net/arp output."""
    count = 0
    for line in text.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 4:
            continue
        ip, flags, mac = parts[0], parts[2], parts[3]
        if flags != "0x0" and mac != "00:00:00:00:00:00" and ip != gateway:
            count += 1
    return count


def _parse_arp_devices(text: str, prefix: str) -> list[Device]:
    """Reachable hosts on the local subnet from /proc/net/arp output."""
    devices: list[Device] = []
    for line in text.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 4:
            continue
        ip, flags, mac = parts[0], parts[2], parts[3]
        if flags != "0x0" and mac != "00:00:00:00:00:00" and ip.startswith(prefix):
            devices.append(Device(ip=ip, mac=mac))
    devices.sort(key=lambda d: [int(x) for x in d.ip.split(".") if x.isdigit()])
    return devices


# ---------------------------------------------------------------------------
# Authenticated device list (CR1000A admin API) — pure helpers
# ---------------------------------------------------------------------------


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _sha512_hex(s: str) -> str:
    return hashlib.sha512(s.encode()).hexdigest()


def _arc_md5(s: str) -> str:
    """The firmware's ``ArcMD5``: SHA-512 of the lowercase MD5 hex digest."""
    return _sha512_hex(_md5_hex(s))


def _login_hash(password: str, salt: str) -> str:
    """``login_encode(password, loginToken)`` = SHA-512(salt + ArcMD5(password))."""
    return _sha512_hex(salt + _arc_md5(password))


def _conn_kind(port: str) -> str:
    """Map a CR1000A interface name to a human connection type."""
    p = port.lower()
    if p.startswith(("eth", "veth", "lan", "coax", "moca", "plc")):
        return "Ethernet"
    if p.startswith(("ath", "wl", "wifi", "wlan", "ra")):
        return "Wi-Fi"
    return ""


def _ip_key(ip: str) -> tuple[int, ...]:
    parts = ip.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts)
    return (999, 999, 999, 999)


def _extract_rod(js_text: str, key: str) -> str | None:
    """Pull one ``addROD("<key>", <json>)`` value out of an owl CGI response.

    The CGI returns JavaScript that the SPA ``eval``s; each datum arrives as an
    ``addROD`` call. We brace-match the JSON argument (values here never contain
    braces, so a simple depth counter is safe; bad input just fails json.loads)."""
    marker = f'addROD("{key}",'
    i = js_text.find(marker)
    if i < 0:
        return None
    j = i + len(marker)
    while j < len(js_text) and js_text[j] not in "{[":
        j += 1
    if j >= len(js_text):
        return None
    open_ch = js_text[j]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    for k in range(j, len(js_text)):
        c = js_text[k]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return js_text[j:k + 1]
    return None


def parse_owl_devices(js_text: str) -> list[Device]:
    """Parse the currently-connected devices out of ``cgi_owl.js``.

    The ``known_device_list`` ROD holds every device the router has ever seen;
    ``activity != 0`` marks the ones online right now (this exactly matches the
    router's online-station set). Names prefer the user/router label over the
    DHCP hostname; placeholder hostnames (``unknown_<mac>``) are dropped."""
    blob = _extract_rod(js_text, "known_device_list")
    if blob is None:
        return []
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return []
    out: list[Device] = []
    for d in data.get("known_devices", []):
        if not d.get("activity"):
            continue
        name = (d.get("name") or d.get("suggested_name") or "").strip()
        host = (d.get("hostname") or "").strip()
        if not name and host and not host.startswith("unknown_"):
            name = host
        kind = (d.get("dev_class") or "").strip()
        if kind in ("(null)", "Unknown", "null"):
            kind = ""
        out.append(Device(
            ip=(d.get("ip") or "").strip(),
            mac=(d.get("mac") or "").strip().lower(),
            name=name,
            kind=kind,
            conn=_conn_kind(d.get("port") or ""),
            online=True,
        ))
    out.sort(key=lambda x: _ip_key(x.ip))
    return out


def _default_gateway() -> str:
    """The IPv4 default gateway from /proc/net/route, or '' if undetermined."""
    try:
        with open("/proc/net/route") as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        return ""
    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "00000000" and int(fields[3], 16) & 2:
            gw = fields[2]  # little-endian hex
            return ".".join(str(int(gw[i:i + 2], 16)) for i in (6, 4, 2, 0))
    return ""


# ---------------------------------------------------------------------------
# Authenticated device list (CR1000A admin API) — client
# ---------------------------------------------------------------------------


class RouterAuthClient:
    """Logs into the CR1000A admin API and reads its device database.

    Verizon's firmware authenticates with a SHA-512 challenge: ``GET
    loginStatus.cgi`` yields a per-session salt (``loginToken``); we ``POST
    login.cgi`` the hashed credentials (see :func:`_login_hash`) and the session
    rides on a cookie. ``GET cgi/cgi_owl.js`` then returns the device list.

    Lockout-safe: a wrong password (HTTP 403, ``flag=1``) permanently disables
    this client for the session rather than risk the firmware's lockout, and a
    reported lockout (``timeout>0``) is honoured until it elapses. The password is
    only ever read from config and hashed — never logged.
    """

    TIMEOUT = 6  # seconds per request

    def __init__(self, host: str, password: str):
        self.host = host
        self.password = password
        self.base = f"https://{host}"
        self.error = ""
        self._disabled = False
        self._locked_until = 0.0
        self._jar = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # router uses a self-signed cert
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            urllib.request.HTTPSHandler(context=ctx),
        )

    def available(self) -> bool:
        return bool(self.password) and not self._disabled and time.time() >= self._locked_until

    def fetch_devices(self) -> list[Device] | None:
        """Connected devices per the router, or None if the lookup is unavailable."""
        if not self.available():
            return None
        try:
            owl = self._fetch_owl()
        except OSError:
            self.error = "router unreachable"
            return None
        if owl is None:
            return None
        self.error = ""
        return parse_owl_devices(owl)

    # -- internals ---------------------------------------------------------
    def _get(self, path: str) -> str:
        req = urllib.request.Request(self.base + path)
        with self._opener.open(req, timeout=self.TIMEOUT) as resp:
            return resp.read().decode("utf-8", "ignore")

    def _status(self) -> dict[str, str]:
        try:
            return json.loads(self._get("/loginStatus.cgi"))
        except (ValueError, OSError):
            return {}

    def _fetch_owl(self) -> str | None:
        """Ensure a session, fetch the owl CGI, retrying the fetch once on 401/403.

        At most ONE login attempt per call: the owl GET is retried once (sessions
        can lapse between status and fetch), but we never fire a second login on
        that retry — repeated failed logins are what trips the firmware's lockout.
        """
        logged_in = False
        for attempt in (1, 2):
            status = self._status()
            # The firmware rotates the session token on every loginStatus.cgi
            # GET, so the login POST must reuse the token from *this* status
            # call — a second GET would invalidate it and the login 403s.
            if status.get("islogin") != "1":
                if logged_in:
                    return None  # session won't hold; don't risk a second login
                if not self._login(status):
                    return None
                logged_in = True
            try:
                return self._get("/cgi/cgi_owl.js")
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 1:
                    continue  # session lapsed between status and fetch — retry once
                raise
        return None

    def _login(self, status: dict[str, str]) -> bool:
        salt = status.get("loginToken")
        if not salt:
            self.error = "router unreachable"
            return False
        form = urllib.parse.urlencode({
            "luci_username": _arc_md5("admin"),
            "luci_password": _login_hash(self.password, salt),
            "luci_view": "1920",
            "luci_token": salt,
            "luci_keep_login": "0",
        }).encode()
        req = urllib.request.Request(self.base + "/login.cgi", data=form, method="POST")
        try:
            with self._opener.open(req, timeout=self.TIMEOUT):
                pass
        except urllib.error.HTTPError as e:
            self._handle_login_error(e)
            return False
        # A bare 200 is not proof of auth — the firmware serves the login-page
        # HTML on a rejected attempt too. The session cookie is the real signal.
        if not any(c.name == "sysauth" for c in self._jar):
            self.error = "login failed — check [router] password"
            return False
        self.error = ""
        return True

    def _handle_login_error(self, e: urllib.error.HTTPError) -> None:
        info: dict[str, object] = {}
        try:
            info = json.loads(e.read().decode("utf-8", "ignore"))
        except (ValueError, OSError):
            pass
        lock = _to_int(info.get("timeout"))
        if lock > 0:
            self._locked_until = time.time() + lock
            self.error = f"login disabled for {lock}s (too many attempts)"
        elif info.get("flag") == 1:
            self._disabled = True  # wrong password — stop, don't risk a lockout
            self.error = "login failed — check [router] password"
        else:
            self.error = "login failed"


# ---------------------------------------------------------------------------
# ICMP echo (unprivileged where the OS allows it; caller falls back to TCP)
# ---------------------------------------------------------------------------


class _ICMPUnavailable(Exception):
    """The OS won't let us open an unprivileged ICMP socket — fall back to TCP."""


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def _icmp_echo_packet() -> bytes:
    ident = os.getpid() & 0xFFFF
    payload = b"home-control"
    header = struct.pack("!BBHHH", 8, 0, 0, ident, 1)          # type 8 (echo), checksum 0
    chksum = _checksum(header + payload)
    return struct.pack("!BBHHH", 8, 0, chksum, ident, 1) + payload


def _icmp_ping(host: str, timeout: float) -> tuple[bool, float | None]:
    """One unprivileged ICMP echo. Raises _ICMPUnavailable if the OS forbids the
    datagram ICMP socket; returns (False, None) for a normal timeout/unreachable."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except (PermissionError, OSError) as e:
        raise _ICMPUnavailable from e
    try:
        sock.settimeout(timeout)
        start = time.time()
        sock.sendto(_icmp_echo_packet(), (host, 0))
        sock.recvfrom(1024)
        return True, (time.time() - start) * 1000
    except PermissionError as e:  # creation allowed but sending isn't → treat as unavailable
        raise _ICMPUnavailable from e
    except OSError:
        return False, None
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Controller (no curses)
# ---------------------------------------------------------------------------


class RouterController:
    def __init__(self, router_ip: str | None = None):
        self.router_ip = (
            router_ip or config.get("router", "router_ip", "") or _default_gateway()
        )
        self.igd_url = config.get("router", "igd_url", "")
        self.probe_host = config.get("router", "probe_host", DEFAULT_PROBE_HOST)
        self._lock = threading.Lock()
        self.state = NetState()
        self._services: dict[str, tuple[str, str]] = {}
        self._last_sample: tuple[float, int, int] | None = None
        self._location = ""  # cached IGD descriptor URL (kept across re-probes)
        self._tput_hist: deque[tuple[float, float]] = deque(maxlen=TPUT_WINDOW)
        self._probe_window: deque[bool] = deque(maxlen=PROBE_WINDOW)
        self._icmp_ok: bool | None = None  # None=untried, True=use ICMP, False=TCP fallback
        self._last_scan_t = 0.0
        self.mock = os.environ.get("HOME_CONTROL_MOCK") == "1"
        # Authenticated device list (preferred when a password is configured).
        password = config.get("router", "password", "")
        self._auth = (
            RouterAuthClient(self.router_ip, password)
            if password and self.router_ip else None
        )

    # -- polling (background thread) ---------------------------------------
    def poll(self, focused: bool) -> None:
        if self.mock:
            self._load_mock()
            return
        self._measure()  # connectivity probe + neighbours (independent of UPnP)
        if not self._services:
            # Try the pinned/cached descriptor URLs first (skips SSDP), but the
            # router's UPnP port is ephemeral (changes across reboots/firmware
            # updates) — a stale one must fall through to fresh discovery rather
            # than wedging forever on a dead URL.
            found = False
            for location in (self.igd_url, self._location):
                if location and self._setup(location):
                    found = True
                    break
            if not found:
                location = self._discover()
                if location and self._setup(location):
                    self._location = location  # reuse on re-probe; skips SSDP next time
                    found = True
            if not found and not self.state.error:
                with self._lock:
                    self.state.error = "router not found (UPnP)"
        if self._services:
            self._refresh()
        # The authenticated lookup is the device list the user cares about, so
        # refresh it on cadence even when collapsed; the noisier ARP sweep only
        # runs when focused (or as the auth fallback).
        auth_ok = self._auth is not None and self._auth.available()
        if (focused or auth_ok) and not self.state.scanning \
                and time.time() - self._last_scan_t > SCAN_INTERVAL:
            self.scan()

    # -- connectivity probe ------------------------------------------------
    def _measure(self) -> None:
        ok, ms = self._ping(self.probe_host)
        _, gw_ms = self._ping(self.router_ip) if self.router_ip else (False, None)
        self._probe_window.append(ok)
        loss = 100.0 * (1 - sum(self._probe_window) / len(self._probe_window))
        neighbours = self._count_neighbours()
        with self._lock:
            self.state.online = ok
            self.state.latency_ms = ms
            self.state.gateway_ms = gw_ms
            self.state.loss_pct = loss
            self.state.lan_devices = neighbours

    def _ping(self, host: str) -> tuple[bool, float | None]:
        """ICMP echo where the OS allows it (cached after first attempt), else a
        TCP-connect RTT to 443. Both legs use the same path so they're comparable."""
        if self._icmp_ok is not False:  # untried or known-good
            try:
                result = _icmp_ping(host, PROBE_TIMEOUT)
                self._icmp_ok = True
                return result
            except _ICMPUnavailable:
                self._icmp_ok = False  # permanent: never retry the ICMP socket
        return self._tcp_probe(host, 443)

    @staticmethod
    def _tcp_probe(host: str, port: int) -> tuple[bool, float | None]:
        start = time.time()
        try:
            with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
                return True, (time.time() - start) * 1000
        except OSError:
            return False, None

    def _count_neighbours(self) -> int:
        try:
            with open("/proc/net/arp") as f:
                return _parse_arp_table(f.read(), self.router_ip)
        except OSError:
            return -1

    # -- device list -------------------------------------------------------
    def scan(self) -> None:
        """Refresh the device list (authenticated lookup, else ARP sweep). Non-blocking."""
        if self.mock:
            return  # keep the fixture device list
        with self._lock:
            if self.state.scanning:
                return
            self.state.scanning = True
        self._last_scan_t = time.time()
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self) -> None:
        try:
            devices: list[Device] = []
            source = ""
            if self._auth is not None and self._auth.available():
                got = self._auth.fetch_devices()
                if got:  # authoritative router list wins
                    devices, source = got, "router"
            if not devices:  # fall back to the self-driven subnet sweep
                devices, source = self._sweep(), "scan"
            with self._lock:
                self.state.devices = devices
                self.state.device_source = source if devices else ""
                self.state.scanned = True
                self.state.auth_error = self._auth.error if self._auth else ""
                if devices:
                    self.state.lan_devices = len(devices)
        finally:
            with self._lock:
                self.state.scanning = False

    def _sweep(self) -> list[Device]:
        """Discover on-subnet hosts ourselves (no router auth): prime ARP, name them."""
        prefix = self.router_ip.rsplit(".", 1)[0] + "." if "." in self.router_ip else ""
        if not prefix:
            return []
        self._trigger_arp(prefix)
        time.sleep(SCAN_SETTLE)
        try:
            with open("/proc/net/arp") as f:
                devices = _parse_arp_devices(f.read(), prefix)
        except OSError:
            return []
        self._resolve_names(devices)
        for d in devices:  # label the gateway if reverse-DNS didn't
            if d.ip == self.router_ip and not d.name:
                d.name = "Router"
        return devices

    @staticmethod
    def _trigger_arp(prefix: str) -> None:
        """Poke every host so the kernel resolves its MAC into the ARP cache.
        A UDP datagram to the discard port needs no privileges and prompts L2
        ARP resolution even for hosts that ignore ICMP."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            return
        sock.setblocking(False)
        for i in range(1, 255):
            try:
                sock.sendto(b"\x00", (f"{prefix}{i}", 9))
            except OSError:
                pass
        sock.close()

    @staticmethod
    def _resolve_names(devices: list[Device]) -> None:
        """Reverse-DNS each device (names come from the router's DNS), bounded."""
        if not devices:
            return
        def name_of(ip: str) -> str:
            try:
                return socket.gethostbyaddr(ip)[0]
            except OSError:
                return ""
        try:
            with ThreadPoolExecutor(max_workers=32) as ex:
                futures = {ex.submit(name_of, d.ip): d for d in devices}
                for fut in as_completed(futures, timeout=5):
                    futures[fut].name = fut.result()
        except FuturesTimeout:
            pass  # keep whatever names resolved before the deadline

    # -- UPnP discovery + setup --------------------------------------------
    def _discover(self) -> str | None:
        for st in _IGD_TYPES:
            loc = self._ssdp_search(st)
            if loc:
                return loc
        return None

    def _ssdp_search(self, st: str) -> str | None:
        msg = "\r\n".join([
            "M-SEARCH * HTTP/1.1", "HOST: 239.255.255.250:1900",
            'MAN: "ssdp:discover"', "MX: 2", f"ST: {st}", "", "",
        ]).encode()
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2.0)
            sock.sendto(msg, ("239.255.255.250", 1900))
            start = time.time()
            while time.time() - start < 2.5:
                try:
                    data, _ = sock.recvfrom(2048)
                except TimeoutError:
                    break
                for line in data.decode("utf-8", "ignore").split("\r\n"):
                    if line.lower().startswith("location:"):
                        loc = line.split(":", 1)[1].strip()
                        if loc and (not self.router_ip or self.router_ip in loc):
                            return loc
        except OSError:
            return None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        return None

    def _setup(self, location: str) -> bool:
        xml = self._http_get(location)
        if xml is None:
            return False
        try:
            identity, services = parse_root_desc(xml, location)
        except ET.ParseError:
            return False
        if not services:
            return False
        host = urlsplit(location).hostname or self.router_ip
        admin = identity.get("presentationURL") or (f"https://{host}/" if host else "")
        admin = urljoin(location, admin) if admin else ""
        if admin.startswith("http://"):
            admin = "https://" + admin[len("http://"):]  # admin UI redirects to TLS
        model = identity.get("modelName", "") or identity.get("modelNumber", "")
        manuf = identity.get("manufacturer", "")
        # Prefer "<Manufacturer> <Model>" (e.g. "Verizon CR1000A") over the often
        # redundant friendlyName ("Verizon Router").
        display = f"{manuf} {model}".strip() if manuf and model else (
            identity.get("friendlyName") or "Router")
        with self._lock:
            self._services = services
            if host and not self.router_ip:
                self.router_ip = host
            self.state.connected = True
            self.state.error = ""
            self.state.model = model
            self.state.name = display
            self.state.serial = identity.get("serialNumber", "")
            self.state.admin_url = admin
        return True

    def _refresh(self) -> None:
        conn = self._services.get("WANIPConnection") or self._services.get("WANPPPConnection")
        common = self._services.get("WANCommonInterfaceConfig")
        status = uptime = ext = access = ""
        up_max = down_max = sent = recv = 0
        if conn:
            ext = self._soap(conn, "GetExternalIPAddress").get("NewExternalIPAddress", "")
            info = self._soap(conn, "GetStatusInfo")
            status = info.get("NewConnectionStatus", "")
            uptime = info.get("NewUptime", "0")
        if common:
            link = self._soap(common, "GetCommonLinkProperties")
            access = link.get("NewWANAccessType", "")
            up_max = _to_int(link.get("NewLayer1UpstreamMaxBitRate"))
            down_max = _to_int(link.get("NewLayer1DownstreamMaxBitRate"))
            sent = _to_int(self._soap(common, "GetTotalBytesSent").get("NewTotalBytesSent"))
            recv = _to_int(self._soap(common, "GetTotalBytesReceived").get("NewTotalBytesReceived"))

        up_bps, down_bps = self._throughput(sent, recv)
        self._tput_hist.append((down_bps, up_bps))
        with self._lock:
            self.state.status = status
            self.state.uptime_s = _to_int(uptime)
            self.state.external_ip = ext
            self.state.access_type = access
            self.state.up_max_bps = up_max
            self.state.down_max_bps = down_max
            self.state.bytes_sent = sent
            self.state.bytes_recv = recv
            self.state.up_bps = up_bps
            self.state.down_bps = down_bps
            self.state.down_hist = [d for d, _ in self._tput_hist]
            self.state.up_hist = [u for _, u in self._tput_hist]

    def _throughput(self, sent: int, recv: int) -> tuple[float, float]:
        up, down = self.state.up_bps, self.state.down_bps
        now = time.time()
        if self._last_sample and (sent or recv):
            t0, s0, r0 = self._last_sample
            dt = now - t0
            if dt > 0:
                ds, dr = sent - s0, recv - r0
                if ds >= 0:  # ignore counter wrap (32-bit routers)
                    up = ds * 8 / dt
                if dr >= 0:
                    down = dr * 8 / dt
        if sent or recv:
            self._last_sample = (now, sent, recv)
        return up, down

    # -- HTTP / SOAP -------------------------------------------------------
    def _http_get(self, url: str) -> str | None:
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001 — any network/parse failure → unavailable
            return None

    def _soap(self, svc: tuple[str, str], action: str) -> dict[str, str]:
        stype, url = svc
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{stype}"></u:{action}></s:Body></s:Envelope>'
        )
        headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{stype}#{action}"'}
        try:
            req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return parse_soap(resp.read().decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            return {}

    # -- snapshot ----------------------------------------------------------
    def snapshot(self) -> NetState:
        with self._lock:
            return dataclasses.replace(self.state)

    # -- mock fixtures -----------------------------------------------------
    def _load_mock(self) -> None:
        if self.state.connected:
            return
        import math
        down_hist = [max(0.0, (6e6 + 6e6 * math.sin(i / 5)) * (0.4 + 0.6 * (i % 7) / 6))
                     for i in range(48)]
        up_hist = [max(0.0, 1.5e6 + 1.2e6 * math.sin(i / 4 + 1)) for i in range(48)]
        with self._lock:
            self.state = NetState(
                connected=True, online=True,
                model="CR1000A", name="Verizon CR1000A", serial="ABV25102725",
                admin_url="https://192.168.1.1/",
                status="Connected", uptime_s=337200, external_ip="71.241.0.42",
                access_type="Ethernet", up_max_bps=1_000_000_000, down_max_bps=1_000_000_000,
                bytes_sent=372_000_000_000, bytes_recv=1_280_000_000_000,
                up_bps=up_hist[-1], down_bps=down_hist[-1],
                up_hist=up_hist, down_hist=down_hist,
                latency_ms=11.0, gateway_ms=2.0, loss_pct=0.0, lan_devices=6,
                scanned=True, device_source="router",
                devices=[
                    Device("192.168.1.50", "fc:df:00:8c:16:a4", "Midea_LR", "Air Conditioner", "Wi-Fi"),
                    Device("192.168.1.61", "34:7e:5c:10:c7:9a", "SonosZP-K", "Speaker", "Wi-Fi"),
                    Device("192.168.1.70", "5c:01:3b:76:ae:ac", "yoto-player", "Speaker", "Wi-Fi"),
                    Device("192.168.1.80", "9c:f1:d4:f2:15:2d", "PeppersBigScreen", "Streaming Device", "Ethernet"),
                    Device("192.168.1.99", "ec:b5:fa:bf:6b:24", "PhilipsHueBridge", "Light Controller", "Ethernet"),
                    Device("192.168.1.194", "52:c3:3c:c5:c2:18", "iPhone", "Phone", "Wi-Fi"),
                ],
            )


def _to_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Panel (curses)
# ---------------------------------------------------------------------------


class RouterSystem(System):
    name = "Router"
    color_key = "router"
    collapsed_height = 1

    poll_interval_focused = 2.0
    poll_interval_idle = 5.0

    DOWN_COLOR = "green"  # download throughput chart (top, grows up)
    UP_COLOR = "cyan"     # upload throughput chart (bottom, grows up)
    MAX_CHART_H = 10      # stacked-chart height (download 4 + base + upload 4 + base)

    def __init__(self):
        self.ctl = RouterController()
        self.scroll = 0  # device-list scroll offset

    def poll(self, focused: bool) -> None:
        self.ctl.poll(focused)

    # -- collapsed ---------------------------------------------------------
    def collapsed_lines(self, width: int) -> list[Line]:
        s = self.ctl.snapshot()
        if not s.online and not s.connected:
            return [[Seg(s.error or "checking connection...", dim=True)]]
        badge = "● ONLINE" if s.online else "● OFFLINE"
        color = "green" if s.online else "red"
        # Order mirrors the expanded view: WAN IP · latency · throughput · devices.
        bits: list[str] = []
        if s.external_ip:
            bits.append(s.external_ip)
        if s.latency_ms is not None:
            bits.append(f"{round(s.latency_ms)}ms")
        if s.down_bps or s.up_bps:
            bits.append(f"↓{_fmt_rate(s.down_bps)} ↑{_fmt_rate(s.up_bps)}")
        count = self._device_count(s)
        if count >= 0:
            bits.append(f"{count} devices")
        right = " · ".join(bits) if bits else f"{s.loss_pct:.0f}% loss/5m"
        line = pad_between(badge, right, width)
        return [[Seg(line[: len(badge)], color, bold=True), Seg(line[len(badge):])]]

    @staticmethod
    def _device_count(s: NetState) -> int:
        return len(s.devices) if s.scanned else s.lan_devices

    # -- expanded ----------------------------------------------------------
    def render_expanded(self, region: Region) -> None:
        s = self.ctl.snapshot()
        w, h = region.width, region.height
        y = self._render_status(region, s)
        y = self._render_throughput(region, s, y)
        self._render_devices(region, s, y)
        # bottom-pinned notes (only if there's spare room below everything).
        if s.auth_error and h:
            region.text(h - 1, 0, s.auth_error[:w], dim=True)

    def _render_status(self, region: Region, s: NetState) -> int:
        w = region.width
        # Line 0: status badge + uptime (left) · public IP (right). The badge sits on
        # the top line so it doesn't move when the panel expands from its collapsed form.
        badge = "● ONLINE" if s.online else "● OFFLINE"
        left: Line = [Seg(badge, "green" if s.online else "red", bold=True)]
        extra = []
        if s.status and s.status.lower() != "connected":
            extra.append(s.status)
        if s.uptime_s:
            extra.append(f"up {_fmt_uptime(s.uptime_s)}")
        if extra:
            left.append(Seg("    " + " · ".join(extra)))
        wan: Line = (
            [Seg("WAN IP ", dim=True), Seg(s.external_ip)]
            if s.external_ip else []
        )
        region.segs(0, justify(left, wan, w))
        # Line 1: device name (left) · admin URL (right). Plain weight — the badge
        # above is the line that should stand out, not the static model name.
        name = s.name or "Router"
        admin: Line = [Seg(s.admin_url, dim=True)] if s.admin_url else []
        region.segs(1, justify([Seg(name)], admin, w))
        # Line 3: latency / loss — dim labels, plain values, single-dot separators.
        lat: Line = []
        if s.gateway_ms is not None:
            lat += [Seg("gateway ", dim=True), Seg(f"{round(s.gateway_ms)} ms"),
                    Seg(" · ", dim=True)]
        lat += [Seg("internet ", dim=True),
                Seg(f"{round(s.latency_ms)} ms" if s.latency_ms is not None else "—"),
                Seg(" · ", dim=True),
                Seg("loss ", dim=True), Seg(f"{s.loss_pct:.0f}%")]
        region.text(3, 0, "Latency", self.color, bold=True)
        region.segs(3, lat, 12)
        return 5  # next free row

    def _render_throughput(self, region: Region, s: NetState, y: int) -> int:
        w, h = region.width, region.height
        down_text = f"● ↓ {_fmt_rate(s.down_bps)}"
        up_text = f"● ↑ {_fmt_rate(s.up_bps)}"
        # Size the chart to leave the title, a blank, the device header, and ≥2
        # device rows below it.
        height = min(self.MAX_CHART_H, h - y - 6)
        if height >= 4:
            pad = 2  # left/right breathing room around the whole chart
            cw = max(1, w - 2 * pad)
            chart = _stack_chart(
                s.down_hist, s.up_hist, cw, height,
                down_color=self.DOWN_COLOR, up_color=self.UP_COLOR,
                down_text=down_text, up_text=up_text,
            )
            collecting = not chart
            if collecting:
                chart = _empty_chart(
                    cw, height, down_color=self.DOWN_COLOR, up_color=self.UP_COLOR,
                    down_text=down_text, up_text=up_text,
                )
            # Title row shares the line with the chart's overflow row: draw that first,
            # then "Throughput" over its left, then (before data) the collecting hint.
            region.segs(y, chart[0], pad)
            region.text(y, 0, "Throughput", self.color, bold=True)
            if collecting:
                region.text(y, 13, "collecting throughput...", dim=True)
            for i, line in enumerate(chart[1:], start=1):
                region.segs(y + i, line, pad)
            return y + len(chart) + 1  # trailing blank
        # No vertical room for a graph — title + one-line readout.
        region.text(y, 0, "Throughput", self.color, bold=True)
        region.segs(y, [
            Seg("● ", self.DOWN_COLOR), Seg(f"↓ {_fmt_rate(s.down_bps)}", dim=True),
            Seg("      "),
            Seg("● ", self.UP_COLOR), Seg(f"↑ {_fmt_rate(s.up_bps)}", dim=True),
        ], 12)
        return y + 2

    def _render_devices(self, region: Region, s: NetState, y: int) -> None:
        w, h = region.width, region.height
        if y >= h:
            return
        # The router's own list is the preferred source and needs no annotation; only
        # flag the ARP-cache fallback (explained in the help overlay).
        via = " · via ARP cache (fallback method)" if s.device_source == "scan" else ""
        hdr: Line = [Seg(f"Devices on LAN ({len(s.devices)})", self.color, bold=True),
                     Seg(via, dim=True)]
        if s.scanning:
            hdr.append(Seg("  refreshing...", dim=True))
        region.segs(y, hdr)
        y += 1
        visible = h - y
        if visible <= 0:
            return
        dev = s.devices
        if not dev:
            region.text(y, 1, "refreshing..." if s.scanning else "no devices found", dim=True)
            return
        overflow = len(dev) > visible
        rows = visible - 1 if overflow else visible  # reserve a line for the position hint
        self.scroll = max(0, min(self.scroll, len(dev) - rows))
        for r in range(rows):
            i = self.scroll + r
            if i >= len(dev):
                break
            d = dev[i]
            # Class and connection get their own columns; the ARP fallback (no class/
            # conn) shows the MAC in the class column instead.
            kind = d.kind or (d.mac if not d.conn else "")
            # Widths are tuned so that at a 80-col terminal (76-col interior)
            # the connection column lands where "Ethernet" abuts the border.
            row = (f" {d.ip:<15} {(d.name or '—')[:27]:<27} "
                   f"{kind[:21]:<21} {d.conn}")
            region.text(y + r, 0, row[:w])
        if overflow:
            hint = f" {self.scroll + 1}–{self.scroll + rows} of {len(dev)}   ↑↓ scroll"
            region.text(h - 1, 0, hint, dim=True)

    def _toolbar_hints(self) -> Line:
        # key_color pins the hotkeys to the exact accent the section headers use
        # (e.g. "Devices on LAN") rather than the paler auto-lightened tint —
        # green needs the extra saturation to read as highlighted.
        return hint_row(
            hint("↕", "scroll", self.color, key_color=self.color),
            hint("r", "refresh devices", self.color, paren=True, key_color=self.color),
        )

    def toolbar_line(self) -> Line:
        return self._toolbar_hints()

    def help_notes(self) -> list[str]:
        # Keep in sync with the Router entry in README.md "Device support".
        return [
            "Shows WAN health that the router publishes over UPnP (IGD, "
            "discovered via SSDP), no login required: connection status, "
            "uptime, public IP, link rates, and live throughput (sampled "
            "from the router's running byte counters). A separate TCP probe "
            "to a public host each poll gives latency and rolling packet-loss.",
            "The device list has two sources. With a [router] password set, "
            "it logs into the router's web interface (custom built for the "
            "Verizon Fios CR1000A, SHA-512 challenge auth) and reads the "
            "router's device database — friendly names, device class, and "
            "Wi-Fi/Ethernet per device. Without a password it falls back to "
            "an ARP sweep of the local subnet, which sees fewer devices and "
            "fewer details.",
            "Config: [router] password unlocks the authoritative device list "
            "(Fios only); router_ip restricts discovery to one gateway; "
            "igd_url skips SSDP using a known descriptor URL; probe_host sets "
            "the latency/loss target (default 1.1.1.1).",
        ]

    # -- input -------------------------------------------------------------
    def handle_key(self, key: int) -> bool:
        n = len(self.ctl.snapshot().devices)
        if key in (curses.KEY_DOWN, ord("j")):
            self.scroll = min(max(0, n - 1), self.scroll + 1)
            return True
        if key in (curses.KEY_UP, ord("k")):
            self.scroll = max(0, self.scroll - 1)
            return True
        if key in (ord("r"), ord("R")):
            # WAN status / latency / throughput already refresh on every poll;
            # the device list only auto-refreshes on a slow cadence, so 'r' just
            # forces an immediate device lookup.
            self.ctl.scan()
            self.set_status("Refreshing devices...")
            return True
        return False
