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

import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..ui import Line, Region

# How long a transient action-confirmation message stays on the status line.
STATUS_TTL = 4.0

# ---------------------------------------------------------------------------
# Reachability — is the device answering, and what do we say while it isn't
# ---------------------------------------------------------------------------

# Consecutive failed attempts tolerated before a panel says why. Every panel
# retries forever; this is the only knob that says how long it stays quiet
# about it. Three attempts covers a Roku waking from standby or a speaker
# dropping a request, without hiding a real failure indefinitely.
GRACE_ATTEMPTS = 3

# Device libraries report failures with the whole request URL, which can embed
# a token or an API username, and is far wider than a panel line. Keep the host.
_URL_RE = re.compile(r"https?://([^\s/]+)\S*")
# ...usually wrapped in framing the panel already supplies, since it names the
# device itself, plus an error code that says less than the sentence after it.
_REQUEST_RE = re.compile(r"^(?:GET|PUT|POST|DELETE) Request to \S+ (?:failed: )?")
_CODE_RE = re.compile(r"^(?:Error -?\d+:|\[Errno \d+\])\s*")
# urllib hands back the real errno inside "<urlopen error ...>". Keep the inside.
_WRAPPED_RE = re.compile(r"<urlopen error ([^>]*)>")
# When a stack of libraries has wrapped the failure — urllib3's
# HTTPConnectionPool(...) round a NewConnectionError round the real cause — the
# errno clause is the one sentence a person wants, wherever it ended up.
_ERRNO_TAIL_RE = re.compile(r"\[Errno \d+\] ([^'\")>]+)")


def scrub_error(msg: str) -> str:
    """Trim a library error down to the part worth putting on one line.

    ``Error -1: GET Request to http://10.0.0.2/api/<secret>/lights/ failed:
    [Errno 113] No route to host`` → ``No route to host``
    """
    msg = str(msg).strip()
    errno = _ERRNO_TAIL_RE.search(msg)
    if errno:
        return errno.group(1).strip()
    msg = _WRAPPED_RE.sub(r"\1", msg).strip()
    msg = _URL_RE.sub(r"\1", msg).strip()
    msg = _CODE_RE.sub("", msg).strip()
    msg = _REQUEST_RE.sub("", msg).strip()
    return _CODE_RE.sub("", msg).strip()


def in_parens(msg: str) -> str:
    """A reachability message reflowed to sit in parentheses after a device's
    name. Lowercases an ordinary opening word, but leaves an acronym alone —
    "HTTPConnectionPool" must not become "hTTPConnectionPool"."""
    if len(msg) > 1 and msg[1].isupper():
        return msg
    return msg[:1].lower() + msg[1:]


# The four things a panel can be saying while it has no live state, plus LIVE.
LIVE = "live"                  # last attempt landed
CONNECTING = "connecting"      # never reached it, still within grace
FAILED = "failed"              # never reached it, and now we know why
RECONNECTING = "reconnecting"  # had it, missing a beat — last values still stand
UNREACHABLE = "unreachable"    # had it, lost it past grace


@dataclass
class Reachability:
    """Whether a device is answering, and what to say while it isn't.

    Every panel used to answer this its own way and no two agreed: Hue reported
    the first failed read instantly, Roku deliberately stayed silent, Sonos
    swallowed the exception, Midea spoke up only when it had no units at all.
    The variable was never *retrying* — all of them retry forever — but how much
    silence to tolerate first. That is this one number, `grace`.

    Hold one per thing that can independently be reachable: a bridge, a TV, each
    speaker, each AC unit. Poll code calls `succeeded()` / `failed(reason)`;
    render code reads `state`, `message` and `has_values`.
    """

    grace: int = GRACE_ATTEMPTS
    fails: int = 0
    ever: bool = False   # a real read has landed at least once
    reason: str = ""     # why the last attempt failed, already scrubbed

    def succeeded(self) -> None:
        self.fails = 0
        self.ever = True
        self.reason = ""

    def failed(self, reason: str = "") -> None:
        self.fails += 1
        if reason:
            self.reason = scrub_error(reason)

    @property
    def state(self) -> str:
        if self.fails == 0:
            return LIVE if self.ever else CONNECTING
        if not self.ever:
            return CONNECTING if self.fails < self.grace else FAILED
        return RECONNECTING if self.fails < self.grace else UNREACHABLE

    @property
    def has_values(self) -> bool:
        """True when cached device state is worth drawing. A missed beat inside
        grace keeps the last reading on screen; past grace it is no longer
        something we can claim, and never-reached means there is nothing to draw."""
        return self.state in (LIVE, RECONNECTING)

    @property
    def message(self) -> str:
        """One line for the panel: '' while live, else why there's nothing."""
        state = self.state
        if state == LIVE:
            return ""
        if state == CONNECTING:
            return "Connecting..."
        if state == RECONNECTING:
            return "reconnecting..."
        if state == FAILED:
            return self.reason or "unreachable"
        return f"unreachable — {self.reason}" if self.reason else "unreachable"


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

        return SYSTEM_COLORS.get(self.color_key or self.name.lower(), "")

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

    def toolbar_line(self) -> Line | None:
        """Per-system key hints shown above the global toolbar while focused,
        built with `ui.hint`. None (the default) means the panel has no toolbar.

        For unstyled hints return a single plain `Seg` — there's no separate
        string-valued hook.
        """
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
