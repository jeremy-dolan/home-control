#!/usr/bin/env python3
"""
Roku Query Endpoint Tester

Tests which query endpoints are supported by a Roku device.
"""

import argparse
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any


def query_endpoint(base_url: str, endpoint: str) -> tuple[bool, str | None]:
    """Query an endpoint and return (success, raw_response)."""
    url = f"{base_url}/query/{endpoint}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                return True, response.read().decode("utf-8")
            return False, None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def parse_device_info(xml_str: str) -> dict[str, str]:
    """Parse device-info XML into a dict."""
    info = {}
    try:
        root = ET.fromstring(xml_str)
        for child in root:
            info[child.tag] = child.text or ""
    except ET.ParseError:
        pass
    return info


def parse_active_app(xml_str: str) -> dict[str, str]:
    """Parse active-app XML."""
    info = {}
    try:
        root = ET.fromstring(xml_str)
        app = root.find("app")
        if app is not None:
            info["id"] = app.get("id", "")
            info["name"] = app.text or ""
        screensaver = root.find("screensaver")
        if screensaver is not None:
            info["screensaver_id"] = screensaver.get("id", "")
            info["screensaver_name"] = screensaver.text or ""
    except ET.ParseError:
        pass
    return info


def parse_media_player(xml_str: str) -> dict[str, Any]:
    """Parse media-player XML."""
    info = {}
    try:
        root = ET.fromstring(xml_str)
        info["error"] = root.get("error", "")
        info["state"] = root.get("state", "")
        
        plugin = root.find("plugin")
        if plugin is not None:
            info["plugin_id"] = plugin.get("id", "")
            info["plugin_name"] = plugin.get("name", "")
        
        position = root.find("position")
        if position is not None:
            info["position"] = position.text
            
        duration = root.find("duration")
        if duration is not None:
            info["duration"] = duration.text
            
        is_live = root.find("is_live")
        if is_live is not None:
            info["is_live"] = is_live.text
    except ET.ParseError:
        pass
    return info


def parse_apps(xml_str: str) -> list[tuple[str, str]]:
    """Parse apps XML into list of (id, name)."""
    apps = []
    try:
        root = ET.fromstring(xml_str)
        for app in root.findall(".//app"):
            app_id = app.get("id", "")
            name = app.text or ""
            if app_id and name:
                apps.append((app_id, name))
    except ET.ParseError:
        pass
    return apps


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Roku query endpoints")
    parser.add_argument("--ip", required=True, help="Roku IP address")
    parser.add_argument("--port", type=int, default=8060, help="Roku port")
    parser.add_argument("--raw", action="store_true", help="Show raw XML responses")
    args = parser.parse_args()

    base_url = f"http://{args.ip}:{args.port}"
    print(f"Testing Roku at {base_url}\n")
    print("=" * 60)

    # Test device-info
    print("\n[1] /query/device-info")
    success, response = query_endpoint(base_url, "device-info")
    if success and response:
        print("    ✓ Supported")
        info = parse_device_info(response)
        if info:
            print(f"    Model: {info.get('model-name', 'N/A')} ({info.get('model-number', 'N/A')})")
            print(f"    Software: {info.get('software-version', 'N/A')}")
            print(f"    Serial: {info.get('serial-number', 'N/A')}")
            print(f"    Device Name: {info.get('user-device-name', info.get('friendly-device-name', 'N/A'))}")
        if args.raw:
            print(f"\n    Raw:\n{response[:1000]}")
    else:
        print(f"    ✗ Not supported ({response})")

    # Test active-app
    print("\n[2] /query/active-app")
    success, response = query_endpoint(base_url, "active-app")
    if success and response:
        print("    ✓ Supported")
        info = parse_active_app(response)
        if info.get("name"):
            print(f"    Active App: {info.get('name')} (ID: {info.get('id')})")
        if info.get("screensaver_name"):
            print(f"    Screensaver: {info.get('screensaver_name')}")
        if args.raw:
            print(f"\n    Raw:\n{response}")
    else:
        print(f"    ✗ Not supported ({response})")

    # Test media-player
    print("\n[3] /query/media-player")
    success, response = query_endpoint(base_url, "media-player")
    if success and response:
        print("    ✓ Supported")
        info = parse_media_player(response)
        if info:
            print(f"    State: {info.get('state', 'N/A')}")
            if info.get("error"):
                print(f"    Error: {info.get('error')}")
            if info.get("plugin_name"):
                print(f"    Plugin: {info.get('plugin_name')} (ID: {info.get('plugin_id')})")
            if info.get("position"):
                print(f"    Position: {info.get('position')}")
            if info.get("duration"):
                print(f"    Duration: {info.get('duration')}")
        if args.raw:
            print(f"\n    Raw:\n{response}")
    else:
        print(f"    ✗ Not supported ({response})")

    # Test apps
    print("\n[4] /query/apps")
    success, response = query_endpoint(base_url, "apps")
    if success and response:
        print("    ✓ Supported")
        apps = parse_apps(response)
        print(f"    Installed apps: {len(apps)}")
        if apps and not args.raw:
            print("    First 5:")
            for app_id, name in apps[:5]:
                print(f"      {app_id}: {name}")
        if args.raw:
            print(f"\n    Raw:\n{response[:2000]}")
    else:
        print(f"    ✗ Not supported ({response})")

    # Test tv-channels (may not be supported on all devices)
    print("\n[5] /query/tv-channels")
    success, response = query_endpoint(base_url, "tv-channels")
    if success and response:
        print("    ✓ Supported")
        if args.raw:
            print(f"\n    Raw:\n{response[:1000]}")
    else:
        print(f"    ✗ Not supported ({response})")

    # Test tv-active-channel
    print("\n[6] /query/tv-active-channel")
    success, response = query_endpoint(base_url, "tv-active-channel")
    if success and response:
        print("    ✓ Supported")
        if args.raw:
            print(f"\n    Raw:\n{response}")
    else:
        print(f"    ✗ Not supported ({response})")

    # Test icon (just check if endpoint responds)
    print("\n[7] /query/icon/<app_id> (testing with Home app ID 'tvinput.hdmi1')")
    try:
        url = f"{base_url}/query/icon/12"  # Netflix icon
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                content_type = response.headers.get("Content-Type", "")
                length = len(response.read())
                print(f"    ✓ Supported (Content-Type: {content_type}, Size: {length} bytes)")
            else:
                print(f"    ✗ Not supported (HTTP {response.status})")
    except Exception as e:
        print(f"    ✗ Not supported ({e})")

    print("\n" + "=" * 60)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
