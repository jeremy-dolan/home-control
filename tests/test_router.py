"""Headless tests for the Router panel's pure helpers (no network)."""

from home_control.systems import router as net

_ROOT_DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:2</deviceType>
    <friendlyName>Verizon Router</friendlyName>
    <manufacturer>Verizon</manufacturer>
    <modelName>CR1000A</modelName>
    <serialNumber>ABV25102725</serialNumber>
    <presentationURL>http://192.168.1.1/</presentationURL>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:WANDevice:2</deviceType>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType>
            <controlURL>/upnp/control/WANCommonIFC1</controlURL>
          </service>
        </serviceList>
        <deviceList>
          <device>
            <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:2</deviceType>
            <serviceList>
              <service>
                <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
                <controlURL>/upnp/control/WANIPConn1</controlURL>
              </service>
            </serviceList>
          </device>
        </deviceList>
      </device>
    </deviceList>
  </device>
</root>"""

_SOAP_EXT_IP = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <u:GetExternalIPAddressResponse xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">
      <NewExternalIPAddress>71.241.0.42</NewExternalIPAddress>
    </u:GetExternalIPAddressResponse>
  </s:Body>
</s:Envelope>"""


# --- formatting --------------------------------------------------------------


def test_fmt_rate():
    assert net._fmt_rate(12_300_000) == "12.3 Mbps"
    assert net._fmt_rate(940_000_000) == "940 Mbps"
    assert net._fmt_rate(850_000) == "850 Kbps"


def test_fmt_uptime():
    assert net._fmt_uptime(337200) == "3d 21h"
    assert net._fmt_uptime(3 * 3600 + 12 * 60) == "3h 12m"
    assert net._fmt_uptime(8 * 60) == "8m"


# --- UPnP parsing ------------------------------------------------------------


def test_service_key():
    assert net._service_key("urn:schemas-upnp-org:service:WANIPConnection:1") == "WANIPConnection"
    assert net._service_key("urn:schemas-upnp-org:service:Layer3Forwarding:1") == "Layer3Forwarding"


def test_parse_root_desc():
    loc = "http://192.168.1.1:40701/rootDesc.xml"
    identity, services = net.parse_root_desc(_ROOT_DESC, loc)
    assert identity["modelName"] == "CR1000A"
    assert identity["serialNumber"] == "ABV25102725"
    assert set(services) == {"WANCommonInterfaceConfig", "WANIPConnection"}
    stype, url = services["WANIPConnection"]
    assert stype.endswith("WANIPConnection:1")
    assert url == "http://192.168.1.1:40701/upnp/control/WANIPConn1"  # relative resolved


def test_parse_soap():
    assert net.parse_soap(_SOAP_EXT_IP) == {"NewExternalIPAddress": "71.241.0.42"}
    assert net.parse_soap("not xml") == {}


# --- ARP cache ---------------------------------------------------------------


def test_parse_arp_table():
    table = (
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.1      0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0\n"
        "192.168.1.50     0x1         0x2         11:22:33:44:55:66     *        eth0\n"
        "192.168.1.51     0x1         0x2         22:33:44:55:66:77     *        eth0\n"
        "192.168.1.99     0x1         0x0         00:00:00:00:00:00     *        eth0\n"  # incomplete
    )
    # gateway (.1) and the incomplete entry are excluded → 2 real neighbours
    assert net._parse_arp_table(table, gateway="192.168.1.1") == 2
    assert net._parse_arp_table(table, gateway="") == 3  # .1 counted when not the gateway


def test_parse_arp_devices():
    table = (
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.61     0x1         0x2         34:7e:5c:10:c7:9a     *        eth0\n"
        "192.168.1.1      0x1         0x2         78:67:0e:d9:7a:21     *        eth0\n"
        "10.0.0.5         0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0\n"  # other subnet
        "192.168.1.99     0x1         0x0         00:00:00:00:00:00     *        eth0\n"  # incomplete
    )
    devs = net._parse_arp_devices(table, prefix="192.168.1.")
    assert [d.ip for d in devs] == ["192.168.1.1", "192.168.1.61"]  # sorted, on-subnet only
    assert devs[0].mac == "78:67:0e:d9:7a:21"


# --- ICMP echo ---------------------------------------------------------------


