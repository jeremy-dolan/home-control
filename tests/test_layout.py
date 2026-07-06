"""Headless tests for the pure layout math (no curses)."""

from home_control import layout


def test_focused_fills_leftover_space():
    heights = [1, 1, 2, 1]  # collapsed content lines
    slots = layout.compute_layout(heights, focused_idx=1, screen_height=40)
    # Boxes stack with no gaps and consume everything above the toolbar.
    assert slots[0].top == 0
    for prev, cur in zip(slots, slots[1:]):
        assert cur.top == prev.top + prev.height
    used = sum(s.height for s in slots)
    assert used == 40 - layout.TOOLBAR_ROWS
    assert slots[1].focused and not slots[0].focused


def test_collapsed_boxes_use_declared_height():
    heights = [1, 3, 1]
    slots = layout.compute_layout(heights, focused_idx=0, screen_height=50)
    assert slots[1].height == layout.box_height(3)  # 3 content + 2 borders
    assert slots[2].height == layout.box_height(1)


def test_focused_respects_minimum_on_short_screen():
    heights = [1, 1, 1, 1, 1, 1]
    slots = layout.compute_layout(heights, focused_idx=0, screen_height=5)
    assert slots[0].height >= layout.MIN_FOCUSED_HEIGHT


def test_focus_index_clamped():
    heights = [1, 1]
    slots = layout.compute_layout(heights, focused_idx=99, screen_height=20)
    assert slots[-1].focused


def test_empty():
    assert layout.compute_layout([], 0, 24) == []
