#!/usr/bin/env python3
"""Preview the panel fill-bars in truecolor, outside curses.

Terminals disagree about how a dim half-block reads next to a bright one, and
the TUI can't show two candidates side by side. This prints the bar styles at
several fill levels so a choice can be made by eye.

Colors come from ui.PALETTE, so this cannot drift from what the panels draw.

    tools/slider-test.py                 # every system accent
    tools/slider-test.py --system sonos
    tools/slider-test.py --width 20
"""

from __future__ import annotations

import argparse

from home_control.ui import PALETTE, SYSTEM_COLORS

RESET, DIM = "\033[0m", "\033[2m"
STYLES = {"plain": "", "dot": "●", "cross": "╫", "full": "█"}
LEVELS = (0.0, 0.2, 0.42, 0.78, 1.0)


def rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def colorize(hex_color: str) -> str:
    r, g, b = rgb(hex_color)
    return f"\033[38;2;{r};{g};{b}m"


def bar(pct: float, hex_color: str, knob: str, width: int) -> str:
    f = round(pct * width)
    filled = ("━" * (f - 1) + knob) if (knob and 0 < f <= width) else "━" * f
    empty = "─" * (width - len(filled))
    return f"{colorize(hex_color)}{filled}{RESET}{DIM}{empty}{RESET}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--system", action="append", dest="systems",
                    choices=sorted(SYSTEM_COLORS), help="limit to one system; repeatable")
    ap.add_argument("--width", type=int, default=12, help="bar width in cells")
    args = ap.parse_args()

    for system in args.systems or sorted(SYSTEM_COLORS):
        name = SYSTEM_COLORS[system]
        hex_color = PALETTE[name]
        print(f"\n{system}  {name} {hex_color}")
        for pct in LEVELS:
            cells = "  ".join(
                f"{label} {bar(pct, hex_color, knob, args.width)}"
                for label, knob in STYLES.items()
            )
            print(f"  {pct * 100:5.0f}%  {cells}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