def test_icmp_echo_packet_is_valid():
    pkt = net._icmp_echo_packet()
    assert pkt[0] == 8 and pkt[1] == 0          # echo request, code 0
    assert pkt.endswith(b"home-control")
    assert net._checksum(pkt) == 0              # checksum self-verifies to zero


def test_ping_falls_back_to_tcp_when_icmp_unavailable(monkeypatch):
    c = net.RouterController(router_ip="192.168.1.1")
    c.mock = False
    calls = {"icmp": 0, "tcp": 0}

    def fake_icmp(host, timeout):
        calls["icmp"] += 1
        raise net._ICMPUnavailable

    def fake_tcp(host, port):
        calls["tcp"] += 1
        return True, 5.0

    monkeypatch.setattr(net, "_icmp_ping", fake_icmp)
    monkeypatch.setattr(c, "_tcp_probe", staticmethod(fake_tcp))

    assert c._ping("1.1.1.1") == (True, 5.0)
    assert c._ping("1.1.1.1") == (True, 5.0)
    assert c._icmp_ok is False        # capability cached after first failure
    assert calls["icmp"] == 1         # ICMP attempted once, never retried
    assert calls["tcp"] == 2


def test_ping_uses_icmp_when_available(monkeypatch):
    c = net.RouterController(router_ip="192.168.1.1")
    c.mock = False
    monkeypatch.setattr(net, "_icmp_ping", lambda host, timeout: (True, 3.0))
    assert c._ping("192.168.1.1") == (True, 3.0)
    assert c._icmp_ok is True


# --- authenticated device list ----------------------------------------------

import hashlib  # noqa: E402


def test_login_crypto_matches_firmware():
    # ArcMD5(x) = SHA512(MD5(x)); login_encode(pwd, salt) = SHA512(salt + ArcMD5(pwd)).
    md5 = hashlib.md5(b"admin").hexdigest()
    assert net._arc_md5("admin") == hashlib.sha512(md5.encode()).hexdigest()
    salt, pwd = "deadbeef", "s3cret!!"
    inner = net._arc_md5(pwd)
    assert net._login_hash(pwd, salt) == hashlib.sha512((salt + inner).encode()).hexdigest()


def test_conn_kind():
    assert net._conn_kind("ath0") == "Wi-Fi"
    assert net._conn_kind("veth2") == "Ethernet"
    assert net._conn_kind("eth0") == "Ethernet"
    assert net._conn_kind("") == ""


_OWL_JS = '''
addCfg("lan_ip", "x", "192.168.1.1");
addROD("known_device_list", { "known_devices": [
  { "mac": "FC:DF:00:8C:16:A4", "name": "Midea_LR", "suggested_name": "AC",
    "hostname": "midea", "ip": "192.168.1.50", "activity": 1,
    "dev_class": "Air Conditioner", "port": "ath1" },
  { "mac": "9C:F1:D4:F2:15:2D", "name": "", "suggested_name": "Roku",
    "hostname": "unknown_9c:f1:d4:f2:15:2d", "ip": "192.168.1.80", "activity": 1,
    "dev_class": "(null)", "port": "veth2" },
  { "mac": "02:41:0A:7C:F8:A2", "name": "", "suggested_name": "Apple Computer",
    "hostname": "unknown_02:41:0a:7c:f8:a2", "ip": "192.168.1.200", "activity": 0,
    "dev_class": "Computer", "port": "ath0" }
] });
addROD("hardware_model", ["CR1000A"]);
'''


def test_parse_owl_devices():
    devs = net.parse_owl_devices(_OWL_JS)
    # only activity!=0 devices, sorted by IP
    assert [d.ip for d in devs] == ["192.168.1.50", "192.168.1.80"]
    midea, roku = devs
    assert midea.name == "Midea_LR"          # name preferred over hostname
    assert midea.mac == "fc:df:00:8c:16:a4"  # lowercased
    assert midea.kind == "Air Conditioner"
    assert midea.conn == "Wi-Fi"
    assert roku.name == "Roku"               # falls back to suggested_name
    assert roku.kind == ""                   # "(null)" dropped
    assert roku.conn == "Ethernet"


