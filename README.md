# home-control

A unified interface to control *every* device in your home. *ALL* smart devices
are supported! Whatever hardware you've got, just ask Claude to wire it up :)

Each module talks to its devices directly; no Internet or cloud are required
(except a one-time token retrieval for Midea AC units, noted below).

<p align="center">
  <img src="assets/demo.gif" width="746" />
</p>

<!-- Or, 7,000 lines of Python hallucinated by Claude that let me turn my
lights off. -->

To get a feel for the current state of agentic engineering, I built this with
Claude (mostly Opus 4.8) and a self-imposed rule to never look at the
actual code. Claude (`--dangerously-skip-permissions`) had free rein on a
sandbox, with some impressive results:
- Opus one-shot the voice-command mode even without access to an API key for
  testing.
- Opus also managed to reverse engineer the heavily obfuscated login process
  for my router (which I had failed to do a year prior, even with ChatGPT's
  help).

I did start Claude off with copies of my (already working) standalone TUIs for
my lights and Roku, but that was the last I considered the implementation.

Some places Claude fell short:
- Opus was generally unwilling to do basic due diligence on my offhanded
  suggestions and stubbornly stuck to them, rather than propose more sensible
  alternatives.
- Opus's design sense is still very bad. For almost every UI element, I had to
  design it character-by-character. It would even routinely misalign elements
  and not notice, despite prudently inspecting screen shots before committing.

## Device support

If you want to use this, the thing to do is just have Claude wire in whatever
home devices you have---the shell should extend quite easily. Out of the box,
home-control supports the following:

### Router: Verizon Fios CR1000A

Shows WAN health that the router publishes over UPnP (IGD, discovered via
SSDP), no login required: connection status, uptime, public IP, link rates,
and live throughput (sampled from the router's running byte counters). A
separate TCP probe to a public host each poll gives latency and rolling
packet-loss.

The device list has two sources. With a `[router] password` set, it logs into
the router's web interface (custom built for the Verizon Fios CR1000A, SHA-512
challenge auth) and reads the router's device database — friendly names, device
class, and Wi-Fi/Ethernet per device. Without a password it falls back to an
ARP sweep of the local subnet, which sees fewer devices and fewer details.

Config: `[router]` password unlocks the authoritative device list (Fios only);
`router_ip` restricts discovery to one gateway; `igd_url` skips SSDP using a
known descriptor URL; `probe_host` sets the latency/loss target (default
1.1.1.1).

### Media player: Roku

Controls Roku players over ECP (External Control Protocol — HTTP on port 8060):
navigation, playback, app launch, and typing into on-screen text fields. The
player is auto-discovered via SSDP at startup (~3s); if several respond, the
first is used. ECP reports the foreground app and playback state but not the
media title (Roku doesn't expose it), so the status line names the app, not the
particular content being played.

Keyboard mode forwards each character straight to whatever field the Roku is
currently showing (logins, in-app search). Search mode composes a query locally
and fires it at Roku's global search in one shot.

Config: `[roku] ip` pins the device and skips the SSDP sweep for a quicker
initialization.

### Sound system: Sonos

Controls Sonos speakers through the community-supported SoCo library: play/pause
and transport, volume, grouping, the queue, and your Sonos Favorites. Speakers
are discovered automatically on the LAN. (Local library serving, streaming
service integration, and EQ editing aren't ported yet.)

Config: `[sonos] speaker_order` sets the top-to-bottom display order using exact
Sonos room names; leave it empty to sort alphabetically.

### Lighting (Hue)

Controls a Philips Hue bridge via the phue2 library for on/off, brightness,
colour, scenes, and per-room or per-light control, plus direct HTTPS calls to
Hue's CLIP v2 API for the dynamic effects (candle, fire, prism; plain colorloop
is the older v1 API).

Config: `[hue] bridge_ip` points at the bridge. The first connection has to be
authorized by pressing the physical link button on the bridge — the panel will
prompt you — after which the credential is cached and reconnects are automatic.

### Midea AC

Controls Midea air conditioners over their local-LAN protocol via midea-local
(the extracted core of Home Assistant's `midea_ac_lan` integration). Each
connected unit runs its own persistent background thread doing heartbeats and
state refreshes, so the cards reflect live state with no polling lag. Units are
auto-discovered by LAN broadcast.

Config `[midea]`: `units` pins units by IP (skipping broadcast discovery) and
can give each a friendly `name` that overrides the unit's firmware name (e.g.
"net_ac_16A4"). Newer "V3" units require a one-time cloud login to fetch a
per-device token, cached locally afterward so the cloud is only touched once;
set `account` and `password` to your Midea app login, and `cloud` to match which
app it belongs to ("nethome_plus" or "smarthome").

### Yoto

Not implemented yet — placeholder panel with no backend.
