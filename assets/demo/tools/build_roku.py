"""Build 5a-roku.ans (Roku focused, asleep) and 5b-roku-voice.ans (voice over it).

Usage: build_roku.py FRAMES_DIR   (reads 1-router.ans, 4a-lighting.ans,
4b-lighting-and-voice.ans and roku-mock-capture.ans there)
"""
import sys

from ansgrid import Style, parse, serialize_row
from backdrop import lighten, palette, recede

if len(sys.argv) != 2:
    sys.exit(f"usage: {sys.argv[0]} FRAMES_DIR")
SRC = sys.argv[1].rstrip("/") + "/"

lighting = parse(open(SRC + "4a-lighting.ans").read())
router1 = parse(open(SRC + "1-router.ans").read())
cap = parse(open(SRC + "roku-mock-capture.ans").read())

BASE = palette("roku_purple")
BRIGHT = lighten("roku_purple")
# tmux quantises the palette to the xterm-256 cube; map back to the exact hex.
REMAP: dict[tuple | None, tuple | None] = {("256", 135): BASE, ("256", 177): BRIGHT}
MUTED = Style(fg=("rgb", 127, 127, 127))      # the muted grey the other frames use
PLAIN = Style()

roku = []
for row in cap[6:36]:
    row = (row + [(" ", PLAIN)] * 80)[:80]
    roku.append([(ch, Style(fg=REMAP.get(st.fg, st.fg), bold=st.bold, dim=st.dim)) for ch, st in row])

# Border/title styled like the focused Lighting panel: bright accent, title bold.
border = Style(fg=BRIGHT)
for c in range(80):
    roku[0][c] = (roku[0][c][0], border)
    roku[-1][c] = (roku[-1][c][0], border)
for r in range(1, len(roku) - 1):
    roku[r][0] = ("│", border)
    roku[r][79] = ("│", border)
for c in range(5, 9):
    roku[0][c] = (roku[0][c][0], Style(fg=BRIGHT, bold=True))

# Header: asleep, matching the collapsed "● ASLEEP" in the other frames.
header = "● ASLEEP"
roku[1][2:78] = [(ch, MUTED) for ch in header] + [(" ", PLAIN)] * (76 - len(header))
roku[2][2:78] = [(ch, MUTED) for ch, _ in roku[2][2:78]]     # device line, muted

frame = lighting[0:3] + router1[30:33] + roku + lighting[36:]
assert len(frame) == len(lighting), len(frame)
assert [len(r) for r in frame] == [len(r) for r in lighting]
open(SRC + "5a-roku.ans", "w").write("\n".join(serialize_row(r) for r in frame))

# ---------------------------------------------------------------- voice overlay
v = [[(ch, recede(st)) for ch, st in row] for row in frame]
voice_ref = parse(open(SRC + "4b-lighting-and-voice.ans").read())
for r in range(19, 31):                     # the Voice box, cell for cell
    v[r][16:64] = voice_ref[r][16:64]
open(SRC + "5b-roku-voice.ans", "w").write("\n".join(serialize_row(r) for r in v))