def test_parse_owl_devices_handles_garbage():
    assert net.parse_owl_devices("not javascript") == []
    assert net.parse_owl_devices('addROD("known_device_list", {bad json}) ;') == []


def test_auth_client_wrong_password_disables(monkeypatch):
    c = net.RouterAuthClient("192.168.1.1", "wrong")
    # a 403 with flag=1 must permanently disable the client (no lockout risk)
    err = net.urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    monkeypatch.setattr(err, "read", lambda: b'{"flag":1,"timeout":0}')
    c._handle_login_error(err)
    assert c.available() is False
    assert "password" in c.error


def test_auth_client_lockout_is_honoured(monkeypatch):
    c = net.RouterAuthClient("192.168.1.1", "pw")
    err = net.urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    monkeypatch.setattr(err, "read", lambda: b'{"flag":0,"timeout":300}')
    c._handle_login_error(err)
    assert c.available() is False           # locked out now
    assert c._locked_until > net.time.time()


def test_auth_client_unavailable_without_password():
    assert net.RouterAuthClient("192.168.1.1", "").available() is False


# --- throughput chart -------------------------------------------------------


def test_axis_label():
    assert net._axis_label(0) == "0"
    assert net._axis_label(850) == "850"
    assert net._axis_label(12_300) == "12k"
    assert net._axis_label(940_000) == "940k"
    assert net._axis_label(1_200_000) == "1.2M"
    assert net._axis_label(12_000_000) == "12M"
    assert net._axis_label(1_000_000_000) == "1G"
    # the throughput steps render cleanly (no trailing ".0")
    assert net._axis_label(40_000) == "40k"
    assert net._axis_label(2_000_000) == "2M"
    assert net._axis_label(100_000_000) == "100M"


def test_tick_label_steps():
    # the fixed 4-row scale: floor 10k → ceiling 100M, log-uniform rows rounded to
    # one significant figure so they read as nice values (10k · 200k · 5M · 100M).
    assert [net._tick_label(p, 4) for p in (1, 2, 3, 4)] == ["10k", "200k", "5M", "100M"]


def test_round_1sig():
    assert net._round_1sig(215_443) == 200_000   # the true 2nd-row value reads "200k"
    assert net._round_1sig(4_641_589) == 5_000_000
    assert net._round_1sig(10_000) == 10_000
    assert net._round_1sig(100_000_000) == 100_000_000


def test_row_anchors_are_the_label_values():
    # the gridlines ARE the rounded labels; an exact interior anchor value plots on its
    # own row, so the waveform lines up with the labels (not the raw 215k/4.6M).
    assert net._row_anchors(4) == [10_000, 200_000, 5_000_000, 100_000_000]
    for row in (2, 3):                       # interior rows (floor→baseline, ceil→overflow)
        value = net._row_anchors(4)[row - 1]
        cells = net._series_cells([value, value], plot_w=2, ph=4, overflow=True)
        assert {y for y, _, _ in cells} == {row}


def test_log_scale_keeps_small_visible():
    # with a 1 Gbps spike in the window, a 1 kbps sample must NOT collapse to the
    # baseline the way a linear scale would (1e3/1e9 ≈ 0 → bottom row).
    g = net._plot([1e3, 1e9], plot_w=2, rows=5, lo=100.0, hi=1e9)
    assert g[4][0] == " "          # bottom row (baseline) is empty for the 1 kbps point
    assert any(g[r][0] != " " for r in range(4))   # it's plotted above the baseline
    assert g[0][1] != " "          # the 1 Gbps point reaches the top row


def _row_text(line):
    return "".join(s.text for s in line)


def test_stack_chart_shape_and_baselines():
    down = [float(v) for v in (0, 1e6, 5e6, 3e6, 8e6, 2e6)]
    up = [float(v) for v in (0, 5e5, 1e6, 8e5, 1.2e6, 4e5)]
    lines = net._stack_chart(down, up, width=50, height=10,
                             down_color="green", up_color="cyan",
                             down_text="● ↓ 2.0 Mbps", up_text="● ↑ 400 Kbps")
    # title-overlay row + (download 4 + base + upload 4 + base)
    assert len(lines) == 11
    # two 0 baselines: one for each chart (body rows 4 and 9 → lines 5 and 10)
    assert lines[5][1].text == "┼"                       # download baseline
    assert lines[10][1].text == "┼"                      # upload baseline
    # both series colors appear
    colors = {seg.color for line in lines for seg in line}
    assert {"green", "cyan"} <= colors
    # never wider than requested
    assert all(sum(len(s.text) for s in line) <= 50 for line in lines)
    # readouts ride the top (ceiling) row of each chart
    assert "↓ 2.0 Mbps" in _row_text(lines[1])          # download top row
    assert "↑ 400 Kbps" in _row_text(lines[6])          # upload top row


