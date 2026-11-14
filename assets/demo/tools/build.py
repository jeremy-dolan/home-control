"""Build 3-router.ans (one more chart tick) and 4b-lighting-and-voice.ans (voice
overlay on Lighting).

Usage: build.py FRAMES_DIR   (reads 2-router.ans and 4a-lighting.ans there)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))   # repo root, for home_control
from ansgrid import Style, parse, serialize_row  # noqa: E402
from backdrop import lighten, palette, recede  # noqa: E402

from home_control import app  # noqa: E402

if len(sys.argv) != 2:
    sys.exit(f"usage: {sys.argv[0]} FRAMES_DIR")
SRC = sys.argv[1].rstrip("/") + "/"

# ---------------------------------------------------------------- router tick
old_text = open(SRC + "2-router.ans").read()
old = parse(old_text)
new = [list(r) for r in old]
PLOT0, PLOT1 = 10, 75             # plot columns (inclusive)
GREEN, CYAN = Style(fg=("ansi", 2)), Style(fg=("ansi", 6))
DIM, PLAIN = Style(dim=True), Style()

for r in range(6, 17):
    for c in range(PLOT0, PLOT1):
        new[r][c] = old[r][c + 1]
# Newest sample: both series drop 200k -> 10k; baselines continue.
last = {6: " ", 7: " ", 8: " ", 9: "╮", 10: "╰", 11: "─",
        12: " ", 13: " ", 14: "╮", 15: "╰", 16: "─"}
for r, ch in last.items():
    st = DIM if ch == "─" else (GREEN if r <= 10 else CYAN)
    new[r][PLOT1] = (ch, st)
# Overlays drawn on top of the plot: the "Throughput" label and both readouts.
new[6][2:12] = old[6][2:12]


def readout(row, text, bullet):
    row[10] = (" ", bullet)
    for i, ch in enumerate(text):
        row[11 + i] = (ch, bullet if i == 0 else PLAIN)


readout(new[7], "● ↓ 42 Kbps", GREEN)
readout(new[12], "● ↑ 24 Kbps", CYAN)

lines = old_text.split("\n")
for r in range(6, 17):
    lines[r] = serialize_row(new[r])
open(SRC + "3-router.ans", "w").write("\n".join(lines))

# ---------------------------------------------------------------- voice frame
base = parse(open(SRC + "4a-lighting.ans").read())
frame = [[(ch, recede(st)) for ch, st in row] for row in base]

H, W = 50, 80
bw = max(len("Voice") + 4, app.VOICE_CONTENT_W) + 4
bh = app.VOICE_BODY_LINES + 4
top, left = (H - bh) // 2, (W - bw) // 2
border = Style(fg=lighten("info_teal"), bold=True)
label = "╭─── Voice "
top_line = label + "─" * (bw - len(label) - 1) + "╮"
bot_line = "╰" + "─" * (bw - 2) + "╯"


def put(y, x, s, st):
    for i, ch in enumerate(s):
        frame[y][x + i] = (ch, st)


put(top, left, top_line, border)
put(top + bh - 1, left, bot_line, border)
for r in range(1, bh - 1):
    put(top + r, left, "│", border)
    put(top + r, left + bw - 1, "│", border)
    put(top + r, left + 1, " " * (bw - 2), PLAIN)

hints = ["You can say things like:"] + [f'  "{ex}"' for ex in app.VOICE_EXAMPLES]
body = ["Listening...", "", *hints, "", "> _"]       # input mode, empty buffer
dim_rows = {0} | set(range(2, 2 + len(hints)))
for i, text in enumerate(body):
    put(top + 1 + i, left + 2, text[:bw - 4], DIM if i in dim_rows else PLAIN)

teal = Style(fg=palette("info_teal"))
bright = Style(fg=lighten("info_teal"), bold=True)
hint_line = [("ENTER", bright), (" send", teal), ("    ", PLAIN), ("ESC", bright), (" cancel", teal)]
col = left + 2 + (bw - 4 - sum(len(t) for t, _ in hint_line)) // 2
for text, st in hint_line:
    put(top + 1 + app.VOICE_BODY_LINES + 1, col, text, st)
    col += len(text)

open(SRC + "4b-lighting-and-voice.ans", "w").write("\n".join(serialize_row(r) for r in frame))
