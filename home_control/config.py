"""User configuration: a TOML file read once at startup.

Location resolution: ``$HOME_CONTROL_CONFIG`` → ``~/.config/home-control/config.toml``.
The file is auto-created with a commented template on first run. Parsing is
read-only (tomllib); to keep user comments intact we never rewrite it — empty
values just fall back to sensible defaults / auto-discovery.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import tomllib

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
# every speaker on the LAN.
# speakers = [
#   { ip = "192.168.1.60" },
#   { ip = "192.168.1.61", name = "Kitchen" },
# ]
#
# Fallback ordering when nothing is pinned: exact Sonos room names, top to
# bottom. Leave empty to sort alphabetically.
speaker_order = []

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


def _load() -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if not CONFIG_PATH.exists():
            try:
                CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                CONFIG_PATH.write_text(_DEFAULT_TEMPLATE)
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
