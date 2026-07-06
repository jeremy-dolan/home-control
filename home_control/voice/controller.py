"""Voice state machine + worker threads the shell drives.

Push-to-talk flow (typed-stub recognizer):
    off → input (capture a line) → thinking (router on a worker) → result → off

A real mic recognizer (``text_input = False``) instead goes:
    off → listening (transcribe on a worker) → thinking → result → off

STT and the LLM call are slow, so both run on daemon worker threads; the shell's
render loop polls ``snapshot()`` and paints the current state. All shared fields
are guarded by a lock.
"""

from __future__ import annotations

import curses
import threading

from ..systems.base import System
from .recognizer import Recognizer, TextStubRecognizer
from .router import CommandRouter

_ENTER_KEYS = (10, 13, curses.KEY_ENTER)
_BACKSPACE_KEYS = (curses.KEY_BACKSPACE, 127, 8)


class VoiceController:
    def __init__(
        self,
        systems: list[System],
        *,
        recognizer: Recognizer | None = None,
        router: CommandRouter | None = None,
    ):
        self.systems = systems
        self.recognizer = recognizer or TextStubRecognizer()
        self.router = router or CommandRouter(systems)
        self._lock = threading.Lock()
        self.mode = "off"        # off | input | listening | thinking | result
        self.buffer = ""         # typed transcript (input mode)
        self.message = ""        # result / error text
        self.is_error = False

    # -- state access ------------------------------------------------------
    def active(self) -> bool:
        with self._lock:
            return self.mode != "off"

    def snapshot(self) -> tuple[str, str, str, bool]:
        with self._lock:
            return self.mode, self.buffer, self.message, self.is_error

    def _set(self, mode: str, message: str = "", is_error: bool = False) -> None:
        with self._lock:
            self.mode = mode
            self.message = message
            self.is_error = is_error

    def cancel(self) -> None:
        with self._lock:
            self.mode = "off"
            self.buffer = ""
            self.message = ""
            self.is_error = False

    # -- entry point (push-to-talk key) ------------------------------------
    def begin(self) -> None:
        if self.active():
            return
        if not self.router.available():
            self._set("result", "Voice unavailable: set ANTHROPIC_API_KEY", True)
            return
        if self.recognizer.text_input:
            with self._lock:
                self.mode = "input"
                self.buffer = ""
                self.message = ""
                self.is_error = False
        else:
            self._set("listening")
            threading.Thread(target=self._listen_worker, daemon=True).start()

    # -- key handling (delegated by the shell while active) ----------------
    def feed_key(self, key: int) -> bool:
        with self._lock:
            mode = self.mode
        if mode == "result":
            self.cancel()  # any key dismisses the result
            return True
        if mode != "input":
            return True  # swallow input while listening/thinking
        if key == 27:  # ESC cancels
            self.cancel()
            return True
        if key in _ENTER_KEYS:
            with self._lock:
                typed = self.buffer.strip()
                self.buffer = ""
            self._dispatch(self.recognizer.transcribe(typed))
            return True
        if key in _BACKSPACE_KEYS:
            with self._lock:
                self.buffer = self.buffer[:-1]
            return True
        if 32 <= key < 127:
            with self._lock:
                self.buffer += chr(key)
            return True
        return True  # swallow everything else while capturing

    # -- workers -----------------------------------------------------------
    def _listen_worker(self) -> None:
        try:
            text = self.recognizer.transcribe()
        except Exception as e:  # noqa: BLE001
            self._set("result", f"Mic error: {e}", True)
            return
        self._dispatch(text)

    def _dispatch(self, text: str) -> None:
        if not text:
            self.cancel()
            return
        self._set("thinking")
        threading.Thread(target=self._think_worker, args=(text,), daemon=True).start()

    def _think_worker(self, text: str) -> None:
        try:
            out = self.router.handle(text)
        except Exception as e:  # noqa: BLE001
            self._set("result", str(e), True)
            return
        self._set("result", out, False)
