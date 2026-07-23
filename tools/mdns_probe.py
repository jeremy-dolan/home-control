#!/usr/bin/env python3
"""Send mDNS queries at a host and decode what it advertises.

The meta-query (_services._dns-sd._udp.local PTR) asks a device to list its
own service types, which is the quickest way to learn what protocol a new
device speaks before committing to a library. Tries multicast (QU bit set, so
the reply comes back to us directly) and unicast to port 5353.

    tools/mdns_probe.py --ip <addr>                    # meta-query + stock set
    tools/mdns_probe.py --ip <addr> --service _airplay._tcp.local
"""

import argparse
import socket
import struct
import time

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353

# Service types worth asking about when the meta-query comes back empty.
STOCK_SERVICES = [
    "_companion-link._tcp.local",
    "_homekit._tcp.local",
    "_airplay._tcp.local",
    "_raop._tcp.local",
    "_googlecast._tcp.local",
    "_spotify-connect._tcp.local",
    "_sonos._tcp.local",
    "_hue._tcp.local",
    "_sleep-proxy._udp.local",
]

RECORD_TYPES = {1: "A", 12: "PTR", 16: "TXT", 28: "AAAA", 33: "SRV"}


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        if label:
            b = label.encode("ascii")
            out += bytes([len(b)]) + b
    return out + b"\x00"


def build_query(qname: str, qtype: int = 12, unicast_response: bool = False) -> bytes:
    """qtype 12 = PTR. If unicast_response, set the QU bit so the device replies
    direct to us rather than multicast."""
    header = struct.pack(">HHHHHH", 0x1234, 0x0000, 1, 0, 0, 0)
    qclass = 0x8001 if unicast_response else 0x0001  # IN, optional QU bit
    question = encode_name(qname) + struct.pack(">HH", qtype, qclass)
    return header + question


def decode_name(data: bytes, offset: int) -> tuple[str, int]:
    labels = []
    jumped = False
    original_offset = offset
    safety = 0
    while True:
        safety += 1
        if safety > 100:
            return ".".join(labels), original_offset + 1
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                original_offset = offset + 2
                jumped = True
            offset = ptr
            continue
        offset += 1
        labels.append(data[offset:offset + length].decode("ascii", errors="replace"))
        offset += length
    return ".".join(labels), (original_offset if jumped else offset)


def parse_response(data: bytes) -> None:
    if len(data) < 12:
        return
    tid, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    print(f"  tid=0x{tid:04x} flags=0x{flags:04x} qd={qd} an={an} ns={ns} ar={ar}")
    offset = 12
    for _ in range(qd):
        _, offset = decode_name(data, offset)
        offset += 4
    for section_name, count in (("ANSWER", an), ("AUTHORITY", ns), ("ADDITIONAL", ar)):
        for _ in range(count):
            name, offset = decode_name(data, offset)
            rtype, _rclass, ttl, rdlen = struct.unpack(">HHIH", data[offset:offset + 10])
            offset += 10
            rdata = data[offset:offset + rdlen]
            type_name = RECORD_TYPES.get(rtype, str(rtype))
            if rtype == 12:
                target, _ = decode_name(data, offset)
                print(f"  {section_name} {name} {type_name} ttl={ttl} -> {target}")
            elif rtype == 1:
                ip = ".".join(str(b) for b in rdata)
                print(f"  {section_name} {name} {type_name} ttl={ttl} -> {ip}")
            elif rtype == 16:
                # TXT is a series of length-prefixed strings.
                items = []
                p = 0
                while p < len(rdata):
                    ln = rdata[p]
                    items.append(rdata[p + 1:p + 1 + ln].decode("utf-8", errors="replace"))
                    p += 1 + ln
                print(f"  {section_name} {name} TXT ttl={ttl} -> {items}")
            elif rtype == 33:
                priority, weight, port = struct.unpack(">HHH", rdata[:6])
                target, _ = decode_name(data, offset + 6)
                print(f"  {section_name} {name} SRV ttl={ttl} -> {target}:{port}"
                      f" (prio={priority}, weight={weight})")
            else:
                print(f"  {section_name} {name} {type_name} ttl={ttl} rdlen={rdlen}")
            offset += rdlen


def probe(qname: str, mode: str, target_ip: str, listen_seconds: float = 3.0) -> None:
    print(f"\n===== {mode} query: {qname} =====")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    if mode == "multicast":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.bind(("", 0))
        sock.sendto(build_query(qname, unicast_response=True), (MDNS_ADDR, MDNS_PORT))
    else:
        sock.bind(("", 0))
        sock.sendto(build_query(qname, unicast_response=False), (target_ip, MDNS_PORT))

    sock.settimeout(0.5)
    end = time.time() + listen_seconds
    got_any = False
    while time.time() < end:
        try:
            data, addr = sock.recvfrom(4096)
        except TimeoutError:
            continue
        except OSError as e:
            print(f"  error: {e}")
            break
        if addr[0] != target_ip:
            continue
        got_any = True
        print(f"\n  ← from {addr[0]}:{addr[1]} ({len(data)} bytes)")
        try:
            parse_response(data)
        except Exception as e:
            print(f"  parse error: {e}")
    sock.close()
    if not got_any:
        print(f"  (no response from {target_ip} within {listen_seconds}s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", required=True, help="host to query")
    ap.add_argument("--service", action="append", dest="services",
                    help="service type to ask about; repeatable (default: a stock set)")
    ap.add_argument("--timeout", type=float, default=1.5, help="seconds to listen per query")
    args = ap.parse_args()

    probe("_services._dns-sd._udp.local", "multicast", args.ip, 3.0)
    probe("_services._dns-sd._udp.local", "unicast", args.ip, 3.0)
    for st in args.services or STOCK_SERVICES:
        probe(st, "multicast", args.ip, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