def test_stack_chart_both_grow_up():
    # a download-only series fills the TOP chart (rows above its baseline); a
    # upload-only series fills the BOTTOM chart — each grows up from its own baseline.
    down = [0.0, 5e6, 9e6, 2e6, 7e6]
    idle = [0.0, 0.0, 0.0, 0.0, 0.0]
    lines = net._stack_chart(down, idle, width=50, height=10,
                             down_color="green", up_color="cyan",
                             down_text="● ↓ 7.0 Mbps", up_text="● ↑ 0 Kbps")
    top = "".join(_row_text(line) for line in lines[1:5])     # download plot rows
    bottom = "".join(_row_text(line) for line in lines[6:10])  # upload plot rows
    assert any(c in top for c in "╭╮╯╰│")               # download drawn in the top chart
    assert not any(c in bottom for c in "╭╮╯╰│")         # upload chart empty (only readout)

    lines = net._stack_chart(idle, down, width=50, height=10,
                             down_color="green", up_color="cyan",
                             down_text="● ↓ 0 Kbps", up_text="● ↑ 7.0 Mbps")
    top = "".join(_row_text(line) for line in lines[1:5])
    bottom = "".join(_row_text(line) for line in lines[6:10])
    assert not any(c in top for c in "╭╮╯╰│")            # download chart empty
    assert any(c in bottom for c in "╭╮╯╰│")             # upload grows up in the bottom chart


def test_stack_chart_upload_overflow_overwrites_download_baseline():
    # upload past the 100M ceiling reaches up into the download baseline row, drawn in
    # the upload colour (rather than clamping/being lost).
    big = [3e8, 5e8, 4e8, 6e8, 5e8]                      # all > 100M
    lines = net._stack_chart([0.0] * 5, big, width=50, height=10,
                             down_color="green", up_color="cyan",
                             down_text="● ↓ 0 Kbps", up_text="● ↑ 480 Mbps")
    assert lines[5][1].text == "┼"                       # still the download baseline axis
    assert any(seg.color == "cyan" for seg in lines[5])  # upload overwrote part of it


def test_stack_chart_download_overflow_into_title_row():
    # download past 100M reaches up into the title-overlay row (lines[0]), in the
    # download colour, so the caller can paint it over the "Throughput" line.
    big = [3e8, 5e8, 4e8, 6e8, 5e8]                      # all > 100M
    lines = net._stack_chart(big, [0.0] * 5, width=50, height=10,
                             down_color="green", up_color="cyan",
                             down_text="● ↓ 480 Mbps", up_text="● ↑ 0 Kbps")
    assert lines[0][1].text != "┼"                       # title row has no axis char
    assert any(seg.color == "green" for seg in lines[0])  # download overflowed up here


def test_stack_chart_needs_room_and_data():
    # too short
    assert net._stack_chart([1.0, 2.0], [1.0, 2.0], width=50, height=3,
                            down_color="green", up_color="cyan",
                            down_text="", up_text="") == []
    # not enough samples
    assert net._stack_chart([1.0], [], width=50, height=10,
                            down_color="green", up_color="cyan",
                            down_text="", up_text="") == []


def test_empty_chart_placeholder():
    lines = net._empty_chart(50, 10, down_color="green", up_color="cyan",
                             down_text="● ↓ 0 Kbps", up_text="● ↑ 0 Kbps")
    assert len(lines) == 11                              # title-overlay row + 10 body rows
    assert lines[5][1].text == "┼"                       # download baseline present
    assert lines[10][1].text == "┼"                      # upload baseline present
    assert "↓ 0 Kbps" in _row_text(lines[1])             # readout on the top row
    # the hint is NOT inside the chart — the caller draws it on the title line
    assert not any("collecting" in _row_text(line) for line in lines)
