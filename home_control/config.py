"""User configuration: a TOML file read once at startup.

Location resolution: ``$HOME_CONTROL_CONFIG`` → ``~/.config/home-control/config.toml``.
The file is auto-created with a commented template on first run. Parsing is
read-only (tomllib); to keep user comments intact we never rewrite it — empty
values just fall back to sensible defaults / auto-discovery.
"""

from __future__ import annotations

import os
import stat
import threading
import tomllib
import warnings
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(
    os.environ.get("HOME_CONTROL_CONFIG", Path.home() / ".config" / "home-control" / "config.toml")
)

_DEFAULT_TEMPLATE = """\
# home-control configuration

[voice]
# Voice control sends the transcript to the Claude API
# api_key = "sk-ant-..."      # or set ANTHROPIC_API_KEY in the environment
# model = "claude-haiku-4-5"  # low-latency model for snappy commands

[hue]
# IP address of the Philips Hue bridge.
bridge_ip = "192.168.1.99"

[roku]
# App will auto-discover Roku devices and connect to the first one found.
# Setting an IP skips the ~3s SSDP sweep on startup and connects instantly.
# ip = "192.168.1.80"

[sonos]
# Pin speakers by IP to skip SSDP discovery and connect instantly (discovery
# otherwise adds a ~2s sweep at startup). The list order is the display order,
# top to bottom. An optional "name" overrides the speaker's own Sonos room
# name. If any speakers are pinned and more are found on the network, a popup
# lists the unpinned ones so you can add them. Leave unset to auto-discover
# every speaker on the LAN (listed alphabetically).
# speakers = [
#   { ip = "192.168.1.60" },
#   { ip = "192.168.1.61", name = "Kitchen" },
# ]

[router]
# Router status via UPnP IGD (auto-discovered over SSDP) — all optional.
# router_ip = "192.168.1.1"     # restrict discovery to this gateway
# igd_url = "http://192.168.1.1:40701/rootDesc.xml"  # skip SSDP, use this descriptor
# probe_host = "1.1.1.1"        # public host for the latency/loss connectivity check
# password = "admin-password"   # enables the authenticated device list (Verizon Fios)

[midea]
# Pin specific units by IP: they show in the panel immediately and, once
# paired, connect with no discovery round at all (broadcast discovery blocks
# ~5s). Each entry's "name" overrides the unit's own reported firmware name
# (e.g. "net_ac_16A4"). Leave unset to auto-discover all reachable units.
# units = [
#   { ip = "192.168.1.50", name = "Living Room" },
# ]
#
# V3 units need a one-time cloud pairing to fetch a token/key (cached locally
# afterward, see ~/.cache/home-control/midea_tokens.json). Set your Midea
# account (the units must already be added to it in whichever app you use):
# account = "you@example.com"
# password = "your-account-password"
#
# Which app/cloud your account belongs to (they're separate backends even
# though they control the same physical protocol):
#   "nethome_plus" (default) — the "NetHome Plus" app
#   "smarthome"              — the "SmartHome"/MSmartHome app (what Midea is
#                              migrating users to)
# cloud = "smarthome"
"""

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
PRIVATE_FILE_MODE = 0o600


def prepare_private_file(path: Path) -> None:
    """Create ``path`` privately, or warn if an existing file is exposed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        warnings.warn(
            f"{path} has insecure permissions {mode:04o}; expected no group or other access",
            RuntimeWarning,
        )


def write_private_text(path: Path, text: str) -> None:
    """Write text, creating a missing file with mode no broader than 0600."""
    prepare_private_file(path)
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            f.write(text)
    finally:
        if fd >= 0:
            os.close(fd)


def _load() -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if not CONFIG_PATH.exists():
            try:
                write_private_text(CONFIG_PATH, _DEFAULT_TEMPLATE)
            except OSError:
                pass
        else:
            try:
                prepare_private_file(CONFIG_PATH)
            except OSError:
                pass
        data: dict[str, Any] = {}
        try:
            with open(CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        _cache = data
        return data


def section(name: str) -> dict[str, Any]:
    """Return a config table (e.g. ``[hue]``), or an empty dict if absent."""
    return _load().get(name, {}) or {}


def get(sect: str, key: str, default: Any = None) -> Any:
    """Return a single config value, treating empty strings/lists as unset."""
    val = section(sect).get(key, default)
    if val == "" or val == []:
        return default
    return val
