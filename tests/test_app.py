"""Headless tests for the shell's modal popup (fake screen, no curses init)."""

import curses

import pytest

from home_control import config, ui
from home_control.app import Shell
from home_control.systems.base import Popup, System


class _Screen:
    """Records addstr calls as a row -> text grid; enough for draw_box/Region."""

    def __init__(self, h: int, w: int):
        self.h, self.w = h, w
        self.grid: dict[int, dict[int, str]] = {}

    def getmaxyx(self):
        return self.h, self.w

    def addstr(self, y, x, s, _attr=0):
        if not (0 <= y < self.h):
            raise curses.error
        row = self.grid.setdefault(y, {})
        for i, ch in enumerate(s):
            row[x + i] = ch

    def rows(self) -> list[str]:
        return ["".join(self.grid.get(y, {}).get(x, " ") for x in range(self.w)) for y in range(self.h)]


class _Alert(System):
    name = "Test"

    def __init__(self, n: int, title: str = "Info"):
        self.lines = [f"line {i:02d}" for i in range(1, n + 1)]
        self.title = title
        self.open = True

    def collapsed_lines(self, width):
        return []

    def pending_popup(self):
        return Popup(title=self.title, lines=self.lines) if self.open else None

    def dismiss_popup(self):
        self.open = False


@pytest.fixture
def shell(monkeypatch, tmp_path):
    monkeypatch.setattr(ui.curses, "color_pair", lambda n: 0)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config, "_cache", None)

    def make(alert: _Alert) -> Shell:
        return Shell([alert])
    return make


def _draw(sh: Shell, alert: _Alert, h: int = 45, w: int = 80) -> list[str]:
    scr = _Screen(h, w)
    sh._render_popup(scr, h, w, alert)  # type: ignore[arg-type]
    return scr.rows()


def _shown(rows: list[str]) -> list[str]:
    return [r.split("line ")[1][:2] for r in rows if "line " in r]


def test_short_popup_is_unchanged(shell):
    alert = _Alert(5)
    rows = _draw(shell(alert), alert)
    assert _shown(rows) == ["01", "02", "03", "04", "05"]
    assert any("press ENTER to close" in r for r in rows)
    assert not any("scroll" in r for r in rows)


def test_tall_popup_fits_the_screen_and_says_it_scrolls(shell):
    alert = _Alert(60)
    rows = _draw(shell(alert), alert, h=45)
    drawn = [y for y, r in enumerate(rows) if r.strip()]
    assert drawn[0] >= 1 and drawn[-1] <= 43         # a row short of each edge
    assert len(_shown(rows)) == 38                    # 45 - 2 margin - 5 chrome
    assert any("↑↓ scroll (1-38 of 60)" in r and "ENTER to close" in r for r in rows)


def test_arrows_and_page_keys_scroll_and_stop_at_the_ends(shell):
    alert = _Alert(60)
    sh = shell(alert)
    _draw(sh, alert)
    sh.handle_key(curses.KEY_DOWN)
    assert _shown(_draw(sh, alert))[0] == "02"
    sh.handle_key(curses.KEY_NPAGE)
    assert _shown(_draw(sh, alert))[0] == "23"        # down a page from 2nd line, clamped to the end
    sh.handle_key(curses.KEY_DOWN)
    assert _shown(_draw(sh, alert))[-1] == "60"
    for _ in range(3):
        sh.handle_key(curses.KEY_PPAGE)
    assert _shown(_draw(sh, alert))[0] == "01"
    sh.handle_key(curses.KEY_UP)
    assert _shown(_draw(sh, alert))[0] == "01"


def test_offset_restarts_for_a_different_popup_and_after_dismiss(shell):
    alert = _Alert(60)
    sh = shell(alert)
    _draw(sh, alert)
    sh.handle_key(curses.KEY_NPAGE)
    alert.title = "Other unit"
    assert _shown(_draw(sh, alert))[0] == "01"

    sh.handle_key(curses.KEY_NPAGE)
    sh.handle_key(ord("\n"))
    assert not alert.open
    alert.open = True
    assert _shown(_draw(sh, alert))[0] == "01"


def test_offset_reclamps_when_the_popup_shrinks_while_open(shell):
    alert = _Alert(60)
    sh = shell(alert)
    _draw(sh, alert)
    for _ in range(5):
        sh.handle_key(curses.KEY_NPAGE)
    alert.lines = alert.lines[:40]
    assert _shown(_draw(sh, alert)) == [f"{i:02d}" for i in range(3, 41)]
