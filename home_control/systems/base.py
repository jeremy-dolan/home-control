"""The System contract every device panel implements.

A System is split conceptually into two halves that may share state:

  * Controller  — device discovery, polling, and commands. No curses. Runs
                  partly on a background thread (`poll`), so it must be
                  thread-safe: mutate a cached snapshot, never the screen.
  * Panel       — `collapsed_lines` / `render_expanded` draw the cached state;
                  `handle_key` runs only while focused, on the main thread.

The shell never reaches into device internals — it only calls these methods.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..ui import Line, Region

# How long a transient action-confirmation message stays on the status line.
STATUS_TTL = 4.0


@dataclass
class Popup:
    """A modal alert the shell renders over everything until dismissed.

    A System returns one from ``pending_popup()`` when it needs to interrupt the
    user (e.g. Sonos found a speaker not pinned in config). The shell draws it
    centered with an accent border and a "press ENTER to close" footer; ENTER
    calls ``dismiss_popup()`` and other keys are swallowed, so the alert can't be
    typed past by accident. ``color`` defaults to the system's accent when blank.
    """

    title: str
    lines: list[str]
    color: str = ""


@dataclass
class VoiceAction:
    """One voice-callable action a System exposes to the NLU layer.

    The voice router turns each of these into a Claude tool: ``name`` +
    ``description`` + a JSON-schema object built from ``parameters`` / ``required``.
    When Claude calls the tool, ``handler`` runs (off the main thread) with the
    parsed argument dict and returns a short human-readable result string.
    """

    name: str
    description: str
    handler: Callable[[dict[str, Any]], str]
    parameters: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)


class System(ABC):
    # Display identity ------------------------------------------------------
    name: str = "System"
    # Key into ui.SYSTEM_COLORS for the accent color. Defaults to name.lower().
    color_key: str = ""
    # Content lines (excluding borders) to show when collapsed/unfocused.
    collapsed_height: int = 1

    # Background poll cadence (seconds).
    poll_interval_focused: float = 1.0
    poll_interval_idle: float = 5.0

    @property
    def color(self) -> str:
        from ..ui import SYSTEM_COLORS

        return SYSTEM_COLORS.get(self.color_key or self.name.lower(), "white")

    # Transient status (action confirmations shown on the global status line) --
    _status_msg: str = ""
    _status_t: float = 0.0

    def set_status(self, msg: str) -> None:
        self._status_msg = msg
        self._status_t = time.time()

    def status(self) -> str:
        """Short transient message for the global status line ('' if none/stale)."""
        if self._status_msg and time.time() - self._status_t < STATUS_TTL:
            return self._status_msg
        return ""

    # Lifecycle -------------------------------------------------------------
    def start(self) -> None:
        """Begin async connect + first poll. Non-blocking; safe to no-op."""

    def stop(self) -> None:
        """Release resources (threads, sockets). Safe to no-op."""

    # Polling (background thread) -------------------------------------------
    def poll(self, focused: bool) -> None:
        """Refresh cached state. Runs off the main thread; must not touch curses."""

    # Rendering (main thread) ----------------------------------------------
    @abstractmethod
    def collapsed_lines(self, width: int) -> list[Line]:
        """Return up to `collapsed_height` styled lines summarizing status."""

    def render_expanded(self, region: Region) -> None:
        """Draw the full interactive view into the given interior region.

        Default: show the collapsed summary so an unfinished panel still renders.
        """
        for i, line in enumerate(self.collapsed_lines(region.width)):
            region.segs(i, line)

    # Modal alerts (shell-level, focus-independent) ------------------------
    def pending_popup(self) -> Popup | None:
        """A modal the shell should render over everything until dismissed, or
        None when there's nothing to show. Checked every frame for every system,
        not just the focused one, so a background poll can raise an alert."""
        return None

    def dismiss_popup(self) -> None:
        """Acknowledge the current `pending_popup()`; called when the user hits ENTER."""

    def toolbar(self) -> str:
        """Per-system key hints shown above the global toolbar while focused."""
        return ""

    def toolbar_line(self) -> Line | None:
        """Rich toolbar hints (accent-highlighted hotkeys), or None to fall back
        to `toolbar()` rendered as plain, uniformly-colored text."""
        return None

    def help_notes(self) -> list[str]:
        """Optional prose shown in the panel's help popup.

        Each entry is one paragraph; the shell word-wraps it to the popup's
        fixed width and blank-line-separates paragraphs, so don't pre-wrap.
        """
        return []

    # Input (main thread, focused only) ------------------------------------
    def handle_key(self, key: int) -> bool:
        """Handle a key while focused. Return True if consumed (else shell globals)."""
        return False

    def captures_text(self) -> bool:
        """True while the focused panel is in a text-entry state. The shell then
        suspends global bindings on printable keys (SPACE push-to-talk) so they
        reach handle_key as characters instead. TAB stays shell-owned."""
        return False

    # Voice control (NLU via Claude tool-calling) --------------------------
    def voice_actions(self) -> list[VoiceAction]:
        """Voice-callable actions this system exposes. Default: none."""
        return []

    def voice_context(self) -> str:
        """One line of current controllable state (e.g. room/speaker names) to
        help the NLU map natural phrasing onto real devices. '' if nothing."""
        return ""
