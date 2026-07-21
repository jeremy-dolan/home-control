"""Toolbar invariants shared by every panel.

`toolbar_line()` is the only toolbar hook (see `Shell._render_focused`); a
panel that returns nothing gets no toolbar at all. Panels used to also carry a
plain-text `toolbar()`, which the shell never reached and which silently
drifted from the hints — Roku's typing modes ended up advertising
"\\ or ESC exit" in a string no one could see.
"""

import pytest

from home_control.systems import hue, midea, roku, router, sonos

# (constructor, attribute holding the mode, every value that attribute takes).
# A panel with a single implicit mode uses (None,).
PANELS = [
    (hue.HueSystem, "mode", ("list", "scenes", "device", "sysinfo")),
    (sonos.SonosSystem, "mode", ("main", "queue", "favorites", "group_confirm", "device_info")),
    (roku.RokuSystem, "mode", ("remote", "apps", "keyboard", "search")),
    (midea.MideaSystem, None, (None,)),
    (router.RouterSystem, None, (None,)),
]

CASES = [(cls, mode) for cls, _attr, modes in PANELS for mode in modes]
IDS = [f"{cls.__name__}-{mode or 'default'}" for cls, mode in CASES]


@pytest.fixture(autouse=True)
def _mock(monkeypatch):
    monkeypatch.setenv("HOME_CONTROL_MOCK", "1")


def _panel(cls, mode):
    panel = cls()
    if mode is not None:
        panel.mode = mode
    return panel


@pytest.mark.parametrize(("cls", "mode"), CASES, ids=IDS)
def test_every_mode_builds_a_rich_toolbar_line(cls, mode):
    # A mode that forgets its hints loses its toolbar entirely — the shell
    # would fall back to `toolbar()`, which is empty for every shipped panel.
    line = _panel(cls, mode).toolbar_line()
    assert line, f"{cls.__name__} mode {mode!r} has no toolbar_line()"


@pytest.mark.parametrize(("cls", "mode"), CASES, ids=IDS)
def test_panels_define_no_plain_text_toolbar(cls, mode):
    # A string-valued toolbar() is a second copy of the text that nothing
    # renders, so it drifts. The hook is gone; don't reintroduce it.
    assert not hasattr(_panel(cls, mode), "toolbar")


def test_text_entry_modes_are_covered_too():
    """Hue and Midea swap in a different toolbar during numeric entry."""
    h = hue.HueSystem()
    h.mode = "device"
    h._num_buf = "2"
    assert h.toolbar_line()
    m = midea.MideaSystem()
    m._num_buf = "2"
    assert m.toolbar_line()


@pytest.mark.parametrize(("cls", "mode"), CASES, ids=IDS)
def test_toolbar_hotkeys_are_the_only_bold_runs(cls, mode):
    # `ui.hint` is the sole producer of bold segments in a toolbar, so the bold
    # runs are exactly the advertised hot keys — relied on when reading a
    # toolbar back. Every mode must advertise at least one.
    line = _panel(cls, mode).toolbar_line() or []
    keys = [s.text for s in line if s.bold]
    assert keys, f"{cls.__name__} mode {mode!r} advertises no hot keys"
    assert all(k.strip() == k and k for k in keys), keys
