#!/usr/bin/env python3
"""Probe a Midea AC the way the panel does, and print every stage.

A fallback for when the Midea card is blank or stale and it isn't obvious
whether the unit, the network, the token cache, or our parsing is at fault.
Each stage prints its own verdict, so the first FAIL line localizes it.

    tools/midea_ping.py                 # every pinned unit in config.toml
    tools/midea_ping.py --ip <addr>     # one unit, skipping config
    tools/midea_ping.py --discover      # UDP broadcast (blocks its full 5s)
    tools/midea_ping.py --raw           # dump all attributes, not the summary

Reads the token cache but never writes it, and never talks to the cloud: an
empty cache means "run the app once to pair", not a failure to debug here.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from midealocal.const import DeviceType
from midealocal.discover import discover as midea_discover

from home_control.config import section
from home_control.systems.midea import (
    TOKEN_CACHE_PATH,
    MideaController,
    _caps_ready,
    _load_token_cache,
    _status_ready,
    _unit_from_device,
    unit_badge,
)


def _say(ok: bool | None, stage: str, detail: str = "") -> None:
    mark = {True: "ok  ", False: "FAIL", None: "--  "}[ok]
    print(f"[{mark}] {stage}{': ' + detail if detail else ''}")


def _pinned(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.ip:
        return [{"ip": args.ip, "name": ""}]
    units = section("midea").get("units") or []
    return [u for u in units if u.get("ip")]


def _raw_for(pins: list[dict[str, Any]], force_discover: bool) -> dict[int, dict[str, Any]]:
    """Discovery metadata per device id, preferring the cache like the
    controller does — a cache hit is also the evidence that pairing survived."""
    if force_discover or not pins:
        t0 = time.time()
        found = midea_discover(discover_type=[DeviceType.AC]) or {}
        _say(bool(found), "discover (UDP broadcast)",
             f"{len(found)} unit(s) in {time.time() - t0:.1f}s")
        return found

    out: dict[int, dict[str, Any]] = {}
    ctrl = MideaController()
    for pin in pins:
        ip, name = pin["ip"], pin.get("name", "")
        raw = ctrl._cached_raw(ip, name)
        if raw:
            _say(True, f"cache hit {ip}", f"id={raw['device_id']} protocol=v{raw['protocol']}")
            out[raw["device_id"]] = raw
            continue
        _say(None, f"cache miss {ip}", "falling back to a single-IP probe")
        t0 = time.time()
        found = midea_discover(discover_type=[DeviceType.AC], ip_address=ip) or {}
        _say(bool(found), f"discover {ip}", f"{len(found)} unit(s) in {time.time() - t0:.1f}s")
        out.update(found)
    return out


def _probe(did: int, raw: dict[str, Any], cache: dict[str, dict[str, Any]],
           timeout: float, dump_raw: bool) -> bool:
    ip = raw["ip_address"]
    print(f"\n--- {ip}  id={did}  {raw.get('_name') or ''}".rstrip())
    # sn only comes from a live discovery reply; the token cache doesn't keep it.
    meta = " ".join(f"{k}={raw[k]}" for k in ("port", "type", "protocol", "model", "sn") if k in raw)
    print(f"    {meta}")

    entry = cache.get(str(did)) or {}
    token, key = entry.get("token", ""), entry.get("key", "")
    if raw["protocol"] == 3 and not (token and key):
        _say(False, "credentials", "no token/key cached — run the app once to pair via cloud")
        return False
    _say(True, "credentials", "v3 token/key from cache" if token else "v2, none needed")

    ctrl = MideaController()
    t0 = time.time()
    dev = ctrl._try_connect(raw, token, key)
    if dev is None:
        _say(False, "connect", f"TCP/auth failed after {time.time() - t0:.1f}s")
        return False
    _say(True, "connect", f"{time.time() - t0:.1f}s")

    try:
        # The replies are parsed by the device's own receive thread, not by
        # _prime — without open() every query goes out and nothing reads it.
        dev.daemon = True
        dev.open()
        ctrl._prime(dev)
        deadline = time.time() + timeout
        while time.time() < deadline and not (_status_ready(dev) and _caps_ready(dev)):
            time.sleep(0.1)
        waited = timeout - max(0.0, deadline - time.time())

        _say(_status_ready(dev), "status reply", f"{waited:.1f}s")
        _say(_caps_ready(dev), "capabilities reply (B5)",
             "" if _caps_ready(dev) else "card would render without mode/fan options")
        if not _status_ready(dev):
            return False

        if dump_raw:
            print("\nattributes:")
            for k, v in sorted(dev.attributes.items(), key=lambda kv: str(kv[0])):
                print(f"  {str(k):28} {v!r}")
            print("\ncapabilities:")
            for k, v in sorted((dev.capabilities or {}).items(), key=lambda kv: str(kv[0])):
                print(f"  {str(k):28} {v!r}")
        else:
            u = _unit_from_device(dev, ip)
            badge = unit_badge(u)[0]
            print(f"\n  {u.name}  [{badge}]")
            print(f"  power={u.power} mode={u.mode} fan={u.fan_speed}")
            print(f"  target={u.target_temp_c}C indoor={u.indoor_temp_c}C outdoor={u.outdoor_temp_c}C")
            print(f"  fan speeds: {', '.join(u.supported_fan_speeds)}")
        return True
    finally:
        try:
            dev.close_socket()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", help="probe one IP instead of the configured units")
    ap.add_argument("--discover", action="store_true", help="UDP broadcast instead of the cache")
    ap.add_argument("--raw", action="store_true", help="dump every attribute, not the card summary")
    ap.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for replies")
    args = ap.parse_args()

    cache = _load_token_cache()
    _say(bool(cache), f"token cache {TOKEN_CACHE_PATH}", f"{len(cache)} device(s)")

    pins = _pinned(args)
    _say(None, "configured units", ", ".join(p["ip"] for p in pins) or "none pinned")

    raw = _raw_for(pins, args.discover)
    if not raw:
        _say(False, "nothing to probe", "no unit answered")
        return 1

    results = [_probe(did, r, cache, args.timeout, args.raw) for did, r in sorted(raw.items())]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
