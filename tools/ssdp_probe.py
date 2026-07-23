#!/usr/bin/env python3
"""Send targeted SSDP M-SEARCH probes and print what answers.

Useful when a device that should be discoverable isn't: it separates "the unit
never replies" from "it replies but not to the search target we send."

    tools/ssdp_probe.py                 # every responder on the LAN
    tools/ssdp_probe.py --ip <addr>     # only that host's replies
    tools/ssdp_probe.py --st ssdp:all   # one search target instead of the set
"""

import argparse
import socket
import time

SEARCH_TARGETS = [
    "ssdp:all",
    "upnp:rootdevice",
    "urn:dial-multiscreen-org:service:dial:1",
    "urn:schemas-upnp-org:device:MediaServer:1",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:Basic:1",
    "roku:ecp",
]

INTERESTING = ("location:", "st:", "usn:", "server:")


def probe(st: str, target_ip: str | None, timeout: float = 2.0) -> list[tuple[str, str]]:
    """Return (responder_ip, response) for each reply to this search target."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        f"ST: {st}\r\n"
        "\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    sock.sendto(msg, ("239.255.255.250", 1900))

    responses = []
    end = time.time() + timeout
    while time.time() < end:
        try:
            sock.settimeout(max(0.05, end - time.time()))
            data, addr = sock.recvfrom(4096)
        except TimeoutError:
            break
        except OSError as e:
            print(f"  error: {e}")
            break
        if target_ip and addr[0] != target_ip:
            continue
        responses.append((addr[0], data.decode("utf-8", errors="replace")))
    sock.close()
    return responses


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", help="only show replies from this host (default: all)")
    ap.add_argument("--st", action="append", dest="targets",
                    help="search target to send; repeatable (default: a stock set)")
    ap.add_argument("--timeout", type=float, default=2.0, help="seconds to listen per target")
    args = ap.parse_args()

    for st in args.targets or SEARCH_TARGETS:
        print(f"\n===== ST: {st} =====")
        responses = probe(st, args.ip, args.timeout)
        if not responses:
            print("  (no response)")
        for ip, raw in responses:
            print(f"  ← {ip}")
            for line in raw.splitlines():
                if line.lower().startswith(INTERESTING):
                    print(f"    {line.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
