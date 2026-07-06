"""Pure vertical layout math for the stacked-panel shell.

No curses here on purpose — these functions are unit-testable headless. The
shell turns the returned Slots into actual box draws.
"""

from __future__ import annotations

from dataclasses import dataclass

# A focused panel never shrinks below this many total rows (incl. borders),
# even if that means the layout overflows a very short terminal (then clipped).
MIN_FOCUSED_HEIGHT = 6
# Rows reserved at the bottom for the global toolbar (1 blank spacer + 1 toolbar).
TOOLBAR_ROWS = 2


@dataclass
class Slot:
    index: int      # index into the systems list
    top: int        # absolute top row of the box
    height: int     # total box height including both borders
    focused: bool


def box_height(collapsed_content_lines: int) -> int:
    """Total rows for a collapsed box: content + top/bottom borders."""
    return collapsed_content_lines + 2


def compute_layout(collapsed_heights: list[int], focused_idx: int, screen_height: int) -> list[Slot]:
    """Stack one box per system; the focused one expands to fill leftover height.

    `collapsed_heights[i]` is the *content* line count system i shows when
    collapsed. The focused system is given all remaining vertical space (above
    the toolbar) after the other systems take their collapsed heights.
    """
    n = len(collapsed_heights)
    if n == 0:
        return []
    focused_idx = max(0, min(n - 1, focused_idx))

    avail = max(0, screen_height - TOOLBAR_ROWS)
    collapsed_total = sum(box_height(h) for i, h in enumerate(collapsed_heights) if i != focused_idx)
    focused_height = max(MIN_FOCUSED_HEIGHT, avail - collapsed_total)

    slots: list[Slot] = []
    top = 0
    for i, h in enumerate(collapsed_heights):
        height = focused_height if i == focused_idx else box_height(h)
        slots.append(Slot(index=i, top=top, height=height, focused=(i == focused_idx)))
        top += height
    return slots


def toolbar_row(screen_height: int) -> int:
    """Absolute row where the global toolbar is drawn."""
    return screen_height - 1
