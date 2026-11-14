"""The app's colour maths applied to parsed .ans cells: backdrop dimming behind
a popup (ui.backdrop_rgb) and the lighten() lift, both as exact truecolor."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))   # repo root, for home_control
from ansgrid import Style  # noqa: E402

from home_control import ui  # noqa: E402

# Basic ANSI colours are the terminal theme's, not the app's; sampled from a
# screenshot with png.py. Only green (the router chrome) appears in the frames.
TERM_ANSI = {2: (37, 193, 51)}


def rgb_of(fg: tuple | None) -> tuple[int, int, int]:
    if fg is None:
        return ui._DEFAULT_FG          # the app assumes a light-grey default fg
    kind, *v = fg
    if kind == "rgb":
        return v[0], v[1], v[2]
    if kind == "256":
        rgb = ui._cube_rgb(v[0])
        assert rgb is not None, f"base-16 colour as 256-index {v[0]}"
        return rgb
    return TERM_ANSI[v[0]]


def recede(st: Style) -> Style:
    """`st` as drawn behind a popup: colour scaled by BACKDROP_SCALE, attributes kept."""
    r, g, b = (round(c * ui.BACKDROP_SCALE) for c in rgb_of(st.fg))
    return Style(fg=("rgb", r, g, b), bold=st.bold, dim=st.dim)


def palette(name: str) -> tuple:
    return ("rgb", *ui._hex_rgb(ui.PALETTE[name]))


def lighten(name: str) -> tuple:
    rgb = ui._lighten_rgb(name)
    assert rgb is not None, f"{name} not in PALETTE"
    return ("rgb", *rgb)
